#!/usr/bin/env python3
"""コメント付きデータの器（引き継ぎ書 第8章）。

SQLite（output/ugc.db）を正とし、配布物は次の4点を ZIP 1本にまとめる:
  videos.csv    1行=1動画。既存13列を順序ごと先頭に据え置き、後ろに列を追記
  comments.csv  1行=1コメント。返信は parent_comment_id で自己参照（別テーブルにしない）
  meta.json     件数・検算結果・入力ファイルの素性
  README.txt    読み方と Excel での注意

入力:
  既存スクレイパーの動画CSV（13列: Index, URL, ..., Username, Song Name/Project Name）
  spatest.py の JSONL（1行1動画。comments=本体 / reply_comments=返信）

守っていること（第8章。覆さない）:
  - 粒度を混ぜない（動画とコメントは別ファイル）。縦持ち1本・横持ちは作らない
  - 19桁ID（video_id / comment_id / user_id）は常に文字列。int にしない
  - CSV書き出し時のみ = + - @ で始まるセルを ' でエスケープ。SQLite には生のまま
  - 既存13列は名前も順序も入力CSVのまま

標準ライブラリのみ。pandas は使わない（NaN 混入で整数列が float になり、19桁IDが壊れるため）。

使い方:
  python store.py build --project プロポーズ --videos-csv プロポーズ.csv \\
                        --comments-jsonl ~/tiktok-overnight/pace100.jsonl
  python store.py check output/プロポーズ_20260911_180000.zip \\
                        --comments-jsonl ~/tiktok-overnight/pace100.jsonl --videos-csv プロポーズ.csv
  python store.py runs
"""

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_DB = os.path.join("output", "ugc.db")

# 既存13列。先頭12列は名前も順序も固定。13列目は世代で名前が違う（Song Name / Project Name）
FIXED_HEAD = ["Index", "URL", "Created Date", "Description", "Likes", "Shares",
              "Comments", "Plays", "Saves", "Reposts", "Type", "Username"]
HEAD_LEN = 13
# 13列 → videos テーブルの列名（同じ順）
HEAD_TO_DB = ["idx", "url", "created_date", "description", "likes", "shares",
              "comment_count", "plays", "saves", "reposts", "type", "username", "project_name"]
HEAD_INT = {"idx", "likes", "shares", "comment_count", "plays", "saves", "reposts"}

# videos.csv の追記列（13列の後ろ）
VIDEO_EXTRA = ["video_id", "comments_fetched", "replies_fetched", "comments_status",
               "comments_total_reported", "comments_has_more", "top_list_count",
               "snapshot_at", "run_id"]

# comments.csv の列
COMMENT_COLS = ["comment_id", "video_id", "parent_comment_id", "reply_to_comment_id", "level",
                "text", "like_count", "reply_count", "created_at",
                "user_id", "user_unique_id", "user_nickname",
                "is_author", "is_pinned", "is_author_liked", "is_top_list",
                "language", "image_url", "source", "tiktok_status", "snapshot_at", "run_id"]

# spatest.py の status → comments_status。未知の値はそのまま通す
STATUS_MAP = {"ok": "ok", "no_comments": "empty", "blocked": "blocked"}

FORMULA_PREFIX = ("=", "+", "-", "@")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  project_name TEXT,
  videos_csv TEXT,
  videos_csv_header TEXT,      -- 入力CSVの先頭13列のヘッダ（JSON配列）。書き戻すときそのまま使う
  comments_jsonl TEXT,
  snapshot_at TEXT,
  snapshot_at_source TEXT,
  ingested_at TEXT,
  video_count INTEGER,
  comment_count INTEGER,       -- level 1（本体）
  reply_count INTEGER,         -- level 2（返信。reply_api + embedded）
  stats_json TEXT,
  status TEXT
);
CREATE TABLE IF NOT EXISTS videos (
  run_id TEXT NOT NULL,
  video_id TEXT NOT NULL,      -- URL 末尾の数値ID。必ず TEXT
  seq INTEGER,                 -- 入力CSVの行順（書き戻しの順序）
  idx INTEGER, url TEXT, created_date TEXT, description TEXT,
  likes INTEGER, shares INTEGER, comment_count INTEGER,
  plays INTEGER, saves INTEGER, reposts INTEGER, type TEXT, username TEXT, project_name TEXT,
  comments_fetched INTEGER,    -- 実際に取れた本体の件数
  replies_fetched INTEGER,     -- 実際に取れた返信の件数
  comments_status TEXT,        -- ok / empty / blocked / skipped / (spatest の生の値)
  comments_total_reported INTEGER,  -- API が申告した総数（第7章: 必ず記録）
  comments_has_more INTEGER,
  top_list_count INTEGER,      -- 上位リスト（sort_tags.top_list=1）の件数
  snapshot_at TEXT,
  PRIMARY KEY (run_id, video_id)
);
CREATE TABLE IF NOT EXISTS comments (
  run_id TEXT NOT NULL,
  comment_id TEXT NOT NULL,    -- cid。必ず TEXT
  video_id TEXT NOT NULL,      -- aweme_id。必ず TEXT
  seq INTEGER,
  parent_comment_id TEXT NOT NULL DEFAULT '',    -- 空=本体。返信は最上位の親（reply_id）
  reply_to_comment_id TEXT NOT NULL DEFAULT '',  -- 返信への返信のとき返信先（reply_to_reply_id）
  level INTEGER NOT NULL,      -- 1=本体 / 2=返信
  text TEXT, like_count INTEGER, reply_count INTEGER, created_at TEXT,
  user_id TEXT, user_unique_id TEXT, user_nickname TEXT,
  is_author INTEGER, is_pinned INTEGER, is_author_liked INTEGER, is_top_list INTEGER,
  language TEXT, image_url TEXT,
  source TEXT,                 -- main / reply_api / embedded
  tiktok_status INTEGER,
  snapshot_at TEXT,
  PRIMARY KEY (run_id, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(run_id, video_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(run_id, parent_comment_id);
"""


class StoreError(Exception):
    pass


# ---------------------------------------------------------------------------
# 小道具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def utc_str(unix: Any) -> str:
    """UNIX秒 → 'YYYY-MM-DD HH:MM:SS'（UTC）。既存の Created Date（Video）と同じ基準"""
    try:
        return datetime.datetime.fromtimestamp(int(unix), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def video_id_from_url(url: str) -> str:
    m = re.search(r"/(?:video|photo)/(\d+)", url or "")
    return m.group(1) if m else ""


def to_int_or_text(s: Any):
    """CSVのセル → int / None / 元の文字列。'2098.0' のような整数値の float も int にする"""
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
        if f.is_integer():
            return int(f)
    except ValueError:
        pass
    return s


def escape_cell(v: Any) -> Any:
    """CSV書き出し時のみ。Excel が数式と解釈する前置文字を ' で無効化する"""
    if isinstance(v, str) and v[:1] in FORMULA_PREFIX:
        return "'" + v
    return v


def unescape_cell(v: str) -> str:
    if v[:1] == "'" and v[1:2] in FORMULA_PREFIX:
        return v[1:]
    return v


def fmt_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return escape_cell(str(v))


def file_fingerprint(path: str) -> Dict[str, Any]:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    st = os.stat(path)
    return {
        "path": os.path.abspath(path),
        "size": st.st_size,
        "md5": h.hexdigest(),
        "mtime": datetime.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
    }


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "").strip()
    return cleaned or "project"


# ---------------------------------------------------------------------------
# 入力の読み込み
# ---------------------------------------------------------------------------

def read_videos_csv(path: str) -> Tuple[List[str], List[List[str]]]:
    """既存13列CSVを文字列のまま読む。戻り値は (先頭13列のヘッダ, 行[先頭13列])"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            raise StoreError(f"動画CSVが空です: {path}")
        rows = [r for r in reader if any(c.strip() for c in r)]
    if len(header) < HEAD_LEN:
        raise StoreError(f"動画CSVの列が13列未満です（{len(header)}列）: {header}")
    if header[:len(FIXED_HEAD)] != FIXED_HEAD:
        raise StoreError(
            "動画CSVの先頭12列が既存の形式と違います。\n"
            f"  期待: {FIXED_HEAD}\n  実際: {header[:len(FIXED_HEAD)]}")
    head13 = header[:HEAD_LEN]
    out = []
    for r in rows:
        r = list(r[:HEAD_LEN]) + [""] * max(0, HEAD_LEN - len(r))
        out.append(r)
    return head13, out


def read_comments_jsonl(path: str) -> Tuple[List[Dict[str, Any]], int]:
    """spatest.py の JSONL。同じ video_id が複数行あれば後の行を採用し、その件数も返す"""
    rows: Dict[str, Dict[str, Any]] = {}
    dup = 0
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                raise StoreError(f"JSONL {ln}行目が読めません: {e}")
            vid = str(r.get("video_id") or "")
            if not vid:
                raise StoreError(f"JSONL {ln}行目に video_id がありません")
            if vid in rows:
                dup += 1
            rows[vid] = r
    return list(rows.values()), dup


# ---------------------------------------------------------------------------
# コメント1件の正規化
# ---------------------------------------------------------------------------

def _label_types(c: Dict[str, Any]) -> set:
    ts = set()
    for lab in (c.get("label_list") or []):
        if isinstance(lab, dict) and lab.get("type") is not None:
            ts.add(lab["type"])
    if c.get("label_type") is not None:      # 同梱返信はこちらの形
        ts.add(c["label_type"])
    return ts


def _first_image_url(c: Dict[str, Any]) -> str:
    for im in (c.get("image_list") or []):
        if not isinstance(im, dict):
            continue
        for key in ("origin_url", "crop_url"):
            urls = (im.get(key) or {}).get("url_list") or []
            if urls:
                return str(urls[0])
    return ""


def _top_list_flag(c: Dict[str, Any]) -> Optional[int]:
    st = c.get("sort_tags")
    if not st:
        return None
    try:
        return 1 if json.loads(st).get("top_list") == 1 else 0
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def normalize_comment(c: Dict[str, Any], video_id: str, level: int, source: str,
                      snapshot_at: str) -> Dict[str, Any]:
    user = c.get("user") or {}
    parent = str(c.get("reply_id") or "0")
    reply_to = str(c.get("reply_to_reply_id") or "0")
    labels = _label_types(c)
    return {
        "comment_id": str(c.get("cid") or ""),
        "video_id": video_id,
        "parent_comment_id": "" if parent == "0" else parent,
        "reply_to_comment_id": "" if reply_to == "0" else reply_to,
        "level": level,
        "text": c.get("text") or "",
        "like_count": int(c.get("digg_count") or 0),
        "reply_count": c.get("reply_comment_total"),   # 返信側は None
        "created_at": utc_str(c.get("create_time")),
        "user_id": str(user.get("uid") or ""),
        "user_unique_id": str(user.get("unique_id") or ""),
        "user_nickname": str(user.get("nickname") or ""),
        "is_author": 1 if 1 in labels else 0,           # label type 1 = 「投稿者」
        "is_pinned": 1 if c.get("author_pin") else 0,
        "is_author_liked": 1 if c.get("is_author_digged") else 0,
        "is_top_list": _top_list_flag(c),
        "language": c.get("comment_language") or "",
        "image_url": _first_image_url(c),
        "source": source,
        "tiktok_status": c.get("status"),
        "snapshot_at": snapshot_at,
    }


# ---------------------------------------------------------------------------
# 取り込み（JSONL + CSV → SQLite）
# ---------------------------------------------------------------------------

def open_db(db_path: str) -> sqlite3.Connection:
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def ingest(db_path: str, project_name: str, videos_csv: Optional[str], comments_jsonl: str,
           run_id: Optional[str] = None, snapshot_at: Optional[str] = None,
           replace: bool = False) -> Dict[str, Any]:
    """入力2ファイルを SQLite に取り込み、統計を返す。

    videos_csv が None の場合は JSONL だけで videos を作る（13列は空。検証用）。
    aweme_id が video_id と一致しないコメントが1件でもあれば取り込まない（2-10 の混入）。
    """
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = run_id or f"{safe_filename(project_name)}_{ts}"

    if videos_csv:
        head13, vrows = read_videos_csv(videos_csv)
    else:
        head13, vrows = FIXED_HEAD + ["Project Name"], []
    jrows, dup_video_rows = read_comments_jsonl(comments_jsonl)

    if snapshot_at:
        snapshot_src = "argument"
    else:
        snapshot_at = file_fingerprint(comments_jsonl)["mtime"]
        snapshot_src = "jsonl_mtime"

    # --- videos: CSV 行を主、JSONL だけにある動画を末尾に追加 ---
    videos: List[Dict[str, Any]] = []
    seen_vid: Dict[str, int] = {}
    bad_urls: List[str] = []
    for i, r in enumerate(vrows):
        vid = video_id_from_url(r[1])
        if not vid:
            bad_urls.append(r[1])
            continue
        if vid in seen_vid:
            raise StoreError(f"動画CSVに同じ video_id が2回あります: {vid}（{seen_vid[vid]+2}行目と{i+2}行目）")
        seen_vid[vid] = i
        v = {"seq": i, "video_id": vid}
        for name, cell in zip(HEAD_TO_DB, r):
            v[name] = to_int_or_text(cell) if name in HEAD_INT else cell
        videos.append(v)
    if bad_urls:
        raise StoreError("動画CSVの URL から video_id が取れない行があります:\n  " + "\n  ".join(bad_urls[:10]))

    jmap = {str(r["video_id"]): r for r in jrows}
    only_in_jsonl = [vid for vid in jmap if vid not in seen_vid]
    # CSV に無い動画（CSV 収集後に投稿されたもの等）。13列は空のまま末尾に置く
    for k, vid in enumerate(only_in_jsonl):
        v = {"seq": len(videos) + k, "video_id": vid}
        for name in HEAD_TO_DB:
            v[name] = None
        videos.append(v)

    # --- comments ---
    comments: List[Dict[str, Any]] = []
    seen_cid: set = set()
    mixed: List[Tuple[str, str, str]] = []
    skipped_dup = {"main": 0, "reply_api": 0, "embedded": 0}
    non_digit_ids = 0
    per_video: Dict[str, Dict[str, int]] = {}

    def add(c: Dict[str, Any], vid: str, level: int, source: str):
        nonlocal non_digit_ids
        aw = str(c.get("aweme_id") or "")
        if aw != vid:
            mixed.append((vid, aw, str(c.get("cid"))))
            return
        n = normalize_comment(c, vid, level, source, snapshot_at)
        if not n["comment_id"]:
            raise StoreError(f"cid の無いコメントがあります（video {vid}）")
        if not n["comment_id"].isdigit():
            non_digit_ids += 1
        if n["comment_id"] in seen_cid:
            skipped_dup[source] += 1
            return
        seen_cid.add(n["comment_id"])
        n["seq"] = len(comments)
        comments.append(n)
        pv = per_video.setdefault(vid, {"main": 0, "reply_api": 0, "embedded": 0})
        pv[source] += 1

    # 優先順: 本体 → 返信API → 同梱返信（同じ cid があれば先勝ち）
    for r in jrows:
        vid = str(r["video_id"])
        for c in (r.get("comments") or []):
            add(c, vid, 1, "main")
    for r in jrows:
        vid = str(r["video_id"])
        for c in (r.get("reply_comments") or []):
            add(c, vid, 2, "reply_api")
    for r in jrows:
        vid = str(r["video_id"])
        for c in (r.get("comments") or []):
            for e in (c.get("reply_comment") or []):
                add(e, vid, 2, "embedded")

    if mixed:
        sample = "\n  ".join(f"video={v} aweme_id={a} cid={c}" for v, a, c in mixed[:5])
        raise StoreError(
            f"aweme_id が video_id と一致しないコメントが {len(mixed)} 件あります（2-10 の混入）。取り込みません。\n  {sample}")

    # --- videos にコメント側の情報を付ける ---
    for v in videos:
        vid = v["video_id"]
        j = jmap.get(vid)
        pv = per_video.get(vid, {"main": 0, "reply_api": 0, "embedded": 0})
        v["comments_fetched"] = pv["main"]
        v["replies_fetched"] = pv["reply_api"] + pv["embedded"]
        if j is None:
            v["comments_status"] = "skipped"
            v["comments_total_reported"] = None
            v["comments_has_more"] = None
            v["top_list_count"] = None
            v["snapshot_at"] = None
        else:
            raw = str(j.get("status") or "")
            v["comments_status"] = STATUS_MAP.get(raw, raw or "unknown")
            v["comments_total_reported"] = j.get("total")
            v["comments_has_more"] = j.get("has_more")
            v["top_list_count"] = j.get("top_list")
            v["snapshot_at"] = snapshot_at

    stats = {
        "run_id": run_id,
        "project_name": project_name,
        "snapshot_at": snapshot_at,
        "snapshot_at_source": snapshot_src,
        "inputs": {
            "videos_csv": file_fingerprint(videos_csv) if videos_csv else None,
            "comments_jsonl": file_fingerprint(comments_jsonl),
            "videos_csv_header": head13,
        },
        "videos": {
            "in_csv": len(vrows),
            "in_jsonl": len(jrows),
            "matched": sum(1 for v in videos if v["url"] is not None and v["video_id"] in jmap),
            "only_in_csv": sum(1 for v in videos if v["url"] is not None and v["video_id"] not in jmap),
            "only_in_jsonl": len(only_in_jsonl),
            "total_rows": len(videos),
            "jsonl_duplicate_rows": dup_video_rows,
            "status": {},
        },
        "comments": {
            "input_main": sum(len(r.get("comments") or []) for r in jrows),
            "input_reply_api": sum(len(r.get("reply_comments") or []) for r in jrows),
            "input_embedded": sum(len(c.get("reply_comment") or []) for r in jrows for c in (r.get("comments") or [])),
            "level1": sum(1 for c in comments if c["level"] == 1),
            "level2": sum(1 for c in comments if c["level"] == 2),
            "by_source": {s: sum(1 for c in comments if c["source"] == s) for s in ("main", "reply_api", "embedded")},
            "skipped_duplicate_cid": skipped_dup,
            "total_rows": len(comments),
            "aweme_id_mismatch": len(mixed),
            "non_digit_comment_id": non_digit_ids,
            "reply_to_reply": sum(1 for c in comments if c["reply_to_comment_id"]),
        },
    }
    st_count: Dict[str, int] = {}
    for v in videos:
        st_count[v["comments_status"]] = st_count.get(v["comments_status"], 0) + 1
    stats["videos"]["status"] = st_count

    # --- 書き込み ---
    conn = open_db(db_path)
    try:
        cur = conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,))
        if cur.fetchone():
            if not replace:
                raise StoreError(f"run_id {run_id} は既に取り込み済みです。--replace で上書きできます")
            for t in ("comments", "videos", "runs"):
                conn.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
        vcols = ["run_id", "video_id", "seq"] + HEAD_TO_DB + [
            "comments_fetched", "replies_fetched", "comments_status",
            "comments_total_reported", "comments_has_more", "top_list_count", "snapshot_at"]
        conn.executemany(
            f"INSERT INTO videos ({','.join(vcols)}) VALUES ({','.join('?' * len(vcols))})",
            [[run_id] + [v.get(k) for k in vcols[1:]] for v in videos])
        ccols = ["run_id", "seq"] + [c for c in COMMENT_COLS if c != "run_id"]
        conn.executemany(
            f"INSERT INTO comments ({','.join(ccols)}) VALUES ({','.join('?' * len(ccols))})",
            [[run_id] + [c.get(k) for k in ccols[1:]] for c in comments])
        conn.execute(
            "INSERT INTO runs (run_id, project_name, videos_csv, videos_csv_header, comments_jsonl, "
            "snapshot_at, snapshot_at_source, ingested_at, video_count, comment_count, reply_count, "
            "stats_json, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, project_name, os.path.abspath(videos_csv) if videos_csv else None,
             json.dumps(head13, ensure_ascii=False), os.path.abspath(comments_jsonl),
             snapshot_at, snapshot_src, now_iso(), len(videos),
             stats["comments"]["level1"], stats["comments"]["level2"],
             json.dumps(stats, ensure_ascii=False), "ingested"))
        conn.commit()
    finally:
        conn.close()
    return stats


# ---------------------------------------------------------------------------
# 書き出し（SQLite → CSV ×2 + meta.json + README.txt → ZIP）
# ---------------------------------------------------------------------------

def _csv_bytes(header: List[str], rows: Iterable[List[Any]]) -> bytes:
    """utf-8-sig（Excel が文字化けしない）/ LF（既存出力と同じ）/ 必要なセルだけ引用"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(header)
    for r in rows:
        w.writerow([fmt_cell(c) for c in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _load_run(conn: sqlite3.Connection, run_id: str) -> Dict[str, Any]:
    conn.row_factory = sqlite3.Row
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise StoreError(f"run_id {run_id} がありません（`python store.py runs` で一覧）")
    return dict(run)


def export_zip(db_path: str, run_id: str, out_dir: str = "output",
               zip_path: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    conn = open_db(db_path)
    try:
        run = _load_run(conn, run_id)
        head13 = json.loads(run["videos_csv_header"])
        videos = [dict(r) for r in conn.execute(
            "SELECT * FROM videos WHERE run_id=? ORDER BY seq", (run_id,))]
        comments = [dict(r) for r in conn.execute(
            "SELECT * FROM comments WHERE run_id=? ORDER BY seq", (run_id,))]
    finally:
        conn.close()

    # videos.csv: 13列（入力のヘッダ名のまま）+ 追記列
    vheader = head13 + VIDEO_EXTRA
    vrows = [[v[k] for k in HEAD_TO_DB] + [v.get(k) if k != "run_id" else run_id for k in VIDEO_EXTRA]
             for v in videos]

    # comments.csv: 動画順 → 本体は取得順、その直後にその本体への返信
    by_video: Dict[str, List[Dict[str, Any]]] = {}
    for c in comments:
        by_video.setdefault(c["video_id"], []).append(c)
    ordered: List[Dict[str, Any]] = []
    video_order = [v["video_id"] for v in videos] + [vid for vid in by_video if vid not in {v["video_id"] for v in videos}]
    for vid in video_order:
        cs = by_video.get(vid) or []
        tops = [c for c in cs if c["level"] == 1]
        reps: Dict[str, List[Dict[str, Any]]] = {}
        for c in cs:
            if c["level"] != 1:
                reps.setdefault(c["parent_comment_id"], []).append(c)
        top_ids = set()
        for t in tops:
            ordered.append(t)
            top_ids.add(t["comment_id"])
            ordered.extend(reps.get(t["comment_id"], []))
        for pid, rs in reps.items():           # 親が見つからない返信は末尾（検算で拾う）
            if pid not in top_ids:
                ordered.extend(rs)
    crows = [[c.get(k) if k != "run_id" else run_id for k in COMMENT_COLS] for c in ordered]

    stats = json.loads(run["stats_json"] or "{}")
    escaped = {f"videos.{k}": n for k, n in _count_escaped(vheader, vrows).items()}
    escaped.update({f"comments.{k}": n for k, n in _count_escaped(COMMENT_COLS, crows).items()})
    meta = {
        "format_version": 1,
        "run_id": run_id,
        "project_name": run["project_name"],
        "snapshot_at": run["snapshot_at"],
        "snapshot_at_source": run["snapshot_at_source"],
        "ingested_at": run["ingested_at"],
        "exported_at": now_iso(),
        "timezone_note": "created_at / Created Date は UTC。snapshot_at はローカル時刻（オフセット付き）",
        "files": {
            "videos.csv": {"rows": len(vrows), "columns": vheader},
            "comments.csv": {"rows": len(crows), "columns": COMMENT_COLS},
        },
        "escaped_cells": escaped,
        "stats": stats,
    }

    os.makedirs(out_dir, exist_ok=True)
    if not zip_path:
        zip_path = os.path.join(out_dir, f"{run_id}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("videos.csv", _csv_bytes(vheader, vrows))
        z.writestr("comments.csv", _csv_bytes(COMMENT_COLS, crows))
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        z.writestr("README.txt", README.format(
            project=run["project_name"], run_id=run_id, snapshot_at=run["snapshot_at"],
            n_videos=len(vrows), n_comments=len(crows), head13=", ".join(head13)))
    return zip_path, meta


def _count_escaped(header: List[str], rows: Iterable[List[Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        for name, v in zip(header, r):
            if isinstance(v, str) and v[:1] in FORMULA_PREFIX:
                out[name] = out.get(name, 0) + 1
    return out


# ---------------------------------------------------------------------------
# 検算（書き出した ZIP を読み直して確かめる）
# ---------------------------------------------------------------------------

def _read_csv_from_zip(z: zipfile.ZipFile, name: str) -> Tuple[List[str], List[List[str]]]:
    text = z.read(name).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:]


def check_zip(zip_path: str, comments_jsonl: Optional[str] = None,
              videos_csv: Optional[str] = None) -> Tuple[bool, List[Tuple[str, bool, str]]]:
    """ZIP の中身だけで成り立つ検算と、入力ファイルを渡したときの突き合わせ。

    戻り値: (全部OKか, [(項目, OKか, 説明)])
    """
    res: List[Tuple[str, bool, str]] = []

    def ok(name: str, cond: bool, detail: str):
        res.append((name, bool(cond), detail))

    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        ok("ZIPの構成", {"videos.csv", "comments.csv", "meta.json", "README.txt"} <= names, f"{sorted(names)}")
        vh, vrows = _read_csv_from_zip(z, "videos.csv")
        ch, crows = _read_csv_from_zip(z, "comments.csv")
        meta = json.loads(z.read("meta.json").decode("utf-8"))

    head13 = meta["stats"]["inputs"]["videos_csv_header"]
    ok("videos.csv 先頭13列のヘッダが入力と一致", vh[:HEAD_LEN] == head13, f"{vh[:HEAD_LEN]}")
    ok("videos.csv 追記列", vh[HEAD_LEN:] == VIDEO_EXTRA, f"{vh[HEAD_LEN:]}")
    ok("comments.csv の列", ch == COMMENT_COLS, f"{len(ch)}列")

    vidx = {n: i for i, n in enumerate(vh)}
    cidx = {n: i for i, n in enumerate(ch)}
    V = lambda r, k: r[vidx[k]]
    C = lambda r, k: r[cidx[k]]

    # ID の型（全て数字だけの文字列か）
    vids = [V(r, "video_id") for r in vrows]
    cids = [C(r, "comment_id") for r in crows]
    ok("video_id が全行 数字のみ", all(x.isdigit() for x in vids), f"{len(vids)}行")
    ok("comment_id が全行 数字のみ", all(x.isdigit() for x in cids), f"{len(cids)}行")
    n19 = sum(1 for x in vids if len(x) == 19)
    ok("video_id の桁数", True, f"19桁 {n19}/{len(vids)}（19桁以外は古い動画の可能性。エラーではない）")
    ok("comment_id に重複なし", len(set(cids)) == len(cids), f"{len(cids) - len(set(cids))}件重複")
    ok("video_id に重複なし", len(set(vids)) == len(vids), f"{len(vids) - len(set(vids))}件重複")

    # 結合の整合
    vset = set(vids)
    orphans = [r for r in crows if C(r, "video_id") not in vset]
    ok("comments の video_id が全て videos に存在（孤児0件）", not orphans, f"孤児 {len(orphans)}件")
    cvid = {C(r, "comment_id"): C(r, "video_id") for r in crows}
    bad_parent = []
    for r in crows:
        lv, pid = C(r, "level"), C(r, "parent_comment_id")
        if lv == "1":
            if pid != "":
                bad_parent.append(("level1に親", r[0]))
        else:
            if pid not in cvid:
                bad_parent.append(("親が無い", r[0]))
            elif cvid[pid] != C(r, "video_id"):
                bad_parent.append(("親が別動画", r[0]))
    ok("返信の parent_comment_id が同じ動画の本体を指す", not bad_parent,
       f"不整合 {len(bad_parent)}件 {bad_parent[:3]}")

    # 動画ごとの件数が comments.csv の実数と一致
    per_l1: Dict[str, int] = {}
    per_l2: Dict[str, int] = {}
    for r in crows:
        d = per_l1 if C(r, "level") == "1" else per_l2
        d[C(r, "video_id")] = d.get(C(r, "video_id"), 0) + 1
    mism = [(V(r, "video_id"), V(r, "comments_fetched"), per_l1.get(V(r, "video_id"), 0))
            for r in vrows if int(V(r, "comments_fetched") or 0) != per_l1.get(V(r, "video_id"), 0)]
    mism2 = [(V(r, "video_id"), V(r, "replies_fetched"), per_l2.get(V(r, "video_id"), 0))
             for r in vrows if int(V(r, "replies_fetched") or 0) != per_l2.get(V(r, "video_id"), 0)]
    ok("videos.comments_fetched == comments.csv の level1 件数", not mism, f"不一致 {len(mism)}本 {mism[:3]}")
    ok("videos.replies_fetched == comments.csv の level2 件数", not mism2, f"不一致 {len(mism2)}本 {mism2[:3]}")

    # エスケープ: CSV上に = + - @ で始まるセルが残っていない
    raw_formula = sum(1 for r in vrows + crows for c in r if c[:1] in FORMULA_PREFIX)
    ok("CSV に = + - @ で始まるセルが無い", raw_formula == 0, f"{raw_formula}件")
    ok("meta.json の件数 == CSV の行数",
       meta["files"]["videos.csv"]["rows"] == len(vrows) and meta["files"]["comments.csv"]["rows"] == len(crows),
       f"videos {len(vrows)} / comments {len(crows)}")

    # 入力との突き合わせ
    if videos_csv:
        ih, irows = read_videos_csv(videos_csv)
        ok("入力CSVのヘッダ == videos.csv 先頭13列", ih == vh[:HEAD_LEN], f"{ih[-1]!r}")
        diffs = []
        for i, ir in enumerate(irows):
            if i >= len(vrows):
                diffs.append((i, "行が足りない"))
                continue
            got = [unescape_cell(c) for c in vrows[i][:HEAD_LEN]]
            if got != ir:
                diffs.append((i, [(a, b) for a, b in zip(ir, got) if a != b][:2]))
        ok("入力CSVの13列が行順・値ともに保存されている（エスケープを戻して比較）",
           not diffs and len(vrows) >= len(irows), f"差分 {len(diffs)}行 {diffs[:3]}")
        ok("入力CSVに無い動画（JSONLのみ）は末尾",
           all(V(r, "URL") == "" for r in vrows[len(irows):]), f"{len(vrows) - len(irows)}本")

    if comments_jsonl:
        jrows, _ = read_comments_jsonl(comments_jsonl)
        n_main = sum(len(r.get("comments") or []) for r in jrows)
        n_rapi = sum(len(r.get("reply_comments") or []) for r in jrows)
        emb_ids = {e.get("cid") for r in jrows for c in (r.get("comments") or []) for e in (c.get("reply_comment") or [])}
        rapi_ids = {c.get("cid") for r in jrows for c in (r.get("reply_comments") or [])}
        main_ids = {c.get("cid") for r in jrows for c in (r.get("comments") or [])}
        n_emb_unique = len(emb_ids - rapi_ids - main_ids)
        src = {}
        for r in crows:
            src[C(r, "source")] = src.get(C(r, "source"), 0) + 1
        ok("本体の件数 == JSONL の comments 合計", src.get("main", 0) == n_main, f"CSV {src.get('main', 0)} / JSONL {n_main}")
        ok("返信API の件数 == JSONL の reply_comments 合計", src.get("reply_api", 0) == n_rapi, f"CSV {src.get('reply_api', 0)} / JSONL {n_rapi}")
        ok("同梱返信の件数 == JSONL の reply_comment 合計（重複除く）", src.get("embedded", 0) == n_emb_unique,
           f"CSV {src.get('embedded', 0)} / JSONL {n_emb_unique}（生 {len(emb_ids)}）")
        jv = {str(r["video_id"]) for r in jrows}
        ok("JSONL の動画が全て videos.csv にある", jv <= vset, f"{len(jv - vset)}本欠け")
        ok("JSONL に無い動画は comments_status=skipped",
           all(V(r, "comments_status") == "skipped" for r in vrows if V(r, "video_id") not in jv),
           f"{sum(1 for r in vrows if V(r, 'video_id') not in jv)}本")
        # 「投稿者」ラベル（is_author）の解釈の確認
        #   合否: 動画ごとに is_author=1 の人物は1人だけか（2人以上ならラベルの読み違い）
        #   参考: 動画の Username と一致する割合。unique_id は変更できるので不一致=誤りではない
        #        （実測: CSV と7か月差の pace100 で 316/324。不一致8件は2動画に集中し各1人）
        authors: Dict[str, set] = {}
        for r in crows:
            if C(r, "is_author") == "1":
                authors.setdefault(C(r, "video_id"), set()).add(C(r, "user_id"))
        multi = [v for v, s in authors.items() if len(s) > 1]
        ok("is_author=1 の人物が動画ごとに1人", not multi, f"{len(authors)}本中 複数人 {len(multi)}本 {multi[:3]}")
        uname = {V(r, "video_id"): V(r, "Username") for r in vrows if V(r, "Username")}
        auth = [r for r in crows if C(r, "is_author") == "1" and C(r, "video_id") in uname]
        agree = sum(1 for r in auth if unescape_cell(C(r, "user_unique_id")) == unescape_cell(uname[C(r, "video_id")]))
        ok("（参考）is_author=1 の unique_id が動画の Username と一致", True,
           f"{agree}/{len(auth)}（不一致は unique_id 変更の可能性。合否には含めない）")

    return all(r[1] for r in res), res


def format_report(res: List[Tuple[str, bool, str]]) -> str:
    lines = [f"  [{'OK' if good else 'NG'}] {name}: {detail}" for name, good, detail in res]
    lines.append("結果: " + ("全項目 OK" if all(r[1] for r in res) else "NG あり"))
    return "\n".join(lines)


def print_report(res: List[Tuple[str, bool, str]]) -> None:
    print(format_report(res))


def attach_check(zip_path: str, res: List[Tuple[str, bool, str]]) -> None:
    """検算結果を ZIP に check.txt として同梱する（受け取った側が端末なしで確認できるように）"""
    body = f"検算 {now_iso()}\n" + format_report(res) + "\n"
    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("check.txt", body)


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

README = """TikTok UGC Analyzer — コメント付きデータ
========================================
プロジェクト : {project}
run_id       : {run_id}
取得時点     : {snapshot_at}
動画         : {n_videos} 行 (videos.csv)
コメント     : {n_comments} 行 (comments.csv)

ファイル
--------
videos.csv    1行 = 1動画。先頭13列は従来のCSVと同じ名前・同じ順序:
                {head13}
              その後ろに追記列:
                video_id                 URL末尾の動画ID（19桁）。comments.csv との結合キー
                comments_fetched         取得できた本体コメント数（level 1）
                replies_fetched          取得できた返信数（level 2）
                comments_status          ok / empty(コメント0件) / blocked / skipped(取得対象外) / その他は取得側の生の値
                comments_total_reported  TikTok が申告した総数（返信込み。取得数ではない）
                comments_has_more        1 なら続きがある（上位リストを取り切って止めた場合も 1 になる）
                top_list_count           上位リスト（TikTok の関連度ランキング）に載っていた件数
                snapshot_at              コメント取得の時点（JSONL の更新時刻）
                run_id                   取り込み単位の ID。時系列比較はこれで区別する

comments.csv  1行 = 1コメント。返信も同じファイルに入る:
                comment_id            コメントID（19桁）。主キー
                video_id              動画ID（19桁）。videos.csv と結合する
                parent_comment_id     空 = 本体コメント。値あり = その本体への返信
                reply_to_comment_id   返信への返信のとき、直接の返信先。空なら本体宛
                level                 1 = 本体 / 2 = 返信
                text                  本文（画像コメントは空。image_url を見る）
                like_count            いいね数
                reply_count           返信の総数（TikTok 申告。取得数ではない）。返信行は空
                created_at            投稿時刻（UTC）
                user_id / user_unique_id / user_nickname   投稿者
                is_author             1 = 動画の投稿者本人のコメント
                is_pinned             1 = 投稿者が固定
                is_author_liked       1 = 投稿者がいいねした
                is_top_list           1 = 上位リストに載っていた / 0 = 尾部 / 空 = 不明（返信）
                language              言語コード
                image_url             画像コメントの画像URL（期限付き）
                source                main = 本体API / reply_api = 返信API / embedded = 本体レスポンスに同梱
                tiktok_status         TikTok の生の status（1 = 通常。他の値の意味は未確認）
                snapshot_at / run_id  videos.csv と同じ

meta.json     件数・入力ファイルの MD5・エスケープしたセル数。数字を確かめるときはここを見る
check.txt     書き出し直後に ZIP を読み直して行った検算の結果（全項目 OK であること）

Excel で開くときの注意（重要）
-----------------------------
1. 19桁の ID は Excel で開くと末尾が丸められます（7284621209680022785 → 7284621209680020000）。
   警告は出ません。丸まった ID では結合できなくなります。
   → 「データ」→「テキスト/CSV から」で取り込み、video_id / comment_id / parent_comment_id /
     reply_to_comment_id / user_id の列を「テキスト」に指定してください。
   → 正確な結合が必要なら CSV ではなく output/ugc.db（SQLite）を使ってください。

2. = + - @ で始まるセルには先頭に ' を付けてあります（Excel が数式として実行するのを防ぐため）。
   本文の先頭が「@ユーザー名」「-1」などのときに付きます。SQLite には付いていません。

3. videos.csv と comments.csv を結合した表で再生数などを SUM しないでください。
   コメント件数ぶん重複加算されます。集計は videos.csv 単体で行ってください。

4. 文字コードは UTF-8（BOM 付き）。Excel でそのまま開けます。改行入りの本文は引用符で囲ってあります。

結合のしかた
------------
comments.csv の video_id = videos.csv の video_id
comments.csv の parent_comment_id = comments.csv の comment_id（同じファイル内の自己参照）
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_build(a: argparse.Namespace) -> int:
    stats = ingest(a.db, a.project, a.videos_csv, a.comments_jsonl,
                   run_id=a.run_id, snapshot_at=a.snapshot_at, replace=a.replace)
    print(f"取り込み: run_id={stats['run_id']}")
    print(f"  動画: CSV {stats['videos']['in_csv']} / JSONL {stats['videos']['in_jsonl']} / "
          f"一致 {stats['videos']['matched']} / JSONLのみ {stats['videos']['only_in_jsonl']} → {stats['videos']['total_rows']}行")
    c = stats["comments"]
    print(f"  コメント: 本体 {c['by_source']['main']} / 返信API {c['by_source']['reply_api']} / "
          f"同梱 {c['by_source']['embedded']} → {c['total_rows']}行"
          f"（入力: {c['input_main']} / {c['input_reply_api']} / {c['input_embedded']}）")
    if any(c["skipped_duplicate_cid"].values()):
        print(f"  cid 重複で捨てた: {c['skipped_duplicate_cid']}")
    zip_path, _ = export_zip(a.db, stats["run_id"], out_dir=a.out_dir)
    print(f"書き出し: {zip_path}")
    good, res = check_zip(zip_path, comments_jsonl=a.comments_jsonl, videos_csv=a.videos_csv)
    attach_check(zip_path, res)
    print("検算:")
    print_report(res)
    return 0 if good else 1


def cmd_export(a: argparse.Namespace) -> int:
    zip_path, _ = export_zip(a.db, a.run_id, out_dir=a.out_dir)
    print(f"書き出し: {zip_path}")
    good, res = check_zip(zip_path)
    attach_check(zip_path, res)
    print_report(res)
    return 0 if good else 1


def cmd_check(a: argparse.Namespace) -> int:
    good, res = check_zip(a.zip, comments_jsonl=a.comments_jsonl, videos_csv=a.videos_csv)
    print_report(res)
    return 0 if good else 1


def cmd_runs(a: argparse.Namespace) -> int:
    conn = open_db(a.db)
    try:
        rows = conn.execute(
            "SELECT run_id, project_name, snapshot_at, video_count, comment_count, reply_count, ingested_at "
            "FROM runs ORDER BY ingested_at").fetchall()
    finally:
        conn.close()
    if not rows:
        print("取り込み済みの run はありません")
    for r in rows:
        print(f"{r[0]}\t{r[1]}\tsnapshot={r[2]}\tvideos={r[3]}\tcomments={r[4]}\treplies={r[5]}\tingested={r[6]}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite のパス（既定 {DEFAULT_DB}）")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="取り込み → ZIP 書き出し → 検算")
    b.add_argument("--project", required=True)
    b.add_argument("--videos-csv", help="既存スクレイパーの13列CSV。省略時は JSONL だけで作る（検証用）")
    b.add_argument("--comments-jsonl", required=True, help="spatest.py の出力")
    b.add_argument("--run-id")
    b.add_argument("--snapshot-at", help="取得時点（省略時は JSONL の更新時刻）")
    b.add_argument("--replace", action="store_true", help="同じ run_id があれば上書き")
    b.add_argument("--out-dir", default="output")
    b.set_defaults(func=cmd_build)

    e = sub.add_parser("export", help="取り込み済みの run を ZIP に書き出す")
    e.add_argument("--run-id", required=True)
    e.add_argument("--out-dir", default="output")
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("check", help="ZIP を読み直して検算する")
    c.add_argument("zip")
    c.add_argument("--comments-jsonl")
    c.add_argument("--videos-csv")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("runs", help="取り込み済みの run を一覧")
    r.set_defaults(func=cmd_runs)

    a = p.parse_args(argv)
    try:
        return a.func(a)
    except StoreError as ex:
        print(f"エラー: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
