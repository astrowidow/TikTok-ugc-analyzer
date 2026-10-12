"""
界隈の掘り下げ: 完成したレポートの、ある界隈のコメントを取り足す（2026-10-06 ユーザー）。docs/DEEPEN_COMMUNITY.md

- plan(): 取る動画を決める（AI にも TikTok にも触らない）。2026-10-12 から時間では切らずに全部取り、見込みを伝える
- request(): analysis.json の "deepen" に積む（道具 deepen が呼ぶ。取得の係が拾う）
- Run: 取得の係（acquire/worker.py）が取る。ロック・設定・Chrome・要求の間隔は本線（acquire/pipeline.py の Run）のものをそのまま使う
- merge(): 取った行を原本（raw/comments.jsonl）に足し、derived/comments を作り直す

取る動画（ユーザーの答え Q1・Q2）:
  新しく取る … その界隈のラベルがあり、コメントが無い動画。再生の下限はその分析の週ごとの下限（min_plays_weekly）
  続きを取る … その界隈でコメントはあるが、上限40件で止まり、上位リストがまだ続く動画。上限120件で取り直す
  新しく取る動画を再生の多い順に、続きを再生の多い順に。前は目安25分に詰めていたが、2026-10-12 ユーザー
  「時間がかかるからとか言って勝手に切ってる…一回警告すりゃあいい話」で、時間では切らない（budget_min 0）

本線の analysis/pool.py・acquire/pipeline.py の DEFAULTS は変えない（コメント取得の戦略のセッションが触る）。spatest も引数だけで使う。

0.8.1（docs/PLAN_0_8_1.md 第1章・2-3・2-4）: 掘り下げの枠（analysis.json の "deepen"）を「変更の回」に広げた。回の種類は kind:
  deepen   … 界隈1つの掘り下げ（今まで）
  recut    … 界隈の切り直しの取り足し（acquire/recut.py）
  videos   … 動画ごとの取り足し（plan_videos。利用者が名指しした動画）・全体の取り足し（plan_all。community="all"）
  add      … 動画を足す（request_add。一覧に足す → 属性 → 通し番号を変えずに作り直す → コメント）
  exclude・include … 動画を外す・戻す（取得なし。すぐ済む。印は道具が analysis.json の excluded_videos に書く）
どの取り足しも、外した動画（excluded_videos）は候補から外す。1本の上限は per_video（件数か "all"＝その動画のコメント数まで）で変えられる。
見込みは件数に比例（40件で new_min 分。120件で3分）

  python -m acquire.deepen plan <分析フォルダ> [<界隈>|all] [--minutes 25] [--per-video N|all] [--videos ID,URL,seq…]   … 何を取るかだけ出す
"""
import csv
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from acquire import pipeline  # noqa: E402

# 時間の見込み（分）と上限（件）
EST = {
    # 1回の取得の目安（分）。0 は切らない（2026-10-12 から。前は 25: 2026-10-06 ユーザー「25分程度の取得時間に収まるように配分」）
    "budget_min": 0,
    # 2026-10-06 の実機の試験（シルエットの写し・dancer、docs/DEEPEN_COMMUNITY.md 10 章）で直した:
    # グリッドが 2141本に増えていて、見つからない1本を探してスクロールの上限（150回×2秒）まで回し5.5分。新しく取る7本で6.5分、続き2本で各2.5〜2.9分
    # 2026-10-06 のきゃわの事例（本物の分析、楽曲ページ2つ）で直した: 6分・1.3分では見込み 23.7分 → 実際 7分、切り直しは 14.2分 → 1分。
    # ページを開いて対象を探すのは 3〜30秒（対象が全部グリッドにあった）、上限40件の動画は 9本で 6.5分（1本 約0.7分）。
    # 見つからない動画があると、そのページだけ上限まで探して 5〜6分かかる（シルエットの試験）。そのときは打ち切り（見込み＋10分）で止まる
    "page_min": 1.5,           # 楽曲ページ1つあたりの準備: Chrome・ページを開く・グリッドで対象を探す
    "new_min": 1.0,            # 上限40件の動画1本（実機 0.7〜0.9分。毎分3回の実走では40本で62分＝1.55分）。1本の見込みは件数に比例（video_min）
    "more_min": 3.0,           # 上限120件で取り直す1本（実機 2.5〜2.9分）。比例の式でも 120件＝3分（0.8.1 から video_min で数える）
    "new_cap": 40, "more_cap": 120,
    "deadline_extra_min": 10,  # 打ち切り = 見込み＋これ
    # 0.8.1: 利用者が名指しした動画は、前に一覧で見つからなかったものも探し直す。見つからない動画があるとそのページを上限まで送る
    # （シルエットの試験で 5〜6分。上の page_min の説明）ので、そのページに足す
    "search_min": 5.5,
    # 0.8.1 動画を足す（request_add）: 1本の属性とサムネ（動画ページを開いて読む。enrich_sleep 2秒＋読み込み）と、
    # 集計と AI の入力の作り直し（サムネの一覧を描き直す）
    "enrich_min": 0.2, "derive_min": 1.0,
}
GONE = ("no_video", "no_comments", "open_failed")   # 取りに行っても取れない（削除・コメント無し・開けない）
KINDS = ("deepen", "recut", "videos", "add", "exclude", "include")   # 回の種類（冒頭の説明）
NO_FETCH = ("exclude", "include")                                    # 取得の無い回（積んだらすぐ済む）
# 名指しの動画を取らない理由（plan_videos の skipped のキー → 利用者に伝える言葉）
SKIP_WHY = {
    "excluded": "外した動画（戻すまで取らない）",
    "not_in_analysis": "この分析に無い動画（足すときは動画を足す頼みで）",
    "gone": "取りに行っても取れない（削除・コメントが無い・開けない）",
    "all_fetched": "コメントはもう全部取ってある（続きが無い）",
    "top_list_done": "上位のコメントは取り切ってある（続きを取っても増えない）",
    "enough": "頼まれた件数はもう取ってある",
    "no_gain": "前の取り足しで同じ件数まで取り直したが増えなかった",
    "no_page": "コメントを取る楽曲ページが分からない（音源が読めていない）",
}


def video_min(cap) -> float:
    """動画1本の見込み（分）: 上限の件数に比例（40件で new_min。120件で3分＝more_min と同じ）"""
    return EST["new_min"] * int(cap) / EST["new_cap"]


def estimate(targets: list) -> float:
    """取り足しの見込み（分）: 楽曲ページ1つにつき準備 page_min（前に見つからなかった動画を探し直すページは search_min も）＋動画ごとの video_min"""
    pages = {_page_key(x) for x in targets}
    search = {_page_key(x) for x in targets if x.get("retry")}
    return round(len(pages) * EST["page_min"] + len(search) * EST["search_min"] + sum(video_min(x["cap"]) for x in targets), 1)


def _page_key(x: dict):
    """取るページ: 台帳の楽曲ページの番号（1〜）、台帳に無い動画はその音源の楽曲ページの URL"""
    return x.get("music_url") or int(x.get("page") or 1)


def per_video_of(v):
    """per_video の形を確かめる: None（今の上限: 新しく40・続き120）・件数（1以上）・"all"（その動画のコメント数まで）"""
    if v is None or v == "all":
        return v
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError('per_video は件数（1以上）か "all" にしてください')
    return v


def cap_of(per_video, r: dict, kind: str) -> int:
    """1本の上限（件）。per_video が無ければ今の上限（新しく40・続き120）、数ならその件数、"all" ならその動画のコメント数（40未満なら40）。
    勝手な上限は付けない（2026-10-12 ユーザー）"""
    if per_video is None:
        return EST["new_cap"] if kind == "new" else EST["more_cap"]
    if per_video == "all":
        return max(EST["new_cap"], int(r.get("comments") or 0))
    return int(per_video)


# ---------------------------------------------------------------------------
# 読む
# ---------------------------------------------------------------------------
def _jsonl(p: Path) -> list:
    out = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _records(d: Path, keep_excluded: bool = False) -> dict:
    """動画 ID → records.jsonl の行。利用者が外した動画（excluded）は除く（掘り下げ・切り直し・動画ごと・全体の取り足しの候補に出さない）"""
    out = {str(r["video_id"]): r for r in _jsonl(Path(d) / "derived" / "llm_input" / "records.jsonl")}
    if not keep_excluded:
        for v in excluded(d):
            out.pop(v, None)
    return out


def excluded(d: Path) -> set:
    """利用者が外した動画の ID（analysis.json の excluded_videos: [{video_id, url, seq, at, why}]。道具 videos の exclude が書く）"""
    m = pipeline.read_json(Path(d) / "analysis.json", {}) or {}
    return {str(x["video_id"]) for x in m.get("excluded_videos") or [] if isinstance(x, dict) and x.get("video_id")}


# 動画 ID → (音源の id, 題)。台帳に無い動画のコメントを、その音源の楽曲ページから取るのに使う
_music = pipeline.music_of_videos


def _labels(d: Path) -> dict:
    p = d / "outputs" / "labels.tsv"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {int(r["seq"]): r for r in csv.DictReader(f, delimiter="\t")}


def _rows(p: Path) -> dict:
    """動画 ID → 最後の行（後の行が勝つ）"""
    return {str(r.get("video_id")): r for r in _jsonl(p)}


def _ok_rows(p: Path) -> dict:
    """動画 ID → 最後の ok の行（コメントあり）。flow_w1.fetched と同じ見方"""
    return {str(r["video_id"]): r for r in _jsonl(p) if r.get("status") == "ok" and r.get("comments")}


def _sources(d: Path) -> dict:
    """動画 ID → 見つけた楽曲ページ（1〜。0 は台帳に無い、利用者が足した動画）。pipeline.Run.link_sources と同じ"""
    return {str(g.get("video_id")): pipeline.source_of(g) for g in _jsonl(d / "raw" / "grid_links.jsonl")}


def where(vid: str, src: dict, urls: list, music: dict) -> tuple:
    """コメントを取る楽曲ページ (番号, URL)。台帳の動画は見つけたページ (1〜, None)。
    台帳に無い動画（source 0。利用者が足した動画）は、その動画の音源のページ: 取るページのどれかならその番号、
    ほかの音源なら (0, その音源の楽曲ページの URL)。音源が分からなければ (0, None)＝取れない"""
    k = src.get(vid, 1)
    if k >= 1:
        return k, None
    mid, title = music.get(vid) or (None, None)
    if not mid:
        return 0, None
    ids = [pipeline.music_id_of(u) for u in urls]
    if mid in ids:
        return ids.index(mid) + 1, None
    return 0, pipeline.music_url_of(mid, title)


def min_plays_of(d: Path) -> int:
    m = pipeline.read_json(d / "analysis.json", {}) or {}
    v = (m.get("acquisition_settings") or {}).get("min_plays_weekly")
    return int(v if v is not None else pipeline.DEFAULTS["min_plays_weekly"])


def _can_continue(r: dict) -> bool:
    """上限で止まり、上位リストがまだ続いていた動画（spatest は「上位リストを取り切った」でも止まる。そのときは続きを取っても増えない）"""
    n = len(r.get("comments") or [])
    return bool(r.get("has_more")) and int(r.get("cap") or 40) < EST["more_cap"] and int(r.get("top_list") or 0) >= n > 0


def unreachable(d: Path) -> set:
    """前の取得で楽曲ページの一覧に見つからなかった動画（本線・掘り下げ・切り直しの要約と差し替えの記録から）。探し直すと数分かかるので外す。
    掘り下げ（candidates）と切り直し（acquire/recut.py の plan）で同じ範囲を外す"""
    out = set()
    for p in sorted((Path(d) / "fetch_log").glob("*summary*.json")):
        out |= {str(v) for v in (pipeline.read_json(p, {}) or {}).get("missing") or []}
    for p in sorted((Path(d) / "fetch_log").glob("substitutions*.tsv")):
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
        if not lines:
            continue
        rows = csv.DictReader(lines, delimiter="\t")
        out |= {str(r.get("pool_video")) for r in rows if r.get("pool_video")}
    return out


# ---------------------------------------------------------------------------
# 決める
# ---------------------------------------------------------------------------
def _more_left(r: dict, cap: int) -> bool:
    """続きを取れば増えるか（per_video を指定したとき）: 上位リストがまだ続いていて、今の件数が上限より少ない"""
    n = len(r.get("comments") or [])
    return bool(r.get("has_more")) and int(r.get("top_list") or 0) >= n > 0 and n < cap


class _Look:
    """候補を決めるのに読むもの（AI の入力・取得の原本・取り足しの記録・一覧・ラベル）を1回だけ読む"""

    def __init__(self, d: Path, min_plays: int | None = None):
        d = Path(d)
        self.d = d
        self.all = _records(d, keep_excluded=True)
        self.ex = excluded(d)
        self.recs = {v: r for v, r in self.all.items() if v not in self.ex}
        self.by_seq = {int(r["seq"]): r for r in self.recs.values()}
        self.all_by_seq = {int(r["seq"]): r for r in self.all.values()}
        self.ok = _ok_rows(d / "raw" / "comments.jsonl")
        self.last = _rows(d / "raw" / "comments.jsonl")
        self.tried = {}
        for p in sorted((d / "raw" / "deepen").glob("r*.jsonl")) if (d / "raw" / "deepen").exists() else []:
            self.tried.update(_rows(p))
        self.lost = unreachable(d)   # 前の取得（本線・取り足し）で一覧に見つからなかった動画
        self.src = _sources(d)
        self.urls = pipeline.music_urls_of(pipeline.read_json(d / "analysis.json", {}) or {})
        self.floor = min_plays_of(d) if min_plays is None else int(min_plays)
        self.labels = _labels(d)
        self._music = None

    def item(self, r: dict) -> dict | None:
        """target の共通の項目。台帳に無い動画は、その動画の音源の楽曲ページ（music_url。page 0）。取るページが分からなければ None"""
        vid = str(r["video_id"])
        k = self.src.get(vid, 1)
        if k >= 1:
            page, url = k, None
        else:
            if self._music is None:
                self._music = _music(self.d)
            page, url = where(vid, self.src, self.urls, self._music)
            if not page and not url:
                return None
        x = {"video_id": vid, "seq": int(r["seq"]), "plays": int(r.get("plays") or 0), "date": r.get("date"), "week": r.get("week"),
             "page": page}
        if url:
            x["music_url"] = url
        return x


def _bump(skipped: dict, key: str, n: int = 1) -> None:
    skipped[key] = skipped.get(key, 0) + n


def candidates(d: Path, community: str, min_plays: int | None = None, per_video=None, look: _Look | None = None) -> dict:
    """界隈の「新しく取る」「続きを取る」候補（再生の多い順）と、外した理由ごとの本数。
    per_video（0.8.1）: None なら今の上限（新しく40・続き120）。件数・"all" なら1本の上限をそれに（cap_of）"""
    lk = look or _Look(d, min_plays)
    per_video = per_video_of(per_video)
    floor = lk.floor if min_plays is None else int(min_plays)
    new, more = [], []
    skipped = {"below_min_plays": 0, "gone": 0}
    for s, lab in lk.labels.items():
        if lab.get("community") != community:
            continue
        if s not in lk.by_seq:
            if s in lk.all_by_seq:   # 利用者が外した動画
                _bump(skipped, "excluded")
            continue
        r = lk.by_seq[s]
        vid = str(r["video_id"])
        plays = int(r.get("plays") or 0)
        t = lk.tried.get(vid) or {}
        if vid in lk.ok:
            # 続きも、前の掘り下げで「動画が無い・コメントが無い・開けない」だった動画、一覧に見つからなかった動画、
            # 上限120件で取り直しても増えなかった動画（ok の行が原本に足されず、上限40件の行のまま残る）は外す
            o = lk.ok[vid]
            cap = cap_of(per_video, r, "more")
            cont = _can_continue(o) if per_video is None else _more_left(o, cap)
            if (t.get("status") in GONE or vid in lk.lost
                    or (t.get("status") == "ok" and int(t.get("cap") or 0) >= cap)):
                if cont:
                    skipped["gone"] += 1
                continue
            if cont:
                x = lk.item(r)
                if x is None:
                    _bump(skipped, "no_page")
                    continue
                more.append({**x, "cap": cap, "kind": "more", "had": len(o.get("comments") or [])})
            continue
        if (not r.get("enriched") or r.get("comments") == 0 or (lk.last.get(vid) or {}).get("status") in GONE
                or t.get("status") in GONE or vid in lk.lost):
            skipped["gone"] += 1
            continue
        if plays < floor:
            skipped["below_min_plays"] += 1
            continue
        x = lk.item(r)
        if x is None:
            _bump(skipped, "no_page")
            continue
        new.append({**x, "cap": cap_of(per_video, r, "new"), "kind": "new"})
    key = lambda x: (-x["plays"], x["seq"])   # noqa: E731
    return {"new": sorted(new, key=key), "more": sorted(more, key=key), "skipped": skipped, "min_plays": floor}


def _pages(targets: list) -> dict:
    """plan の pages（台帳の楽曲ページの番号）と music_urls（台帳に無い動画の音源の楽曲ページ）"""
    return {"pages": sorted({int(x["page"]) for x in targets if not x.get("music_url")}),
            "music_urls": sorted({x["music_url"] for x in targets if x.get("music_url")})}


def plan(d: Path, community: str, minutes: float | None = None, min_plays: int | None = None, per_video=None) -> dict:
    """取る動画を決めて、目安の時間に収まるように配る。新しく取る動画を先に、残った時間で続きを（0 は切らない。今はこれ）"""
    budget = float(minutes if minutes is not None else EST["budget_min"])
    per_video = per_video_of(per_video)
    c = candidates(Path(d), community, min_plays, per_video)
    pages, used, take = set(), 0.0, []
    for kind in ("new", "more"):
        for x in c[kind]:
            add = video_min(x["cap"]) + (EST["page_min"] if _page_key(x) not in pages else 0.0)
            if budget <= 0 or used + add <= budget:
                take.append(x)
                pages.add(_page_key(x))
                used += add
    new = [x for x in take if x["kind"] == "new"]
    more = [x for x in take if x["kind"] == "more"]
    return {"community": community, "minutes": budget, "min_plays": c["min_plays"], "est_min": round(used, 1),
            "targets": take, "n_new": len(new), "n_more": len(more),
            "left_new": len(c["new"]) - len(new), "left_more": len(c["more"]) - len(more),
            "skipped": c["skipped"], **_pages(take), "per_video": per_video}


def _plan_of(community: str, targets: list, skipped: dict, floor: int, per_video, **extra) -> dict:
    """plan と同じ形（時間では切らないので left_* は 0）"""
    return {"community": community, "minutes": 0.0, "min_plays": floor, "est_min": estimate(targets), "targets": targets,
            "n_new": sum(1 for x in targets if x["kind"] == "new"), "n_more": sum(1 for x in targets if x["kind"] == "more"),
            "left_new": 0, "left_more": 0, "skipped": skipped, **_pages(targets), "per_video": per_video, **extra}


video_id_of = pipeline.video_id_of   # 動画の URL か ID の文字列 → 動画の ID（分からなければ None）


def _resolve(lk: _Look, items) -> list:
    """名指しの動画（video_id・URL・seq・{video_id|url|seq}）→ [(渡された形, video_id か None)]。重ねない"""
    out, seen = [], set()
    for it in items or []:
        given = it
        if isinstance(it, dict):
            it = it.get("video_id") or it.get("url") or it.get("seq")
        vid = None
        if isinstance(it, int) and not isinstance(it, bool):
            vid = str(lk.all_by_seq[it]["video_id"]) if it in lk.all_by_seq else None
        elif it is not None:
            s = str(it).strip()
            m = re.fullmatch(r"(?i)(?:seq\s*)?(\d{1,11})", s)
            if s in lk.all:
                vid = s
            elif m:
                k = int(m.group(1))
                vid = str(lk.all_by_seq[k]["video_id"]) if k in lk.all_by_seq else None
            else:
                vid = video_id_of(s)
        key = vid or f"?{given}"
        if key in seen:
            continue
        seen.add(key)
        out.append((given, vid))
    return out


def plan_videos(d: Path, video_ids, per_video=None) -> dict:
    """利用者が名指しした動画のコメントを取る計画（0.8.1。道具 deepen の videos）。形は plan と同じ（community は "videos"）。
    コメントがまだ無い動画は新しく（kind new）、あれば続き（kind more）。名指しなので再生の下限・ラベルの有無では外さず、
    前に一覧で見つからなかった動画も探し直す（retry。見込みに search_min）。台帳に無い動画（利用者が足した動画）はその音源の楽曲ページから（music_url・page 0）。
    取らない動画は skipped（理由ごとの本数。SKIP_WHY）と skipped_videos（[{given, video_id, seq, reason, why}]）に。
    video_ids は動画の ID・URL・seq（数か "seq 12"）のどれでも"""
    d = Path(d)
    per_video = per_video_of(per_video)
    lk = _Look(d)
    new, more, skipped, skipped_videos = [], [], {}, []

    def skip(key, given, vid, r=None):
        _bump(skipped, key)
        skipped_videos.append({"given": given, "video_id": vid, "seq": int(r["seq"]) if r else None, "reason": key,
                               "why": SKIP_WHY[key]})

    for given, vid in _resolve(lk, video_ids):
        if vid in lk.ex:
            skip("excluded", given, vid, lk.all.get(vid))
            continue
        r = lk.recs.get(vid) if vid else None
        if r is None:
            skip("not_in_analysis", given, vid)
            continue
        t = lk.tried.get(vid) or {}
        o = lk.ok.get(vid)
        if t.get("status") in GONE or (o is None and (not r.get("enriched") or r.get("comments") == 0
                                                      or (lk.last.get(vid) or {}).get("status") in GONE)):
            skip("gone", given, vid, r)
            continue
        x = lk.item(r)
        if x is None:
            skip("no_page", given, vid, r)
            continue
        lab = lk.labels.get(int(r["seq"])) or {}
        if lab.get("community"):
            x["community"] = lab["community"]
        if vid in lk.lost:
            x["retry"] = True
        if o is None:
            new.append({**x, "cap": cap_of(per_video, r, "new"), "kind": "new"})
            continue
        n = len(o.get("comments") or [])
        cap = cap_of(per_video, r, "more")
        if not o.get("has_more"):
            skip("all_fetched", given, vid, r)
        elif n >= cap:
            skip("enough", given, vid, r)
        elif int(o.get("top_list") or 0) < n:
            skip("top_list_done", given, vid, r)
        elif t.get("status") == "ok" and int(t.get("cap") or 0) >= cap:
            skip("no_gain", given, vid, r)
        else:
            more.append({**x, "cap": cap, "kind": "more", "had": n})
    key = lambda x: (-x["plays"], x["seq"])   # noqa: E731
    return _plan_of("videos", sorted(new, key=key) + sorted(more, key=key), skipped, lk.floor, per_video,
                    skipped_videos=skipped_videos)


def plan_all(d: Path, per_video=None) -> dict:
    """全部の界隈の取り足し（0.8.1。道具 deepen の community="all"）: 界隈ごとの候補（candidates）を合わせ、重なりを除く。
    unknown（分けられなかった動画）は入れない（_main・切り直しと同じ）。target に界隈（community）を付ける（原本の印が界隈ごとになる）"""
    d = Path(d)
    per_video = per_video_of(per_video)
    lk = _Look(d)
    comms = sorted({str(lab.get("community")) for lab in lk.labels.values() if lab.get("community")} - {"unknown"})
    new, more, seen, skipped, by_c = [], [], set(), {}, {}
    for k in comms:
        c = candidates(d, k, per_video=per_video, look=lk)
        for kind, out in (("new", new), ("more", more)):
            for x in c[kind]:
                if x["video_id"] in seen:
                    continue
                seen.add(x["video_id"])
                out.append({**x, "community": k})
        for key, v in c["skipped"].items():
            _bump(skipped, key, v)
        by_c[k] = {"new": len(c["new"]), "more": len(c["more"])}
    key = lambda x: (-x["plays"], x["seq"])   # noqa: E731
    return _plan_of("all", sorted(new, key=key) + sorted(more, key=key), skipped, lk.floor, per_video, communities=by_c)


def _check_free(m: dict) -> None:
    """今の回が順番待ち・取得中なら、新しい回を積まない（上書きすると、係が取っている回の状態を新しい回に書いてしまう）。
    途中の頼みは呼ぶ側で積む（proto_runner の state/pending.json）"""
    dp = m.get("deepen") or {}
    if dp.get("status") in ("queued", "running"):
        raise RuntimeError(f"取り足し（{dp.get('round')}回目・{dp.get('kind') or 'deepen'}）が順番待ち・取得中です。終わってから積んでください")


def _push(d: Path, job: dict) -> dict:
    """analysis.json の "deepen" に回を積む（前の回は deepen_history へ。回の番号は掘り下げ・切り直し・動画の回で共通）"""
    p = Path(d) / "analysis.json"
    m = pipeline.read_json(p, {}) or {}
    _check_free(m)
    hist = m.setdefault("deepen_history", [])
    if m.get("deepen"):
        hist.append(m["deepen"])
    job = {"round": len(hist) + 1, **job}
    m["deepen"] = job
    pipeline.write_json(p, m)
    return job


def request(d: Path, community: str, instruction: str, pl: dict, kind: str = "deepen", **extra) -> dict:
    """analysis.json の "deepen" に今の回を積む（前の回は deepen_history へ）。取る動画が無ければ取得は済んだことにする。
    kind は回の種類（KINDS。recut は acquire/recut.py の request、add は request_add）。extra（video_ids・per_video など）は回にそのまま書く。
    取得の無い回（exclude・include）は、すぐ済んだことにする（result {"skipped": "取得なし"}）"""
    if kind not in KINDS:
        raise ValueError(f"回の種類が違います: {kind}")
    pl = dict(pl or {})
    targets = list(pl.get("targets") or [])
    now = pipeline.now()
    job = {"kind": kind, "community": community, "instruction": instruction, "requested_at": now,
           "plan": {k: v for k, v in pl.items() if k != "targets"}, "targets": targets, "est_min": pl.get("est_min") or 0}
    job.update({k: v for k, v in extra.items() if k not in job and k != "round"})
    if targets and kind not in NO_FETCH:
        job.update({"status": "queued", "queued_at": now})
    elif kind in NO_FETCH:
        job.update({"status": "done", "finished_at": now, "result": {"skipped": "取得なし"}})
    else:
        res = {"skipped": "足す動画が分からない（動画の URL か ID が要る）" if kind == "add" else "取り足せる動画が無い"}
        if pl.get("skipped"):
            res["reasons"] = pl["skipped"]
        if pl.get("skipped_videos"):
            res["videos"] = pl["skipped_videos"]
        job.update({"status": "done", "finished_at": now, "result": res})
    return _push(d, job)


def request_add(d: Path, targets: list, instruction: str, per_video=None) -> dict:
    """動画を足す回（kind "add"。0.8.1 の道具 videos の add で、取得が済んだあと）を積む。targets は [{video_id, url}]（URL だけでも）。
    係（Run._run_add）が 一覧に足す → 属性とサムネ → 通し番号を変えずに作り直す → 足した動画のコメント → 原本に足す。
    見込みは 属性（1本 enrich_min）＋作り直し（derive_min）＋コメント（1本 video_min・ページごとの準備。台帳に無い動画は1本1ページと見る）"""
    d = Path(d)
    per_video = per_video_of(per_video)
    led = {str(r.get("video_id")): r for r in _jsonl(d / "raw" / "ledger.jsonl")}
    ts, seen = [], set()
    for t in targets or []:
        t = t if isinstance(t, dict) else {"url": str(t)}
        vid = str(t.get("video_id") or "").strip() or video_id_of(t.get("url"))
        if not vid or vid in seen:
            continue
        seen.add(vid)
        ts.append({"video_id": vid, "url": pipeline.video_url(t.get("url"), vid)})
    pages = {int((led.get(t["video_id"]) or {}).get("source") or 0) or t["video_id"] for t in ts}
    cap = cap_of(per_video, {}, "new")
    est = round(len(ts) * (EST["enrich_min"] + video_min(cap)) + len(pages) * EST["page_min"] + (EST["derive_min"] if ts else 0), 1)
    pl = {"targets": ts, "est_min": est, "n_add": len(ts), "n_new": len(ts), "n_more": 0,
          "in_ledger": sum(1 for t in ts if t["video_id"] in led), "per_video": per_video}
    return request(d, "add", instruction, pl, kind="add", per_video=per_video)


# ---------------------------------------------------------------------------
# 取る（取得の係）
# ---------------------------------------------------------------------------
def queued(m: dict) -> bool:
    """係が拾う掘り下げの取得か（順番待ち、または途中で係が止まったもの）"""
    import tiktok_lock
    dp = m.get("deepen") or {}
    st = dp.get("status")
    return st == "queued" or (st == "running" and not tiktok_lock.pid_alive(dp.get("pid") or -1))


def what_of(job: dict) -> str:
    """回の呼び名（係のログ・ロックの名前）"""
    kind = job.get("kind") or "deepen"
    if kind == "videos":
        return "全体のコメントの取り足し" if job.get("community") == "all" else "動画のコメントの取り足し"
    return {"recut": "界隈の切り直し", "add": "動画の追加", "exclude": "動画を外す", "include": "動画を戻す"}.get(kind, "界隈の掘り下げ")


def brakes(cap: int) -> list:
    """上限が120件を超える取り足し（per_video の件数・"all"）では、spatest の1本あたりの歯止め（スクロールの段数・要求の回数。
    既定は上限120件までの取り方で決めた数）を上限に比例して広げる（歯止めが先に効いて、頼まれた件数の手前で止まらないように。
    勝手な上限は付けない）。120件までは今までどおり（渡さない）。上位リストを取り切ったら止まる作りはそのまま"""
    if int(cap) <= EST["more_cap"]:
        return []
    from acquire import spatest
    ap = spatest.build_parser()
    k = int(cap) / EST["more_cap"]
    return ["--max-steps", str(math.ceil(ap.get_default("max_steps") * k)),
            "--max-calls-per-video", str(math.ceil(ap.get_default("max_calls_per_video") * k))]


class Run:
    """掘り下げ1回ぶんの取得。本線の pipeline.Run（ロック・設定・ログ・楽曲ページ）を借りる"""

    def __init__(self, analysis_id: str):
        self.base = pipeline.Run(analysis_id)
        self.id = analysis_id
        self.dir = self.base.dir

    def job(self) -> dict:
        return (self.base.meta.get("deepen") or {})

    def set(self, **kv) -> None:
        self.base.update(lambda m: m.setdefault("deepen", {}).update(kv))

    def p(self, *parts) -> Path:
        return self.base.p(*parts)

    def run(self) -> str:
        job = self.job()
        n = job["round"]
        # recut＝界隈の切り直しの取り足し（acquire/recut.py）・videos＝動画ごと・全体の取り足し（0.8.1）。取り方は同じ。add は _run_add
        kind = job.get("kind") or "deepen"
        if kind == "add":
            return self._run_add(job)
        what = what_of(job)
        self.set(status="running", pid=os.getpid(), started_at=job.get("started_at") or pipeline.now())
        log = self.base.log
        log(f"=== {what}（{n}回目・{job['community']}）の取得を始めます: 新しく{job['plan'].get('n_new')}本・"
            f"続き{job['plan'].get('n_more')}本（見込み{job.get('est_min')}分）")
        out = self.p("raw", "deepen", f"r{n}.jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        error, blocked = None, False
        try:
            blocked = self._fetch(job, out)
        except pipeline.StepError as e:
            error = str(e)
            log(f"!! {what}の取得を止めました: {e}")
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log(f"!! {what}の取得が例外で止まりました: {error}\n{traceback.format_exc()}")
        finally:
            self.base.unlock()
        # 止まっても、取れた分は足す（待って取り直すことはしない。短い仕事なので）
        try:
            res = merge(self.dir, n, job["community"], kind,
                        {str(x["video_id"]): x["community"] for x in job.get("targets") or [] if x.get("community")})
        except Exception as e:
            log(f"!! 取った行を原本に足せませんでした: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            self.set(status="failed", error=f"原本に足せなかった（{type(e).__name__}）", failed_at=pipeline.now())
            return "failed"
        res.update({"blocked": blocked, "error": error})
        self.set(status="done", finished_at=pipeline.now(), result=res)
        log(f"=== {what}の取得が終わりました: {json.dumps(res, ensure_ascii=False)}")
        return "done"

    def _run_add(self, job: dict) -> str:
        """動画を足す回（kind "add"。0.8.1）: (1) 一覧（raw/grid_links.jsonl）にまだ無い動画を足す（台帳にあれば見つけたページ・再生数はそこから、
        無ければ source 0）(2) 属性とサムネ（本線の属性の段。取得済みは飛ばす）(3) 通し番号を変えずに集計と AI の入力を作り直す
        （pipeline.Run.derive_keep_seqs。前の derived/ は derived/_before_add_r<回>/ に控える）(4) 足した動画のコメント（plan_videos と同じ取り方。
        台帳にあればそのページ、無ければその動画の音源のページ。見つからなければ取らずに記録）→ 原本に足して derived/comments を作り直す。
        係が途中で止まっても、もう一度回せば済んだところは飛ばす（一覧は足してあるものを足さない・属性は取得済みを飛ばす・控えは上書きしない・
        コメントは --resume）"""
        n = job["round"]
        log = self.base.log
        self.set(status="running", pid=os.getpid(), started_at=job.get("started_at") or pipeline.now())
        ts = [t for t in job.get("targets") or [] if t.get("video_id")]
        log(f"=== 動画の追加（{n}回目）を始めます: {len(ts)}本（見込み{job.get('est_min')}分）")
        out = self.p("raw", "deepen", f"r{n}.jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        res = {}
        try:
            res["links"] = self.base.add_user_videos(ts)
            log(f"    一覧に足しました: {json.dumps(res['links'], ensure_ascii=False)}")
            have = {str(e.get("video_id")) for e in _jsonl(self.p("raw", "enriched.jsonl"))}
            if any(t["video_id"] not in have for t in ts):
                res["enrich"] = self.base.step_enrich()   # 取得済みは飛ばす（続きから）
            else:   # 足す動画の属性はもう取ってある（続きから回したとき）。Chrome を開かない
                res["enrich"] = {"skipped": len(ts), "list_rows": self.base._write_list_csv()}
            self.base.unlock()
            res["derive"] = self.base.derive_keep_seqs(f"add_r{n}")
        except Exception as e:
            error = str(e) if isinstance(e, pipeline.StepError) else f"{type(e).__name__}: {e}"
            log(f"!! 動画の追加が止まりました（一覧・属性・作り直し）: {error}\n{traceback.format_exc()}")
            self.base.unlock()
            self.set(status="failed", error=error, failed_at=pipeline.now(), result={**res, "error": error})
            return "failed"
        # (4) 足した動画のうち、まだコメントの無いものだけ取る（もう分析にあってコメントもある動画は取り直さない）
        pl = plan_videos(self.dir, [t["video_id"] for t in ts], job.get("per_video"))
        targets = [x for x in pl["targets"] if x["kind"] == "new"]
        cjob = {**job, "targets": targets, "est_min": estimate(targets), "community": "add"}
        self.set(comment_targets=targets, comment_skipped=pl.get("skipped_videos") or [])
        error, blocked = None, False
        if targets:
            log(f"    足した動画のコメントを取ります: {len(targets)}本（見込み{cjob['est_min']}分）")
            try:
                blocked = self._fetch(cjob, out)
            except pipeline.StepError as e:
                error = str(e)
                log(f"!! 足した動画のコメントの取得を止めました: {e}")
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log(f"!! 足した動画のコメントの取得が例外で止まりました: {error}\n{traceback.format_exc()}")
            finally:
                self.base.unlock()
        try:
            res.update(merge(self.dir, n, "add", "add",
                             {x["video_id"]: x["community"] for x in targets if x.get("community")}))
        except Exception as e:
            log(f"!! 取った行を原本に足せませんでした: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            self.set(status="failed", error=f"原本に足せなかった（{type(e).__name__}）", failed_at=pipeline.now())
            return "failed"
        res.update(self._add_result(ts, targets, pl, n))
        res.update({"blocked": blocked, "error": error})
        self.set(status="done", finished_at=pipeline.now(), result=res)
        log(f"=== 動画の追加が終わりました: {json.dumps(res, ensure_ascii=False)[:2000]}")
        return "done"

    def _add_result(self, ts: list, targets: list, pl: dict, n: int) -> dict:
        """動画を足した回の結果: added（足した動画ごとの番号・属性が取れたか・コメントの件数・取れなかった理由）と not_found（動画のページが読めなかった）"""
        recs = _records(self.dir, keep_excluded=True)
        ok = _ok_rows(self.p("raw", "comments.jsonl"))
        tried = _rows(self.p("raw", "deepen", f"r{n}.jsonl"))
        missing = set()
        for p in sorted(self.p("fetch_log").glob(f"deepen_r{n}_summary_*.json")):
            missing |= {str(v) for v in (pipeline.read_json(p, {}) or {}).get("missing") or []}
        skipped = {str(x.get("video_id")): x for x in pl.get("skipped_videos") or []}
        aimed = {x["video_id"] for x in targets}
        added, not_found = [], []
        for t in ts:
            vid = str(t["video_id"])
            r = recs.get(vid) or {}
            item = {"video_id": vid, "seq": r.get("seq"), "enriched": bool(r.get("enriched")),
                    "comments": len((ok.get(vid) or {}).get("comments") or [])}
            if not item["comments"]:
                if vid in missing:
                    item["comments_why"] = "楽曲ページの一覧に見つからなかった"
                elif vid in skipped:
                    item["comments_why"] = skipped[vid]["why"]
                elif (tried.get(vid) or {}).get("status") in GONE:
                    item["comments_why"] = "取りに行っても取れない（削除・コメントが無い・開けない）"
                elif vid in aimed:
                    item["comments_why"] = "取れなかった（止まった・時間切れ）"
            added.append(item)
            if not r or not r.get("enriched"):
                not_found.append({"video_id": vid, "url": t.get("url"), "seq": r.get("seq"),
                                  "why": "動画のページを読めなかった（削除・非公開・URL の違いの可能性）"})
        return {"added": added, "not_found": not_found, "comment_videos": len(targets)}

    def _fetch(self, job: dict, out: Path) -> bool:
        s = self.base.settings()
        pipeline.ensure_chrome(s["chrome_port"], self.base.log)   # アプリでは worker_entry が差し替えた Chrome の用意
        from acquire import spatest
        urls = self.base.music_urls()
        n = job["round"]
        kind = job.get("kind") or "deepen"
        t_start = time.time()
        limit_h = (float(job.get("est_min") or EST["budget_min"]) + EST["deadline_extra_min"]) / 60
        blocked = False
        by_page = {}
        for x in job["targets"]:
            by_page.setdefault(_page_key(x), []).append(x)
        # 台帳の楽曲ページ（番号順）→ 台帳に無い動画の音源のページ（0.8.1。利用者が足した動画）
        for k in sorted((k for k in by_page if isinstance(k, int))) + sorted(k for k in by_page if isinstance(k, str)):
            if isinstance(k, str):
                url, tag = k, f"m{pipeline.music_id_of(k)}"
            elif k > len(urls):
                self.base.log(f"    楽曲ページ {k} が分かりません（{len(by_page[k])}本は取らない）")
                continue
            else:
                url, tag = urls[k - 1], f"p{k}"
            pool = self.p("derived", "deepen", f"r{n}_pool_{tag}.tsv")
            write_pool(pool, by_page[k], job["community"], n, kind)
            left = limit_h - (time.time() - t_start) / 3600
            if left <= 0.01:
                self.base.log("    時間の上限に達しています")
                break
            # 1本の上限は pool の cap 列（spatest が行ごとに守る）。--cap はその最大（per_video で 120件を超えることがある）
            cap = max(int(x["cap"]) for x in by_page[k])
            args = ["--port", str(s["chrome_port"]), "--music-url", url, "--pool", str(pool),
                    "--subs-out", str(self.p("fetch_log", f"deepen_r{n}_subs_{tag}.tsv")),
                    "--resume", "--collect-scrolls", str(s["collect_scrolls"]),
                    "--reply-policy", "targets", "--reply-top", str(s["reply_top"]),
                    "--reply-questions", str(s["reply_questions"]), "--reply-author", str(s["reply_author"]),
                    "--cap", str(cap), "--min-comments", "20",
                    "--calls-per-min", str(s["calls_per_min"]), "--max-calls-per-min", str(s["max_calls_per_min"]),
                    "--interval", str(s["interval"]), "--jitter", "0.5", "--deadline-hours", f"{left:.3f}",
                    "--out", str(out), "--log", str(self.p("fetch_log", f"deepen_r{n}.log")),
                    "--summary", str(self.p("fetch_log", f"deepen_r{n}_summary_{tag}.json"))] + brakes(cap)
            # 候補（--candidates）は渡さない: グリッドに無い動画を同じ週の別の動画に差し替えると、界隈の外の動画になるため
            args += [f"--{f.replace('_', '-')}" for f in ("stop_on_no_more", "prescroll", "only_open_video", "remount_on_stall")
                     if s.get(f)]
            a = spatest.build_parser().parse_args(args)
            self.base.lock(what_of(job))
            c = spatest.SpaCollector(a)
            c.between_videos = self.base.yield_lock
            try:
                c.run()
            except spatest.Blocked as e:
                c.log(f"!! 開始前にブロック検知: {e}")
                blocked = True
            finally:
                try:
                    if c.d:
                        c.d.service.stop()
                except Exception:
                    pass
            blocked = blocked or any(r.get("status") == "blocked" for r in c.rows)
            if blocked:
                self.base.log("    ブロックを検知したので、ここまでで止めます（取れた分は足します）")
                break
        return blocked


def write_pool(path: Path, targets: list, community: str, n: int, kind: str = "deepen") -> None:
    """spatest の --pool の形（pool.tsv と同じ列）。新しく取る動画を先に（priority 1）。理由は <回の種類>:r<回>:<界隈>:<new|more>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("video_id\tseq\tdate\tweek\tplays\tcap\tpriority\treasons\n")
        for x in targets:
            f.write(f"{x['video_id']}\t{x['seq']}\t{x.get('date') or ''}\t{x.get('week') or ''}\t{x['plays']}\t{x['cap']}\t"
                    f"{'1' if x['kind'] == 'new' else '2'}\t{kind}:r{n}:{x.get('community') or community}:{x['kind']}\n")


# ---------------------------------------------------------------------------
# 原本に足す
# ---------------------------------------------------------------------------
def merge(d: Path, n: int, community: str, kind: str = "deepen", by_video: dict | None = None) -> dict:
    """r<n>.jsonl の ok の行を raw/comments.jsonl に足し、derived/comments を作り直す。
    続きを取った動画は前のコメントと cid で合わせ、増えなければ足さない（取り直しで減っても前の分は失わない）。
    kind は印の頭（deepen＝掘り下げ・recut＝切り直し・videos＝動画ごと・全体の取り足し・add＝動画を足す）、
    by_video は動画ごとの界隈（切り直し・全体の取り足しは動画ごとに界隈が違う）"""
    d = Path(d)
    src = d / "raw" / "deepen" / f"r{n}.jsonl"
    got = _ok_rows(src)
    old = _ok_rows(d / "raw" / "comments.jsonl")
    add, n_new, n_more, added = [], 0, 0, 0
    for vid, r in got.items():
        r = dict(r)
        r.update({"pool_reason": f"{kind}:r{n}:{(by_video or {}).get(vid) or community}", "deepen_round": n})
        o = old.get(vid)
        if o:
            have = {str(c.get("cid")) for c in r.get("comments") or []}
            extra = [c for c in o.get("comments") or [] if str(c.get("cid")) not in have]
            merged = (r.get("comments") or []) + extra
            before = len(o.get("comments") or [])
            if len(merged) <= before:
                continue
            rc_have = {str(c.get("cid")) for c in r.get("reply_comments") or []}
            r["reply_comments"] = (r.get("reply_comments") or []) + [c for c in o.get("reply_comments") or []
                                                                      if str(c.get("cid")) not in rc_have]
            r.update({"comments": merged, "fetched": len(merged), "deepen_kind": "more", "had": before})
            n_more += 1
            added += len(merged) - before
        else:
            r["deepen_kind"] = "new"
            n_new += 1
            added += len(r.get("comments") or [])
        add.append(r)
    tried = _rows(src)
    res = {"videos_new": n_new, "videos_more": n_more, "comments_added": added,
           "tried": len(tried), "not_ok": sum(1 for r in tried.values() if r.get("status") != "ok")}
    # 楽曲ページごとの要約（台帳のページは _p<番号>、台帳に無い動画の音源のページは _m<音源の id>）
    summs = [pipeline.read_json(p, {}) or {} for p in sorted((d / "fetch_log").glob(f"deepen_r{n}_summary_*.json"))]
    res["unreachable"] = sum(len(s.get("missing") or []) for s in summs)
    res["not_fetched_time"] = sum(len(s.get("not_fetched_time") or []) for s in summs)
    if not add:
        return res
    with open(d / "raw" / "comments.jsonl", "a", encoding="utf-8", newline="\n") as f:
        for r in add:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    res["prep_comments"] = prep(d)
    return res


# 取り足した動画の印（derived/comments の見出しの「プールに入れた理由」）。回の種類 → 印。{c} は界隈
MARKS = {"deepen": "界隈の掘り下げで取り足した（{c}）", "recut": "界隈の切り直しで取り足した（{c}）",
         "videos": "コメントの取り足しで取り足した（{c}）", "add": "利用者が足した動画"}


def mark_of(reason: str) -> str:
    """原本の pool_reason（<回の種類>:r<回>:<界隈>）→ 印。界隈の代わりに回の名前（videos・all・add）が入っているときは界隈を書かない"""
    kind, _, c = (reason.split(":") + ["", "", ""])[:3]
    text = MARKS.get(kind, MARKS["deepen"])
    return text.replace("（{c}）", "") if c in ("", "videos", "all", "add") else text.format(c=c)


def prep(d: Path) -> str:
    """derived/comments を作り直す（本線の comments_md と同じ引数。プールの理由は掘り下げで取った動画の印を足した写しを渡す）"""
    d = Path(d)
    pool_all = d / "derived" / "deepen" / "pool_all.tsv"
    pool_all.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    head = None
    if (d / "derived" / "pool.tsv").exists():
        with open(d / "derived" / "pool.tsv", encoding="utf-8") as f:
            rd = csv.DictReader(f, delimiter="\t")
            head = rd.fieldnames
            rows = list(rd)
    head = list(head or ["video_id", "seq", "cap", "priority", "reasons"])
    by_vid = {r["video_id"]: r for r in rows}
    marked = set()
    for r in _jsonl(d / "raw" / "comments.jsonl"):
        reason = str(r.get("pool_reason") or "")
        vid = str(r["video_id"])
        if not reason.startswith(tuple(f"{k}:" for k in MARKS)) or vid in marked:
            continue
        mark = mark_of(reason)
        if vid in by_vid:   # プールに載っていた動画（週ごとの動画は cap 0 で載る。1ページの取り方 0.5.4〜）にも印を足す
            p = by_vid[vid]
            p["reasons"] = ",".join(x for x in (p.get("reasons") or "", mark) if x)
        else:
            rows.append({"video_id": vid, "reasons": mark, "priority": "3"})
        marked.add(vid)
    with open(pool_all, "w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=head, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in head})
    cmd = [sys.executable, str(BASE_DIR / "analysis" / "prep_comments.py"), str(d / "derived"), str(d / "raw" / "comments.jsonl"),
           "--records", str(d / "derived" / "llm_input" / "records.jsonl"), "--out", str(d / "derived" / "comments"),
           "--pool", str(pool_all)]
    r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONUTF8": "1"})
    if r.returncode != 0:
        raise RuntimeError(f"prep_comments.py が失敗: {(r.stderr or r.stdout)[-300:]}")
    return (r.stdout or "").strip().splitlines()[-1:][0] if (r.stdout or "").strip() else ""


# ---------------------------------------------------------------------------
def _main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="界隈の掘り下げ: 何を取るかを出す（TikTok に触らない）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan")
    pp.add_argument("dir")
    pp.add_argument("community", nargs="?")
    pp.add_argument("--minutes", type=float, default=None)
    pp.add_argument("--min-plays", type=int, default=None)
    pp.add_argument("--per-video", default=None, help="1本の上限（件数か all）")
    pp.add_argument("--videos", default=None, help="名指しの動画（ID・URL・seq をカンマで）。plan_videos")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    per = per_video_of(a.per_video)
    if a.videos or a.community == "all":
        pl = plan_videos(d, [v for v in a.videos.split(",") if v.strip()], per) if a.videos else plan_all(d, per)
        print(f"{pl['community']:22s} 新しく{pl['n_new']:3d}本・続き{pl['n_more']:3d}本 見込み{pl['est_min']:5.1f}分 "
              f"ページ{pl['pages']}{pl['music_urls'] or ''} 外した{pl['skipped']}")
        for x in pl.get("skipped_videos") or []:
            print(f"  取らない: {x['given']}（{x['why']}）")
        return 0
    comms = [a.community] if a.community else sorted({l["community"] for l in _labels(d).values()} - {"unknown"})
    for k in comms:
        pl = plan(d, k, a.minutes, a.min_plays, per)
        print(f"{k:22s} 新しく{pl['n_new']:3d}本（残り{pl['left_new']}）・続き{pl['n_more']:3d}本（残り{pl['left_more']}）"
              f" 見込み{pl['est_min']:5.1f}分 ページ{pl['pages']} 外した{pl['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
