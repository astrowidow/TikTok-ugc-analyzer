"""
AI の段の仕事の列（W1、段3）。取得の段（acquire/）が作った分析フォルダを入力に、2回目の着席の中身を回す。

docs/IMPLEMENTATION_HANDOVER.md 第6章、docs/W1_WORKFLOW.md、docs/IMPLEMENTATION_LOG.md D21〜D24。
proto_runner.py（接続口の核: 状態・計測・道具）が、analysis.json に "proto" の無い分析をこの流れに回す。

仕事の列（kind: ai＝AI がやる / ask_user＝利用者に聞く / service＝サービスが自動でやる / done）:
  axes（過去レポート2本→サンプル→サムネ。界隈ごとの代表例つき）→ ★confirm（初出順・リンク・確認用 CSV）
  → label×（30本ずつ。地域はサービスが付ける）→ phases（区切りの候補と週×界隈の表はサービス）
  → plan［service: 以降の仕事を足す］→ ccomments×（界隈ごとにコメントの傾向。具体の動画で確かめる）
  → ref_select（参考にする過去記事を選ぶ）→ ref_digest×（章立てと論理の運びだけ取り出す）→ outline（構成案と主張・根拠の対応）
  → write×（構成案に沿って。拡大経路は段階ごと・分岐点・音楽・結果・冒頭）
  → assemble［service: REPORT.md と付録］→ finish×（note 用、章ごと）→ finish_title → verify［service: 検算］
  → export［service: Excel 用 ZIP］→ done（ダウンロードのリンク）
完成後の直し（revise）は、revise → finish（その章）→ assemble → verify → done を末尾に足す。
完成後の界隈の掘り下げ（deepen、2026-10-06〜。docs/DEEPEN_COMMUNITY.md）は、取り足しの取得（acquire/deepen.py）のあと、
dcomments → outline_revise →（書き直し）→ review →（直し・仕上げ）→ assemble → verify → export → done を末尾に足す。
前の版は dcomments を受け付けたときに outputs/history/deepen_r<回>/ に写す。
完成後の界隈の切り直し（recut、2026-10-06〜。docs/RECUT_COMMUNITY.md）は、recut（分類軸を利用者の指示どおりに直す）を末尾に足し、
受け付けたら label×（顔ぶれが変わりうる界隈の動画だけ）→ recut_check［service: 新しい界隈の必ず読みたい動画にコメントがあるか。
無ければ取り足しを積む（acquire/recut.py）］→ phases → plan（以降はふつうの流れ）を足す。前の版は outputs/history/recut_r<回>/。
2026-10-03 に、代表の選定（reps）→ 1本ずつのコメント分析（comments）→ 界隈ごとの統合（synthesis）を、界隈ごとのコメント分析（ccomments）に
まとめ、執筆の前に参考記事・構成案を足した（ユーザー）。前の形で始めた分析は、前の形のまま最後まで回る（reps・comments・synthesis を残してある）。
"""
import collections
import copy
import csv
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import proto_runner as pr
import user_settings

BASE_DIR = Path(__file__).resolve().parent
PROMPTS = BASE_DIR / "service_prompts" / "w1"
KB_DIR = Path(os.environ.get("UGC_KB_DIR", BASE_DIR / "output" / "knowledge"))
PUBLIC_URL = os.environ.get("UGC_PUBLIC_URL", "https://giblet-squishy-icing.ngrok-free.dev").rstrip("/")
# 取得アプリ（各自の Mac で全部を回す形）では、成果物をダウンロードのリンクでなく、この Mac のフォルダに置く
REPORTS_DIR = os.environ.get("UGC_REPORTS_DIR")

# 仕事の刻み方（6-5。analysis.json の "ai_settings" で上書きできる）
DEFAULTS = {
    "label_batch": 30,          # ラベル付けの1回の本数
    "n_reps": 60,               # コメント分析する代表の本数（コメントが取れた動画から）
    "reps_min_per_cell": 2,     # 界隈×段階のセルごとの最低本数（最初期1＋最大再生1）
    "extra_past_reports": 3,    # 執筆で AI が選んで読む近い過去レポートの上限
    "axes_sample_max": 220,     # 軸の提案で読ませるサンプルの上限（期間が長いと層化サンプルが膨らむため。再生上位・最初期は全部残す）
    "past_reports": ["2024-08-22_n06c909376d1b", "2024-11-19_nf643834b934f"],  # 軸の提案の前に読む2本（F10、E2E と同じ）
    "n_refs": 3,                # 構成案の前に、組み立てを参考にする過去記事の本数
    "exclude_same_song": True,  # 分析する曲を扱った・触れた過去記事を読ませない（その記事の結論に引っ張られないため）
    "digest_top": 8,            # 界隈ごとのコメント分析で、動画1本から並べるコメントの数（いいね順）
    "digest_chars": 140,        # その1件の長さの上限
    # 完成後の界隈の掘り下げ（docs/DEEPEN_COMMUNITY.md）: 大きな界隈は取ったコメントの1〜2割しか読んでいなかったので、読む量を増やす
    "deepen_digest_top": 15,    # 動画1本から並べるコメントの数（前回は digest_top）
    "deepen_full_reads": "6〜8",  # 全部読む動画の本数（前回は2〜4本）
}
# 読者（Web の記事）に見せない作業の言葉。執筆と仕上げの検査で止める（2026-10-03 ユーザー）
# 「再生を集めた動画」「注目を集めた動画」はふつうの言い方なので止めない。カードは過去記事のカード（kb:cards）の話だけ
# （「メッセージカードに想いを書いて」のような動画の中身は止めない。2026-10-06 通し試験）
READER_RE = re.compile(r"用語集|知識ベース|文体ガイド|因果パターン|過去レポート|過去記事のカード|記事の?カード|"
                       r"カード(?:によれば|によると|に載|の記事)|"
                       r"今回集めた|(?<!を)集めた動画|グリッド|ラベル付き|ラベルを付け|ラベル上|\d+\s*本中")
# 空けておく書き方（人が書き足す前提の文・書けないことの断り書き）。執筆と仕上げの検査で止める
# （2026-10-06 ユーザー「『ここは人が…』の箇所を極限まで無くしたい」。docs/WEB_RESEARCH.md）
PLACEHOLDER_RE = re.compile(r"人の考察|考察を入れる|書き足|扱えな|扱えません|扱えていな|音源(?:そのもの)?を?(?:は)?分析していな")
WEB_REF_RE = re.compile(r"\[W(\d+)\]")                  # ウェブで調べた事実の出どころの印（research.json の id）
KB_FILE_RE = re.compile(r"\d{4}-\d{2}-\d{2}_n[0-9a-z]{6,}")   # 知識ベースの記事の名前（時代背景の材料の出どころ。本文には書かない）
CHAPTER_TITLES = {"intro": "冒頭・本楽曲に着目すべき理由・分析方針", "branch": "拡大経路のまとめ・バズの分岐点とその理由",
                  "music": "楽曲の音楽的特徴・楽曲構成・時代背景", "result": "バズった結果・今回のヒットの核・再現性のある要素"}
REPORT_ORDER_FIXED = ["branch", "music", "result"]


def cfg(a) -> dict:
    return {**DEFAULTS, **(a.meta.get("ai_settings") or {})}


def tpl(name: str) -> str:
    """指示書。利用者が編集した指示書があればそれ（prompt_store.py。使えないときは初期の指示書）"""
    import prompt_store
    return prompt_store.load(name)


# ---------------------------------------------------------------------------
# データ（分析フォルダから読む）
# ---------------------------------------------------------------------------
def records(a) -> dict:
    out = {}
    p = a.derived("llm_input", "records.jsonl")
    for line in open(p, encoding="utf-8"):
        r = json.loads(line)
        out[int(r["seq"])] = r
    return out


def videos(a) -> dict:
    return {int(json.loads(l)["seq"]): json.loads(l) for l in open(a.derived("videos.jsonl"), encoding="utf-8")}


def pool(a) -> dict:
    p = a.derived("pool.tsv")
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {r["video_id"]: r for r in csv.DictReader(f, delimiter="\t")}


def fetched(a) -> dict:
    """コメントが取れた動画 {video_id: 行}（後の行が勝つ）"""
    out = {}
    p = a.dir / "raw" / "comments.jsonl"
    if p.exists():
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("status") == "ok" and r.get("comments"):
                out[str(r["video_id"])] = r
    return out


def all_cids(a) -> set:
    """引用してよい cid（本体・返信・同梱の返信）"""
    out = set()
    for r in fetched(a).values():
        for c in r.get("comments") or []:
            out.add(str(c.get("cid")))
            for rp in c.get("reply_comment") or []:
                out.add(str(rp.get("cid")))
        for rp in r.get("reply_comments") or []:
            out.add(str(rp.get("cid")))
    return out


_SAME_SONG = {}


def _norm(s) -> str:
    return re.sub(r"[\s・\-_/／（）()「」『』【】!！?？.,、。~〜]", "", str(s or "")).lower()


def same_song_files(a) -> set:
    """分析する曲を扱った・触れた過去記事（拡張子なしの名前）。題に曲名がある、または本文に曲名とアーティスト名の両方がある。
    参考に読ませない（kb:cards から外す・note:/past: で読めない）。著者の分析の結論に引っ張られないため（2026-10-03）"""
    if not cfg(a).get("exclude_same_song", True):
        return set()
    if a.id in _SAME_SONG:
        return _SAME_SONG[a.id]
    song = a.meta.get("song") or {}
    title, artist = _norm(song.get("title")), _norm(song.get("artist"))
    out = set()
    if len(title) >= 2 and (KB_DIR / "notes").exists():
        for f in (KB_DIR / "notes").glob("*.md"):
            text = f.read_text(encoding="utf-8")
            if title in _norm(text[:300]) or (artist and title in _norm(text) and artist in _norm(text)):
                out.add(f.stem)
    _SAME_SONG[a.id] = out
    return out


def cards(a) -> list:
    """参考に選べる過去記事のカード（同じ曲の記事を除く）"""
    p = KB_DIR / "distilled" / "cards.jsonl"
    ban = same_song_files(a)
    title = _norm((a.meta.get("song") or {}).get("title"))
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        if c.get("file", "").replace(".md", "") in ban or (title and title == _norm(c.get("song"))):
            continue
        out.append(c)
    return out


def music_pages(a) -> list:
    """取得した楽曲ページ（1つ目が主）。同じ曲の配信版・sped up 版などを合わせて取ったときは複数"""
    pages = pr._read_json(a.dir / "raw" / "music_pages.json", None)
    if isinstance(pages, list) and pages:
        return pages
    m = pr._read_json(a.dir / "raw" / "music_page.json", {}) or {}
    return [m] if m else []


def ugc_total(a):
    """楽曲ページに出ている UGC 数（取得のときに読んだもの。楽曲ページを合わせて取ったときは合計）。
    {"n", "text", "at", "parts", "partial"} か None。parts はページごとの {"label", "n", "text", "url"}（2つ以上のとき）"""
    pages = music_pages(a)
    got = [m for m in pages if m.get("video_count")]
    if not got:
        return None
    at = str(got[0].get("at") or "")[:10]
    if len(pages) == 1:
        return {"n": got[0]["video_count"], "text": got[0].get("video_count_text") or "", "at": at, "parts": [],
                "partial": False}
    parts = [{"label": "／".join(x for x in (m.get("title"), m.get("creator")) if x) or "楽曲ページ",
              "n": m.get("video_count"), "text": (m.get("video_count_text") or "読めず").replace(" 動画", ""),
              "url": m.get("url")} for m in pages]
    return {"n": sum(m["video_count"] for m in got), "text": "＋".join(x["text"] for x in parts), "at": at,
            "parts": parts, "partial": len(got) < len(pages)}


def release_info(a):
    """曲の公開日（楽曲ページが作られた日）と、それより前の日付で除いた投稿の数。{"date", "dropped"} か None"""
    b = pr._read_json(a.dir / "fetch_log" / "before_release.json", None)
    if isinstance(b, dict) and b.get("release"):
        return {"date": str(b["release"])[:10], "dropped": int(b.get("dropped") or 0)}
    return None


def ugc_parts_line(u) -> str:
    """合計の内訳（楽曲ページが2つ以上のとき）"""
    if not u or not u.get("parts"):
        return ""
    return "、".join(f"『{x['label']}』{x['text']}" for x in u["parts"])


def fmt_count(n) -> str:
    n = int(n)
    if n >= 10000:
        return f"約{n / 10000:.1f}万".replace(".0万", "万")
    return f"約{n:,}"


def url_of(v: dict) -> str:
    return (v.get("url") or "").split("?")[0]


def label_seqs(a) -> list:
    """ラベルを付ける動画: 層化のラベル対象 ∪ 候補プール（BLUEPRINT「プール＋上位・最初期」）"""
    seqs = {json.loads(l)["seq"] for l in open(a.derived("sample_label.jsonl"), encoding="utf-8")}
    by_vid = {v["video_id"]: s for s, v in videos(a).items()}
    for vid in pool(a):
        if vid in by_vid:
            seqs.add(by_vid[vid])
    return sorted(int(s) for s in seqs)


def axes_sample(a) -> list:
    """軸の提案で読ませるサンプルの seq。層化サンプル（prep_sample.py）のうち、再生上位100・最初期40は全部残し、
    残り（離陸後の各週の無作為）は時期に偏らないよう等間隔に間引いて axes_sample_max 本に収める"""
    _, body = a.entries("taxonomy_sample")
    seqs = sorted(body)
    cap = cfg(a)["axes_sample_max"]
    if len(seqs) <= cap:
        return seqs
    recs = records(a)
    top = set(sorted(recs, key=lambda s: -recs[s]["plays"])[:100])
    early = set(sorted(recs, key=lambda s: (recs[s]["date"], s))[:40])
    keep = [s for s in seqs if s in top or s in early]
    rest = [s for s in seqs if s not in top and s not in early]
    k = max(0, cap - len(keep))
    if k and rest:
        step = len(rest) / k
        keep += [rest[int(i * step)] for i in range(min(k, len(rest)))]
    return sorted(set(keep))


def labels(a) -> dict:
    p = a.outputs("labels.tsv")
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {int(r["seq"]): r for r in csv.DictReader(f, delimiter="\t")}


def phases(a) -> list:
    return (pr._read_json(a.outputs("phases.json"), {}) or {}).get("phases") or []


def phase_of(date: str, ph: list):
    for p in ph:
        if p["start"] <= date <= p["end"]:
            return p["id"]
    return ph[-1]["id"] if ph and date > ph[-1]["end"] else (ph[0]["id"] if ph else None)


def fmt_plays(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 100000000:
        return f"{n / 100000000:.1f}億"
    return f"{n / 10000:.0f}万" if n >= 100000 else f"{n:,}"


def vline(r: dict) -> str:
    """動画1本の短い記述（材料用）"""
    au = (r.get("author") or {}).get("id")
    return f"seq {r['seq']}（@{au}、{r['date']}、再生 {r['plays']:,}）"


def entry(a, r: dict, sheet_of: dict) -> str:
    """ラベル付けの入力（build_llm_input.py の fmt と同じ形）"""
    au = r.get("author") or {}
    sheet = sheet_of.get(r["seq"])
    parts = [
        f"### seq {r['seq']} | {r['date']} | {r['type']} | 再生 {r['plays']:,} / いいね {r['likes']:,} / コメント {r['comments']:,} / 共有 {r['shares']:,} | 尺 {r.get('duration_s')}s",
        f"- 投稿者: @{au.get('id')}（{au.get('nickname') or '-'}）fol={au.get('followers'):,} videos={au.get('videos')} verified={au.get('verified')}"
        if isinstance(au.get("followers"), int) else f"- 投稿者: @{au.get('id')}（属性取得なし）",
        f"- bio: {au['bio']}" if au.get("bio") else None,
        f"- 言語={r.get('text_language')} TikTokラベル={r.get('tiktok_labels')} 元音源={r.get('uses_original_sound')} 広告={r.get('is_ad')}",
        f"- 説明文: {r.get('desc') or '(なし)'}",
        f"- タグ: {' '.join('#' + t for t in r['hashtags'])}" if r.get("hashtags") else None,
        f"- メンション: {' '.join('@' + m for m in r['mentions'])}" if r.get("mentions") else None,
        f"- 画面上の文字: {r['sticker_texts']}" if r.get("sticker_texts") else None,
        f"- エフェクト: {r['effect_stickers']}" if r.get("effect_stickers") else None,
        f"- TikTok提案語（検索・コメント由来）: {r['suggested_words']}" if r.get("suggested_words") else None,
        f"- サムネイル: {sheet} の「seq {r['seq']}」の枠" if sheet else "- サムネイル: なし",
    ]
    return "\n".join(p for p in parts if p) + "\n\n"


def sheet_index(a) -> dict:
    """seq → read の名前（取得の段のシート＋ラベル用に足したシート）"""
    out = {}
    for s, f in (pr._read_json(a.derived("llm_input", "sheets", "index.json"), {}) or {}).items():
        out[int(s)] = pr._sheet_read_name(f)
    for s, f in (pr._read_json(a.derived("ai", "sheets", "index.json"), {}) or {}).items():
        out.setdefault(int(s), "xsheet:" + re.sub(r"\D", "", f))
    return out


def make_extra_sheets(a, seqs: list) -> int:
    """ラベル対象のうちシートに無い動画のサムネ一覧を足す（derived/ai/sheets/）。Pillow が無ければ作らない"""
    have = sheet_index(a)
    recs = records(a)
    need = [s for s in seqs if s not in have and recs.get(s, {}).get("cover_file")
            and Path(recs[s]["cover_file"]).exists()]
    if not need:
        return 0
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return 0
    out = a.derived("ai", "sheets")
    out.mkdir(parents=True, exist_ok=True)
    font = None
    for fp in ("/System/Library/Fonts/Helvetica.ttc", r"C:\Windows\Fonts\arial.ttf"):
        try:
            font = ImageFont.truetype(fp, 28)
            break
        except Exception:
            continue
    font = font or ImageFont.load_default()
    cols, rows, w, h = 5, 4, 216, 384
    idx = {}
    for n in range(0, len(need), cols * rows):
        chunk = need[n:n + cols * rows]
        sheet = Image.new("RGB", (cols * w, rows * (h + 36)), "white")
        dr = ImageDraw.Draw(sheet)
        for i, s in enumerate(chunk):
            try:
                im = Image.open(recs[s]["cover_file"]).convert("RGB")
                im.thumbnail((w, h))
            except Exception:
                continue
            x, y = (i % cols) * w, (i // cols) * (h + 36)
            sheet.paste(im, (x + (w - im.width) // 2, y + 36))
            dr.rectangle([x, y, x + w, y + 34], fill="black")
            dr.text((x + 6, y + 2), f"seq {s}", fill="white", font=font)
            idx[s] = f"xsheet_{n // (cols * rows):02d}.jpg"
        sheet.save(out / f"xsheet_{n // (cols * rows):02d}.jpg", quality=80)
    pr._write_json(out / "index.json", {str(k): v for k, v in idx.items()})
    return len(idx)


# ---------------------------------------------------------------------------
# 仕事の列
# ---------------------------------------------------------------------------
def _task(st: dict, typ: str, kind: str, title: str, params=None) -> dict:
    st["next_n"] = st.get("next_n", 1)
    n = st["next_n"]
    st["next_n"] += 1
    return {"n": n, "task_id": f"{st['analysis_id']}/{n:03d}-{typ}", "type": typ, "kind": kind, "title": title,
            "params": params or {}, "status": "pending", "first_issued_at": None, "issued_at": None,
            "issue_count": 0, "done_at": None, "rejects": 0}


def build_tasks(a) -> dict:
    """最初の仕事の列（代表の選定まで）。以降は reps が足す"""
    c = cfg(a)
    st = {"analysis_id": a.id, "created_at": pr._now(), "flow": "w1", "next_n": 1, "tasks": []}
    seqs = label_seqs(a)
    make_extra_sheets(a, seqs)
    batches = [seqs[i:i + c["label_batch"]] for i in range(0, len(seqs), c["label_batch"])]
    T = st["tasks"]
    T.append(_task(st, "axes", "ai", "分類軸（界隈など）の案を作る"))
    T.append(_task(st, "confirm", "ask_user", "界隈の確認（利用者に聞く）"))
    for i, b in enumerate(batches, 1):
        T.append(_task(st, "label", "ai", f"ラベル付け（{i}/{len(batches)}）",
                       {"batch": i, "n_batches": len(batches), "seqs": b}))
    T.append(_task(st, "phases", "ai", "段階の区切りと拡散経路の下書き"))
    T.append(_task(st, "plan", "service", "以降の仕事の準備（サービス）"))
    return st


def insert_after(st: dict, t: dict, new: list) -> None:
    i = st["tasks"].index(t)
    st["tasks"][i + 1:i + 1] = new


# ---------------------------------------------------------------------------
# 指示書
# ---------------------------------------------------------------------------
def _base(a) -> dict:
    s = a.meta.get("song", {})
    vs = videos(a)
    dates = sorted(v["date"] for v in vs.values())
    return {"song": a.song_line, "song_short": f"{s.get('artist', '')}「{s.get('title', '')}」",
            "n_videos": len(vs), "period": f"{dates[0]}〜{dates[-1]}" if dates else ""}


def _settings(a, items, task) -> str:
    block = user_settings.prompt_block(a.owner, items)
    user_settings.snapshot(a, task, items)
    return block


def render(a, t: dict, st: dict) -> dict:
    typ = t["type"]
    base = _base(a)
    cat = []
    c = cfg(a)

    if typ == "axes":
        body = axes_sample(a)
        pages = pr._pages_of(a, "taxonomy_sample")
        idx = sheet_index(a)
        sheets = sorted({idx[s_] for s_ in body if s_ in idx and idx[s_].startswith("sheet:")})
        # 過去レポートの固定の2本はやめ、全レポートから蒸留した「界隈の名付け方の手引き」を読む（2026-10-04 ユーザー
        # 「固定するくらいなら蒸留して全レポート参照。既存レポートに引っ張られすぎない。参考にするのは名付け方と粒度」）
        cat.append(pr._catalog_line("kb:community", "界隈の名付け方の手引き（全レポートから蒸留）。**最初に全ページ読む**"))
        cat.append(pr._catalog_line("taxonomy_sample", f"分類軸を考えるためのサンプル {len(body)} 本（page=1〜{len(pages)}）"))
        if sheets:
            cat.append(pr._catalog_line(", ".join(sheets), f"サムネイルの一覧画像 {len(sheets)} 枚（サンプルの動画が載っているもの）"))
        text = pr._fill(tpl("axes.md"), {**base, "n_sample": len(body), "n_pages": len(pages), "n_sheets": len(sheets),
                                         "sheet_list": "、".join(sheets) or "なし",
                                         "past_reports": "kb:community",
                                         "settings": _settings(a, ["community_policy"], t)})

    elif typ == "confirm":
        _safe_copy_materials(a)
        prop = pr._taxonomy(a, confirmed=False)
        text = pr._fill(tpl("confirm.md"), {**base, "present": present_axes(a, prop), "csv_url": review_csv(a, prop),
                                            "proposal_json": json.dumps(prop, ensure_ascii=False, indent=1)})

    elif typ == "label":
        tax = pr._taxonomy(a)
        recs = records(a)
        sheets = sheet_index(a)
        seqs = t["params"]["seqs"]
        entries = "".join(entry(a, recs[s], sheets) for s in seqs if s in recs)
        used = sorted({sheets[s] for s in seqs if s in sheets})
        if used:
            cat.append(pr._catalog_line(", ".join(used), "この回の動画のサムネイルが載っているシート"))
        tax_view = {k: v for k, v in tax.items() if k != "examples"}
        sett = _settings(a, ["community_policy"], t)
        if t["params"].get("recut"):
            sett += ("\n- この分類軸は、完成したレポートを読んだ利用者の指示（「" + recut_record(a, t["params"]["recut"]).get("instruction", "") +
                     "」）で界隈を切り直したもの。前のラベルにとらわれず、新しい定義のシグナルで付ける\n")
        text = pr._fill(tpl("label.md"), {**base, "batch": t["params"]["batch"], "n_batches": t["params"]["n_batches"],
                                          "n": len(seqs), "seq_list": ", ".join(map(str, seqs)), "entries": entries,
                                          "taxonomy_json": json.dumps(tax_view, ensure_ascii=False, indent=1),
                                          "settings": sett})

    elif typ == "phases":
        text = pr._fill(tpl("phases.md"), {**base, **phase_materials(a)})

    elif typ == "comments":
        tax = pr._taxonomy(a)
        seq = t["params"]["seq"]
        comments, video_id = a.comment_text(seq)
        lab = labels(a).get(seq) or {}
        ph = phase_of(videos(a)[seq]["date"], phases(a)) or ""
        label_line = (f"community={lab.get('community')} / format={lab.get('format')} / motive={lab.get('motive')} / "
                      f"tier={lab.get('tier')} / 地域={lab.get('region')}（conf={lab.get('conf')}。根拠: {lab.get('reason')}）／ 段階 {ph}"
                      if lab else f"（ラベルなし）／ 段階 {ph}")
        community = "\n".join(f"- `{k}`: {pr._first_sentence(v)}" for k, v in tax["community"].items() if not k.startswith("_"))
        cat.append(pr._catalog_line("kb:glossary", "著者の用語集（反応の読み方の語彙）。一度読めば次の動画では読まなくてよい"))
        text = pr._fill(tpl("comments.md"), {**base, "i": t["params"]["i"], "n": t["params"]["n"], "seq": seq,
                                             "video_id": video_id, "label_line": label_line, "phase": ph,
                                             "community_list": community, "comments": comments.rstrip(),
                                             "reaction_types": "|".join(pr.REACTION_TYPES),
                                             "settings": _settings(a, ["comment_lens"], t)})

    elif typ == "synthesis":
        text = pr._fill(tpl("synthesis.md"), {**base, **synthesis_materials(a, t)})

    elif typ == "ccomments":
        k = t["params"]["community"]
        mat = community_materials(a, k)
        cat.append(pr._catalog_line(f"ccomments:{k}", f"この界隈のコメント（動画ごとの上位、page=1〜{mat['n_pages']}）"))
        cat.append(pr._catalog_line("comments:<seq>", "動画1本のコメントの全部（傾向を確かめるとき）"))
        cat.append(pr._catalog_line("kb:glossary", "著者の用語集（反応の読み方の語彙）。一度読めば次の界隈では読まなくてよい"))
        text = pr._fill(tpl("comments_community.md"), {**base, **mat, "i": t["params"]["i"], "n": t["params"]["n"],
                                                        "settings": _settings(a, ["comment_lens"], t)})

    elif typ == "dcomments":
        k = t["params"]["community"]
        mat = deepen_materials(a, t, st)
        cat.append(pr._catalog_line(f"dcomments:{k}", f"この界隈のコメント（動画ごとの上位 {mat['digest_top']} 件、page=1〜{mat['n_pages']}）"))
        cat.append(pr._catalog_line("comments:<seq>", "動画1本のコメントの全部（傾向を確かめるとき）"))
        cat.append(pr._catalog_line("chapter:<id>", "今のレポートの章"))
        cat.append(pr._catalog_line("kb:glossary", "著者の用語集（反応の読み方の語彙）"))
        text = pr._fill(tpl("deepen_comments.md"), {**base, **mat, "settings": _settings(a, ["comment_lens"], t)})

    elif typ == "outline_revise":
        k = t["params"]["community"]
        chs = chapter_order(a, st)
        star = set(chapters_with_community(a, k, st))
        syn = a.outputs("synthesis", f"{k}.md")
        change = md_section(syn.read_text(encoding="utf-8"), "#### 前回からの変化") if syn.exists() else ""
        o = outline(a)
        cat += [pr._catalog_line("synthesis", "界隈ごとのコメント分析（この界隈は掘り下げたもの）"),
                pr._catalog_line("chapter:<id>", "今のレポートの章"), pr._catalog_line("pathway", "拡散経路の下書き")]
        text = pr._fill(tpl("outline_revise.md"), {
            **base, "community": k, "instruction": t["params"]["instruction"],
            "change": change or "（掘り下げた分析に「前回からの変化」が無い。read の synthesis で読む）",
            "outline": outline_md(o, chs) if o else "（構成案は無い。前の形の分析。章の今の原稿を read の chapter:<id> で読んで決める）",
            "chapters": "\n".join(f"- {'★' if c_ in star else '　'}`{c_}`: {first_line(a.outputs('chapters', f'{c_}.md'))}" for c_ in chs),
            "settings": _settings(a, ["focus"], t)})

    elif typ == "review":
        pm = t["params"]
        chs = chapter_order(a, st)
        o = outline(a) or {}
        text = pr._fill(tpl("review.md"), {
            **base, "community": pm["community"], "instruction": pm["instruction"], "thesis": o.get("thesis") or "（構成案が無い）",
            "changed": "\n".join(f"- `{x['chapter']}`: {x.get('why', '')}" for x in pm.get("changed") or []) or "（なし）",
            "chapters": "\n".join(f"- `{c_}`: {first_line(a.outputs('chapters', f'{c_}.md'))}" for c_ in chs)})
        cat.append(pr._catalog_line("chapter:<id>", "章の今の原稿（全章を読む）"))

    elif typ == "ref_select":
        cat.append(pr._catalog_line("synthesis", "界隈ごとのコメント分析"))
        cat.append(pr._catalog_line("kb:cards", f"過去の記事のカード（{len(cards(a))}本。1行1本）"))
        text = pr._fill(tpl("ref_select.md"), {**base, "n_refs": cfg(a)["n_refs"], "summary": analysis_summary(a)})

    elif typ == "ref_digest":
        pm = t["params"]
        if not pm.get("file"):
            raise pr.RunnerError("参考記事がまだ選ばれていません（前の仕事）。next_task を呼び直してください")
        body = (KB_DIR / "notes" / pm["file"]).read_text(encoding="utf-8")
        if len(body) > 24000:
            body = body[:24000] + "\n（本文が長いので 24,000 字で切った）\n"
        text = pr._fill(tpl("ref_digest.md"), {**base, "ref_title": pm["title"], "ref_title_short": pm["title"][:40],
                                                "ref_date": pm.get("date", ""), "i": pm["i"], "n": pm["n"],
                                                "why": pm.get("why", ""), "article": body})

    elif typ == "research":
        cat += [pr._catalog_line("pathway", "拡散経路の下書き"), pr._catalog_line("synthesis", "界隈ごとのコメント分析")]
        text = pr._fill(tpl("research.md"), {**base, "materials": research_materials(a), "today": datetime.date.today().isoformat()})

    elif typ == "era":
        cat += [pr._catalog_line("note:<名前>", "著者の過去の記事の全文（材料の一覧の名前）"),
                pr._catalog_line("kb:cards", f"過去の記事のカード（{len(cards(a))}本。1行1本。一覧で足りなければ）"),
                pr._catalog_line("synthesis", "界隈ごとのコメント分析")]
        text = pr._fill(tpl("era.md"), {**base, "materials": era_materials(a)})

    elif typ == "outline":
        cat += [pr._catalog_line("refs", "参考記事の章立てと論理の運び"), pr._catalog_line("synthesis", "界隈ごとのコメント分析"),
                pr._catalog_line("pathway", "拡散経路の下書き")]
        if a.outputs("research.json").exists():
            cat += [pr._catalog_line("research", "ウェブで調べたこと（W番号つき）"), pr._catalog_line("era", "時代背景の材料")]
        lines = [f"- `{c_}`: {heading_of(a, c_)}（{CHAPTER_TITLES.get(c_) or '拡大経路の段階'}）" for c_ in chapter_order(a, st)]
        text = pr._fill(tpl("outline.md"), {**base, "n_refs": cfg(a)["n_refs"], "chapters": "\n".join(lines),
                                             "materials": outline_materials(a, base),
                                             "settings": _settings(a, ["focus"], t)})   # レポートの重点は、主張を決める構成案から効かせる

    elif typ == "write":
        text, cat = write_prompt(a, t, base, st)

    elif typ == "finish":
        text = pr._fill(tpl("finish.md"), {**base, **finish_materials(a, t, st)})

    elif typ == "finish_title":
        ch = chapter_order(a, st)
        heads = "\n".join(f"- `{c_}`: {first_line(a.outputs('note_chapters', f'{c_}.md'))}" for c_ in ch
                          if a.outputs("note_chapters", f"{c_}.md").exists())
        o = outline(a) or {}
        guesses = [f"- `{c_.get('id')}`: {cl.get('claim', '')}" for c_ in o.get("chapters") or [] if isinstance(c_, dict)
                   for cl in c_.get("claims") or [] if isinstance(cl, dict) and cl.get("guess")]
        deleted = [f"seq {s}" for s in mentioned_seqs(a, ch) if not records(a).get(s, {}).get("enriched")]
        cat.append(pr._catalog_line("note_chapter:<章の id>", "仕上げた章"))
        text = pr._fill(tpl("finish_title.md"), {**base, "headings": heads, "guesses": "\n".join(guesses) or "（なし）",
                                                 "deleted": "、".join(deleted) or "なし"})

    elif typ == "revise":
        chs = chapter_order(a, st)
        lines = [f"- `{c_}`: {first_line(a.outputs('chapters', f'{c_}.md'))}" for c_ in chs]
        cat.append(pr._catalog_line("chapter:<id>", "章の今の原稿"))
        text = pr._fill(tpl("revise.md"), {**base, "instruction": t["params"]["instruction"], "chapters": "\n".join(lines),
                                           "settings": _settings(a, ["style", "focus"], t)})

    elif typ == "recut":
        chs = chapter_order(a, st)
        cat += [pr._catalog_line("labeled:<界隈の key>", "その界隈にラベルを付けた動画（説明文・タグ・bio・サムネの場所・今のラベル。再生の多い順）"),
                pr._catalog_line("sheet:NN, xsheet:NN", "サムネイルの一覧画像（各動画の「サムネイル」欄にある名前）"),
                pr._catalog_line("synthesis", "今の版の界隈ごとのコメント分析"), pr._catalog_line("pathway", "今の版の拡散経路の下書き"),
                pr._catalog_line("chapter:<id>", "今の版の章"), pr._catalog_line("kb:community", "界隈の名付け方の手引き（名前の付け方と粒度）")]
        text = pr._fill(tpl("recut.md"), {**base, "instruction": t["params"]["instruction"], "overview": recut_overview(a),
                                           "taxonomy_json": json.dumps(pr._taxonomy(a), ensure_ascii=False, indent=1),
                                           "chapters": "\n".join(f"- `{c_}`: {first_line(a.outputs('chapters', f'{c_}.md'))}" for c_ in chs),
                                           "settings": _settings(a, ["community_policy"], t)})

    elif typ == "done":
        mat = done_materials(a)
        if t["params"].get("deepen"):
            mat["summary"] = deepen_summary(a, t["params"]["deepen"]) + "\n\n" + mat["summary"]
        if t["params"].get("recut"):
            mat["summary"] = recut_summary(a, t["params"]["recut"]) + "\n\n" + mat["summary"]
        # 界隈の確認を省いたことは、最初の完了の知らせでだけ伝える（あとの直しの完了では繰り返さない。切り直しのあとは利用者が決めた界隈）
        first_done = next((x for x in st.get("tasks") or [] if x.get("kind") == "done"), None)
        is_first = first_done is None or first_done.get("task_id") == t.get("task_id")
        if not t["params"].get("deepen") and not t["params"].get("recut") and is_first and confirm_skipped_summary(a):
            mat["summary"] = confirm_skipped_summary(a) + "\n\n" + mat["summary"]
        text = pr._fill(tpl("done.md"), {**base, **mat})

    else:
        raise pr.RunnerError(f"知らない仕事の種類です: {typ}")
    return {"text": text, "catalog": cat}


# --- 界隈の確認（F1〜F3）---
def present_axes(a, tax: dict) -> str:
    """界隈を初出の順に並べ、初出日・定義・代表動画のリンクを添える"""
    recs = records(a)
    vs = videos(a)
    marks = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    ex = tax.get("examples") or {}
    items = []
    for k, v in tax["community"].items():
        if k.startswith("_") or k == "unknown":
            continue
        seqs = [int(s) for s in ex.get(k, []) if int(s) in recs]
        first = min((recs[s]["date"] for s in seqs), default="9999")
        items.append((first, k, v, seqs))
    items.sort()
    lines = []
    for i, (first, k, v, seqs) in enumerate(items):
        mark = marks[i] if i < len(marks) else f"({i + 1})"
        lines.append(f"{mark} {pr._first_sentence(v)}（`{k}`）— 初出 {first if first != '9999' else '不明'}")
        for s in seqs[:3]:
            r = recs[s]
            u = url_of(vs.get(s, {}))
            au = (r.get("author") or {}).get("id")
            stat = f"{fmt_plays(r['plays'])}再生" if r.get("enriched") else "写真・削除済みなど"
            lines.append(f"    - [@{au}（{r['date']}、{stat}）]({u})" if u else f"    - @{au}（{r['date']}）")
    if "unknown" in tax["community"]:
        lines.append(f"（ほかに `unknown`: {pr._first_sentence(tax['community']['unknown'])}）")
    fmt = ", ".join(k for k in tax.get("format", {}) if not k.startswith("_"))
    mot = ", ".join(k for k in tax.get("motive", {}) if not k.startswith("_"))
    lines += ["", f"（参考）投稿の型: {fmt}", f"（参考）この曲を使った理由の型: {mot}"]
    return "\n".join(lines)


def download_key(a) -> str:
    key = a.meta.get("download_key")
    if not key:
        import secrets
        key = secrets.token_urlsafe(24)
        m = pr._read_json(a.dir / "analysis.json")
        m["download_key"] = key
        pr._write_json(a.dir / "analysis.json", m)
        a.meta = m
    return key


# 成果物の名前 → 分析フォルダの中の場所（downloads.py の FILES と同じ）
DELIVERABLES = {"REPORT.md": "outputs/REPORT.md", "NOTE_BODY.md": "outputs/NOTE_BODY.md",
                "EDITOR_NOTES.md": "outputs/EDITOR_NOTES.md", "data.zip": "outputs/data.zip",
                "videos_review.csv": "derived/review/videos_review.csv"}


# 利用者のフォルダに置くときの名前と、完了の知らせの説明（2026-10-06 ユーザー「生成物の説明がわかりにくい。分析レポートと note 用の原稿の
# 違いは？」「書き足すところのメモって、もうちょいいい表現ないかな」）。読む・貼るのは仕上げた版。根拠の番号つきの版は、仕上げる前の原稿
SHOWN = {"NOTE_BODY.md": ("レポート.md", "読む・note に貼るのはこれ（動画は埋め込みの行、根拠の番号なし）"),
         "REPORT.md": ("レポート（根拠の番号つき）.md",
                       "同じ分析の、仕上げる前の原稿。どの動画（seq）・コメント（cid）・ウェブの出どころ（W）から言ったかの番号と、"
                       "使ったデータの付録つき（Excel 用のデータと突き合わせるとき）"),
         "EDITOR_NOTES.md": ("確認メモ.md", "ウェブで調べた数字の出どころと、推測で書いたところ（公開の前に確かめたいとき）")}


def report_folder(a) -> Path:
    """この Mac で成果物を置くフォルダ（取得アプリの形だけ）。曲名と分析を作った日で名前を付ける"""
    title = re.sub(r'[\\/:*?"<>|\s]+', "_", a.title).strip("_")[:60] or a.id
    day = str(a.meta.get("created_at") or "")[:10]
    return Path(REPORTS_DIR) / (f"{title}（{day}）" if day else title)


def folder_lines(a) -> list:
    """成果物のフォルダ（取得アプリの形だけ）: フォルダへのリンクと、リンクが開かないときの場所の文字"""
    from urllib.parse import quote
    d = report_folder(a)
    d.mkdir(parents=True, exist_ok=True)
    shown = str(d).replace(str(Path.home()), "~", 1)
    return [f"- フォルダ: [{d.name}](file://{quote(str(d))}/)",
            f"  （場所: `{shown}`。UGC Analyzer のメニュー「レポートのフォルダを開く」でも開けます）"]


def dl_url(a, name: str) -> str:
    if REPORTS_DIR:
        from urllib.parse import quote
        src = a.dir / DELIVERABLES[name]
        dst = report_folder(a) / (SHOWN[name][0] if name in SHOWN else name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.copy2(src, dst)
            if name in SHOWN and (report_folder(a) / name).exists():   # 前の版の名前（REPORT.md など）で置いた写しは消す
                (report_folder(a) / name).unlink()
        return "file://" + quote(str(dst))
    return f"{PUBLIC_URL}/dl/{download_key(a)}/{name}"


WEEKLY_CSV = "週ごとの投稿数.csv"


def copy_materials(a) -> None:
    """レポートのフォルダに、週ごとの投稿数と再生の表（Excel 用）を置く（取得アプリの形だけ）。
    （2026-10-04 ユーザー「週ごとの投稿数・再生数はいい。そのほかはいらない。data.zip があるならいい」）"""
    if not REPORTS_DIR:
        return
    wk = a.derived("weekly.tsv")
    if not wk.exists():
        return
    rows = [ln.split("\t") for ln in wk.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not rows:
        return
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["週", "投稿数", "再生の合計"])
    w.writerows(rows[1:])
    d = report_folder(a)
    d.mkdir(parents=True, exist_ok=True)
    (d / WEEKLY_CSV).write_text(buf.getvalue(), encoding="utf-8-sig")


def review_csv(a, tax: dict) -> str:
    """確認用の全動画一覧（F3）。サービスが作り、AI は通さない。Excel で開けるよう UTF-8（BOM つき）"""
    recs = records(a)
    vs = videos(a)
    ex = {}
    for k, seqs in (tax.get("examples") or {}).items():
        for s in seqs:
            ex.setdefault(int(s), []).append(k)
    p = a.derived("review", "videos_review.csv")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq", "投稿日", "URL", "再生", "いいね", "コメント", "共有", "投稿者", "表示名", "フォロワー", "認証",
                    "bio", "地域", "言語", "TikTokのラベル", "説明文", "界隈の案の代表例"])
        for s in sorted(recs):
            r = recs[s]
            au = r.get("author") or {}
            w.writerow([s, r["date"], url_of(vs.get(s, {})), r["plays"], r["likes"], r["comments"], r["shares"],
                        au.get("id"), au.get("nickname"), au.get("followers"), au.get("verified"),
                        (au.get("bio") or "").replace("\n", " "), r.get("location_created"), r.get("text_language"),
                        "、".join(r.get("tiktok_labels") or []), (r.get("desc") or "").replace("\n", " "),
                        "、".join(ex.get(s, []))])
    return dl_url(a, "videos_review.csv")


# --- 段階 ---
def phase_materials(a) -> dict:
    labs = labels(a)
    recs = records(a)
    weekly = collections.defaultdict(collections.Counter)
    for s, l in labs.items():
        weekly[recs[s]["week"]][l["community"]] += 1
    allw = {}
    for line in open(a.derived("weekly.tsv"), encoding="utf-8").read().splitlines()[1:]:
        w, n, plays = line.split("\t")
        allw[w] = (int(n), int(plays))
    total = collections.Counter(l["community"] for l in labs.values())
    top = [k for k, _ in total.most_common(8) if k != "unknown"]
    head = "| 週 | " + " | ".join(top) + " | その他 | ラベル計 | 全動画 | 全再生 |\n|" + "---|" * (len(top) + 5)
    rows = [head]
    for w in sorted(set(allw) | set(weekly)):
        cnt = weekly.get(w, collections.Counter())
        other = sum(v for k, v in cnt.items() if k not in top)
        n, plays = allw.get(w, (0, 0))
        rows.append(f"| {w} | " + " | ".join(str(cnt.get(k, 0)) for k in top) +
                    f" | {other} | {sum(cnt.values())} | {n} | {fmt_plays(plays)} |")
    # 区切りの候補（数字で）
    weeks = sorted(allw)
    grand = sum(n for n, _ in allw.values()) or 1
    takeoff = next((w for w in weeks if allw[w][0] >= grand * 0.05), weeks[-1] if weeks else "")
    peak = max(weeks, key=lambda w: allw[w][0]) if weeks else ""
    cands = [f"- 最初の投稿: {min(r['date'] for r in recs.values())}（{min(recs.values(), key=lambda r: r['date'])['week']}）",
             f"- 離陸週（週の本数が全体の5%を初めて超えた週）: {takeoff}",
             f"- 本数のピーク週: {peak}（{allw.get(peak, (0, 0))[0]}本）"]
    prev = None
    for w in weeks:
        cnt = weekly.get(w)
        if not cnt or sum(cnt.values()) < 3:
            continue
        dom = cnt.most_common(1)[0][0]
        if prev and dom != prev:
            cands.append(f"- 最多の界隈が入れ替わった週: {w}（{prev} → {dom}）")
        prev = dom
    firsts = []
    for k in top:
        s = min((s for s, l in labs.items() if l["community"] == k), key=lambda s: recs[s]["date"])
        firsts.append(f"- {k}: {vline(recs[s])}")
    dates = sorted(r["date"] for r in recs.values())
    return {"n_labeled": len(labs), "candidates": "\n".join(cands[:14]), "weekly_table": "\n".join(rows),
            "firsts": "\n".join(firsts), "first_date": dates[0], "last_date": dates[-1]}


# --- 界隈ごとの統合 ---
def video_analyses(a) -> dict:
    out = {}
    d = a.outputs("video_analysis")
    if d.exists():
        for f in d.glob("*.json"):
            out[int(f.stem)] = pr._read_json(f)
    return out


def synthesis_materials(a, t) -> dict:
    tax = pr._taxonomy(a)
    k = t["params"]["community"]
    labs = labels(a)
    recs = records(a)
    ph = phases(a)
    cnt = collections.Counter()
    plays = collections.Counter()
    for s, l in labs.items():
        if l["community"] == k:
            p = phase_of(recs[s]["date"], ph)
            cnt[p] += 1
            plays[p] += recs[s]["plays"]
    counts = "\n".join(f"- {p['id']}（{p['name']}、{p['start']}〜{p['end']}）: {cnt.get(p['id'], 0)}本、再生計 {fmt_plays(plays.get(p['id'], 0))}"
                       for p in ph)
    va = {s: v for s, v in video_analyses(a).items() if v.get("community") == k}
    analyses = "\n\n".join(f"#### {vline(recs[s])}\n```json\n{json.dumps(v, ensure_ascii=False)}\n```" for s, v in sorted(va.items()))
    return {"community": k, "community_def": pr._first_sentence(tax["community"].get(k, "")), "i": t["params"]["i"],
            "n": t["params"]["n"], "counts": counts, "n_videos": len(va), "analyses": analyses or "（この界隈の代表のコメントは無い）",
            "settings": _settings(a, ["comment_lens"], t)}


# --- 界隈ごとのコメント分析（2026-10-03〜。1本ずつの分析と統合をまとめた） ---
def comment_seqs(a) -> dict:
    """界隈 → コメントの取れた動画（日付順）。界隈が unknown のものは除く"""
    recs = records(a)
    got = fetched(a)
    out = collections.defaultdict(list)
    for s, l in labels(a).items():
        r = recs.get(s)
        if not r or l["community"] == "unknown" or r["video_id"] not in got:
            continue
        if a.derived("comments", f"{s}_{r['video_id']}.md").exists():
            out[l["community"]].append(s)
    return {k: sorted(v, key=lambda s: recs[s]["date"]) for k, v in out.items()}


def community_digest(a, k, top_n=None, mark=None) -> list:
    """read の ccomments:<界隈>: 動画ごとに、見出しと、いいねの多いコメント（上位 digest_top 件）。
    掘り下げ（dcomments:<界隈>）では top_n を増やし、今回取り足した動画（mark の seq）に印を付ける"""
    c = cfg(a)
    top_n = top_n or c["digest_top"]
    mark = mark or set()
    recs = records(a)
    ph = phases(a)
    units = []
    for s in comment_seqs(a).get(k, []):
        text, _ = a.comment_text(s)
        lines = text.splitlines()
        head = [ln for ln in lines[:12] if ln.startswith(("- 投稿者", "- 再生", "- 説明文"))]
        head = [ln if len(ln) < 160 else ln[:160] + "…" for ln in head]
        top = [ln for ln in lines if ln.startswith("- [")][:top_n]
        top = [ln if len(ln) <= c["digest_chars"] else ln[:c["digest_chars"]] + "…" for ln in top]
        r = recs[s]
        units.append(f"### seq {s}（{r['date']}、段階 {phase_of(r['date'], ph) or '?'}）" + ("【取り足し】" if s in mark else "") +
                     "\n" + "\n".join(head) + "\n" + "\n".join(top) + "\n\n")
    return units


def community_materials(a, k) -> dict:
    tax = pr._taxonomy(a)
    recs = records(a)
    labs = labels(a)
    ph = phases(a)
    got = fetched(a)
    seqs = comment_seqs(a).get(k, [])
    cnt, cv, cc = collections.Counter(), collections.Counter(), collections.Counter()
    for s, l in labs.items():
        if l["community"] == k:
            cnt[phase_of(recs[s]["date"], ph)] += 1
    lang = collections.Counter()
    for s in seqs:
        p_ = phase_of(recs[s]["date"], ph)
        cv[p_] += 1
        cs = got[recs[s]["video_id"]].get("comments") or []
        cc[p_] += len(cs)
        lang.update((x.get("comment_language") or "?") for x in cs)
    counts = "\n".join(f"- {p['id']}（{p['name']}、{p['start']}〜{p['end']}）: この界隈の動画 {cnt.get(p['id'], 0)}本"
                       f"・うちコメントを読める動画 {cv.get(p['id'], 0)}本（コメント {cc.get(p['id'], 0)}件）" for p in ph)
    tot = sum(lang.values()) or 1
    if lang and set(lang) != {"?"}:
        counts += "\n- コメントの言語: " + "、".join(f"{lg} {n * 100 // tot}%" for lg, n in lang.most_common(6))
    pages = pr._paginate(community_digest(a, k)) or ["（この界隈にはコメントを読める動画が無い）"]
    return {"community": k, "community_def": pr._first_sentence(tax["community"].get(k, "")), "counts": counts,
            "digest": pages[0], "n_pages": len(pages)}


def music_comments(a) -> list:
    """音・曲・原作に触れたコメント（全動画から、いいね順に20件）"""
    recs = records(a)
    by_vid = {r["video_id"]: s for s, r in recs.items()}
    hits = []
    for vid, r in fetched(a).items():
        for c_ in r.get("comments") or []:
            t = c_.get("text") or ""
            if re.search(r"曲|サビ|歌|音|リズム|メロ|OP|オープニング|アニメ|song|music|beat|lyrics", t, re.I):
                hits.append((int(c_.get("digg_count") or c_.get("likes") or 0), by_vid.get(vid), c_))
    hits.sort(key=lambda x: -x[0])
    return [f"- seq {s}: 『{c_.get('text', '')[:80]}』（{n} いいね、cid {c_.get('cid')}）" for n, s, c_ in hits[:20] if s is not None]


# --- 完成後の界隈の掘り下げ（2026-10-06〜。docs/DEEPEN_COMMUNITY.md） ---
# 道具 deepen が仕事の列の末尾に足す: dcomments → outline_revise →（write×）→ review →（write×・finish×・finish_title）
# → assemble → verify → export → done。取り足しの取得は acquire/deepen.py（終わるまで next_task は待ちを返す）
def deepen_tasks(st: dict, job: dict) -> list:
    pm = {"community": job["community"], "round": job["round"], "instruction": job["instruction"]}
    k = job["community"]
    return [_task(st, "dcomments", "ai", f"界隈の掘り下げ（{k}）: コメント分析のやり直し", pm),
            _task(st, "outline_revise", "ai", f"界隈の掘り下げ（{k}）: 構成案の直し", pm),
            _task(st, "review", "ai", f"界隈の掘り下げ（{k}）: 全章の通し読み", {**pm, "changed": [], "thesis_changed": False}),
            _task(st, "assemble", "service", "レポートの組み立て（サービス）"),
            _task(st, "verify", "service", "検算（サービス）"),
            _task(st, "export", "service", "Excel 用のデータ（サービス）"),
            _task(st, "done", "done", f"完了（界隈の掘り下げ: {k}）", {"deepen": job["round"]})]


def deepen_job(a, n=None) -> dict:
    """掘り下げの回（analysis.json の deepen。前の回は deepen_history）"""
    cur = a.meta.get("deepen") or {}
    if n is None or cur.get("round") == n:
        return cur
    return next((h for h in a.meta.get("deepen_history") or [] if h.get("round") == n), {})


def deepen_seqs(a, n) -> set:
    """その回に取り足した動画の seq"""
    by_vid = {str(r["video_id"]): s for s, r in records(a).items()}
    return {by_vid[v] for v, r in fetched(a).items() if r.get("deepen_round") == n and v in by_vid}


def chapters_with_community(a, k, st) -> list:
    """その界隈の動画（seq）を名指ししている章"""
    ks = {s for s, l in labels(a).items() if l["community"] == k}
    out = []
    for ch in chapter_order(a, st):
        p = a.outputs("chapters", f"{ch}.md")
        if p.exists() and {int(x) for x in re.findall(r"seq\s*(\d+)", p.read_text(encoding="utf-8"))} & ks:
            out.append(ch)
    return out


def fetched_note(a, n) -> str:
    """その回の取り足しの結果（AI と利用者に伝える一言）"""
    job = deepen_job(a, n)
    res = job.get("result") or {}
    pl = job.get("plan") or {}
    if not job.get("targets"):
        return "取り足せる動画は無かった（この界隈のコメントの無い動画・続きのある動画が無い）。手元のコメントを、前回より深く読み直す"
    if job.get("status") == "failed" and not res:
        return f"取得が止まり、取り足せなかった（{job.get('error') or '理由不明'}）。手元のコメントを、前回より深く読み直す"
    s = (f"新しく {res.get('videos_new', 0)}本・続き {res.get('videos_more', 0)}本、コメント {res.get('comments_added', 0)}件"
         f"（予定は新しく{pl.get('n_new', 0)}本・続き{pl.get('n_more', 0)}本）")
    if res.get("unreachable"):
        s += f"。楽曲ページの一覧で見つからなかった動画 {res['unreachable']}本は取れていない"
    if res.get("blocked") or res.get("error"):
        s += "。取得は途中で止まった（取れた分だけ使う）"
    return s


def deepen_materials(a, t, st) -> dict:
    k, n = t["params"]["community"], t["params"]["round"]
    c = cfg(a)
    mat = community_materials(a, k)
    pages = pr._paginate(community_digest(a, k, c["deepen_digest_top"], deepen_seqs(a, n))) or \
        ["（この界隈にはコメントを読める動画が無い）"]
    prev = a.outputs("synthesis", f"{k}.md")
    chs = chapters_with_community(a, k, st)
    return {"community": k, "community_def": mat["community_def"], "counts": mat["counts"], "instruction": t["params"]["instruction"],
            "fetched": fetched_note(a, n),
            "previous": prev.read_text(encoding="utf-8").strip() if prev.exists() else "（前回の分析は無い。コメントを読める動画が無かった界隈）",
            "chapters_with": "\n".join(f"- `{ch}`: {first_line(a.outputs('chapters', f'{ch}.md'))}" for ch in chs)
                             or "（この界隈の動画を名指しした章は無い）",
            "digest": pages[0], "n_pages": len(pages), "digest_top": c["deepen_digest_top"], "n_full": c["deepen_full_reads"]}


def md_section(md: str, heading: str) -> str:
    """Markdown の「#### 見出し」の節の中身（次の同じ深さか浅い見出しの手前まで）。見出しは前方一致
    （「#### 前回からの変化（3点）」も拾う。受け取りの検査が部分一致なので合わせる）"""
    m = re.search(rf"(?m)^{re.escape(heading)}[^\n]*$", md)
    if not m:
        return ""
    nxt = re.search(r"(?m)^#{1,4} ", md[m.end():])
    return md[m.end():m.end() + nxt.start() if nxt else len(md)].strip()


def deepen_record(a, n) -> Path:
    return a.outputs("revisions", f"deepen_r{n}.json")


def deepen_summary(a, n) -> str:
    """完了の知らせの頭: 何を取り足し、レポートの何を変えたか（返答の 1 に使う）"""
    job = deepen_job(a, n)
    rec = pr._read_json(deepen_record(a, n), {}) or {}
    chs = rec.get("changed_all") or [x["chapter"] for x in rec.get("changed") or []]
    lines = [f"#### 今回の直し（界隈の掘り下げ: {job.get('community')}）",
             f"- 利用者の頼み: {job.get('instruction', '')}",
             f"- 取り足したコメント: {fetched_note(a, n)}",
             "- 書き直した章: " + ("、".join(first_line(a.outputs("chapters", f"{c_}.md")) for c_ in chs) or "なし")]
    if rec.get("outline_note"):
        lines.append(f"- 構成案の変更: {rec['outline_note']}" + ("（記事全体の主張も変えた）" if rec.get("thesis_changed") else ""))
    if rec.get("review_note"):
        lines.append(f"- 通し読み: {rec['review_note']}")
    lines.append("- 返答の 1 では、拡散の流れの代わりに、この直しで何が分かり何を変えたかを1〜3文で伝える。前の版は残してある")
    return "\n".join(lines)


# --- 完成後の界隈の切り直し（2026-10-06〜。docs/RECUT_COMMUNITY.md） ---
# 道具 recut が仕事 recut（AI: 分類軸を利用者の指示どおりに直す）を末尾に足す。受け付けたら前の版を outputs/history/recut_r<回>/ に写し、
# label×（顔ぶれが変わりうる界隈の動画だけ）→ recut_check［service］→ phases → plan を足す。plan から先はふつうの流れ
# （界隈ごとのコメント分析 → 構成案 → 執筆 → 仕上げ → 完了）。参考記事の章立ては界隈に依らないので使い回す
RECUT_CLEAR_DIRS = ("labels", "synthesis", "chapters", "note_chapters")
RECUT_CLEAR_FILES = ("labels.tsv", "phases.json", "pathway.md", "outline.json", "outline.md", "REPORT.md", "NOTE_BODY.md",
                     "EDITOR_NOTES.md", "note_meta.json", "verify.json")


def recut_record_path(a, n) -> Path:
    return a.outputs("recut", f"r{n}.json")


def recut_record(a, n) -> dict:
    return pr._read_json(recut_record_path(a, n), {}) or {}


def recut_last_round(a) -> int:
    """済んだ（分類軸を受け付けた）切り直しの回。無ければ 0"""
    d = a.outputs("recut")
    ns = [int(m.group(1)) for p in d.glob("r*.json") if (m := re.fullmatch(r"r(\d+)", p.stem))] if d.exists() else []
    return max(ns, default=0)


def recut_prev_dir(a, n) -> Path:
    """n 回目の切り直しの前の版の写し"""
    return a.outputs("history", f"recut_r{n}")


def keep_version(a, kind: str, n, replace: bool = False) -> Path:
    """書き直す前の版（outputs の全部。prompts・history・ZIP の作業場は除く）を outputs/history/<kind>_r<n>/ に写す。
    切り直し（recut）と掘り下げ（deepen）の受け付けで使う。replace=False なら、もう写してあれば写し直さない（同じ回の2回目は後の版になるため）"""
    prev = a.outputs("history", f"{kind}_r{n}")
    if prev.exists():
        if not replace:
            return prev
        shutil.rmtree(prev)
    top = a.outputs()
    shutil.copytree(top, prev, ignore=lambda d, names: [x for x in names if Path(d) == top and x in ("history", "prompts", "_zip")])
    return prev


def _labels_at(p: Path) -> dict:
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {int(r["seq"]): r for r in csv.DictReader(f, delimiter="\t")}


def recut_overview(a) -> str:
    """今の界隈ごとの本数・コメントのある動画・初出・再生上位（切り直しの指示書の材料）"""
    tax = pr._taxonomy(a) or {}
    recs, labs, ok = records(a), labels(a), fetched(a)
    by_c = collections.defaultdict(list)
    for s, l in labs.items():
        if s in recs:
            by_c[l["community"]].append(recs[s])
    lines = []
    for k, v in tax.get("community", {}).items():
        if k.startswith("_"):
            continue
        rs = by_c.get(k, [])
        top = sorted(rs, key=lambda r: -r["plays"])[:3]
        lines.append(f"- `{k}`: {pr._first_sentence(v)} — ラベル {len(rs)}本（コメントのある動画 "
                     f"{sum(1 for r in rs if str(r['video_id']) in ok)}本）、初出 {min((r['date'] for r in rs), default='—')}。"
                     "再生上位: " + ("、".join(vline(r) for r in top) or "なし"))
    return "\n".join(lines)


def accept_recut(a, t, raw, st) -> list:
    """切り直した分類軸を受け付け、前の版を写してから、ラベルの付け直し・確かめ・段階・以降の準備を足す"""
    obj, err = pr._parse_json(raw)
    if err:
        return [err + '（{"taxonomy": {...}, "keep": {...}, "note_to_user": "..."} の形）']
    if not isinstance(obj, dict) or not isinstance(obj.get("taxonomy"), dict):
        return ['{"taxonomy": {...}, "keep": {...}, "note_to_user": "..."} の形にしてください（taxonomy は分類軸の全体）']
    tax, old = obj["taxonomy"], pr._taxonomy(a) or {}
    old_labs = labels(a)
    errs = pr.check_taxonomy(tax, need_region=False)
    if not errs:
        errs += check_examples(tax, set(old_labs), strict=False)
    keep = obj.get("keep") or {}
    if not isinstance(keep, dict):
        errs.append("keep は {前の界隈の key: 新しい界隈の key} の形にしてください（無ければ {}）")
        keep = {}
    old_c = {k for k in old.get("community", {}) if not k.startswith("_") and k != "unknown"}
    new_c = ({k for k in tax["community"] if not k.startswith("_") and k != "unknown"}
             if isinstance(tax.get("community"), dict) else set())
    bad = [k for k in keep if k not in old_c]
    if bad:
        errs.append(f"keep の左（前の界隈）に無い key: {bad[:5]}（前の界隈: {sorted(old_c)}）")
    bad = [v for v in keep.values() if v not in new_c]
    if bad:
        errs.append(f"keep の右（新しい界隈）に無い key: {bad[:5]}")
    # 1対1で残す界隈は、定義を変えていないこと（分けた・定義を変えた界隈は付け直す）。2つ以上をまとめるときは定義が変わってよい
    for k, v in keep.items():
        if k in old_c and v in new_c and list(keep.values()).count(v) == 1 and tax["community"][v] != old["community"][k]:
            errs.append(f"keep の `{k}` → `{v}` は定義が変わっています。分けた・定義を変えた界隈は keep に入れない（動画を付け直す）。"
                        "key を付け替えるだけなら、定義は前のまま写す")
    if not str(obj.get("note_to_user") or "").strip():
        errs.append("note_to_user（どう切り直したか。1〜2文）を入れてください")
    if not errs and json.dumps(tax, sort_keys=True, ensure_ascii=False) == json.dumps(old, sort_keys=True, ensure_ascii=False):
        errs.append("分類軸が前と同じです。利用者の指示どおりに界隈を直してください")
    if errs:
        return errs[:20]
    n = t["params"]["round"]
    # format・motive・tier を変えたら、前のラベルは使えない（全部付け直す）
    same_other = all(json.dumps(tax.get(ax), sort_keys=True) == json.dumps(old.get(ax), sort_keys=True)
                     for ax in ("format", "motive", "tier"))
    if not same_other:
        keep = {}
    if "examples" not in tax and old.get("examples"):   # 代表例は残した界隈の分だけ引き継ぐ（まとめた界隈は合わせる）
        ex = {}
        for k, v in old["examples"].items():
            if k in keep:
                ex.setdefault(keep[k], []).extend(x for x in v if x not in ex.get(keep[k], []))
        tax["examples"] = ex
    # 前の版を写してから、作り直すものを片付ける（参考記事・直しの記録・Excel 用 ZIP は残す）
    keep_version(a, "recut", n, replace=True)
    for dd in RECUT_CLEAR_DIRS:
        shutil.rmtree(a.outputs(dd), ignore_errors=True)
    for f in RECUT_CLEAR_FILES:
        a.outputs(f).unlink(missing_ok=True)
    pr._write_json(a.outputs("taxonomy.json"), tax)
    # 残した界隈（keep）の動画は前のラベルのまま（key だけ付け替える）。ほかは付け直す
    kept = {s: {**r, "community": keep[r["community"]]} for s, r in old_labs.items() if r.get("community") in keep}
    head = ["seq", "community", "format", "motive", "tier", "conf", "reason"]
    pr._write_text(a.outputs("labels", "batch_00.tsv"),
                   "\t".join(head) + "\n" + "".join("\t".join(str(r.get(h, "")) for h in head) + "\n" for _, r in sorted(kept.items())))
    merge_labels(a)
    relabel = sorted((set(label_seqs(a)) | set(old_labs)) - set(kept))
    c = cfg(a)
    batches = [relabel[i:i + c["label_batch"]] for i in range(0, len(relabel), c["label_batch"])]
    new = [_task(st, "label", "ai", f"ラベルの付け直し（{i}/{len(batches)}）", {"batch": i, "n_batches": len(batches), "seqs": b, "recut": n})
           for i, b in enumerate(batches, 1)]
    new += [_task(st, "recut_check", "service", "切り直した界隈のコメントが足りるかの確かめ（サービス）", {"round": n}),
            _task(st, "phases", "ai", "段階の区切りと拡散経路の下書き（切り直した界隈で）", {"recut": n}),
            _task(st, "plan", "service", "以降の仕事の準備（サービス）", {"recut": n})]
    insert_after(st, t, new)
    pr._write_json(recut_record_path(a, n), {
        "round": n, "instruction": t["params"]["instruction"], "note_to_user": str(obj["note_to_user"]).strip(), "keep": keep,
        "old_communities": sorted(old_c), "new_communities": sorted(new_c), "kept_labels": len(kept), "relabel": len(relabel),
        "other_axes_changed": not same_other, "at": pr._now()})
    return []


def service_recut_check(a, t, st) -> dict:
    """切り直した界隈ごとに、必ず読みたい動画（acquire/recut.py の RULE）にコメントがあるか。無ければ取り足しを積む
    （積んだら next_task は待ちを返して止まる。取り終わって利用者が「続けて」と言うと、段階から先へ進む）"""
    from acquire import recut as rc
    n = t["params"]["round"]
    rec = recut_record(a, n)

    def groups(labs):
        g = collections.defaultdict(set)
        for s, l in labs.items():
            if l.get("community") != "unknown":
                g[l["community"]].add(s)
        return g

    og, ng = groups(_labels_at(recut_prev_dir(a, n) / "labels.tsv")), groups(labels(a))
    keep = rec.get("keep") or {}

    def before(k):   # key を付け替えた・そのまままとめた界隈は、元の界隈の顔ぶれと比べる
        olds = [o for o, v in keep.items() if v == k]
        return set().union(*(og.get(o, set()) for o in olds)) if olds else og.get(k)

    changed = sorted(k for k in ng if ng[k] != before(k))
    pl = rc.plan(a.dir, None if rc.RULE["scope"] == "all" else changed)
    rec.update({"changed": changed, "removed": sorted(k for k in og if k not in ng), "scope": rc.RULE["scope"],
                "check": {k: {"labeled": v["labeled"], "with_comments": v["with_comments"], "have": len(v["have"]),
                              "need": v["need"], "unreachable": v["unreachable"]} for k, v in pl["communities"].items()},
                "n_targets": len(pl["targets"]), "est_min": pl["est_min"], "checked_at": pr._now()})
    if pl["targets"]:
        rec["acq_round"] = rc.request(a.dir, rec.get("instruction", ""), pl, n)["round"]
    pr._write_json(recut_record_path(a, n), rec)
    return {"changed": len(changed), "targets": len(pl["targets"]), "est_min": pl["est_min"]}


def recut_short_targets(a, n) -> str:
    """取り足す界隈と本数（待ちの返事・完了の知らせに使う）"""
    rec = recut_record(a, n)
    return "、".join(f"`{k}` {len(v['need'])}本" for k, v in (rec.get("check") or {}).items() if v.get("need"))


def recut_fetched_note(a, n) -> str:
    rec = recut_record(a, n)
    # 読むべき動画のうち、前の取得で「動画が無い・コメントが無い・開けない」や一覧に見つからなかったもの（取りに行かなかった）
    lost = sum(len(v.get("unreachable") or []) for v in (rec.get("check") or {}).values())
    lost_s = f"読むべき動画のうち {lost}本は、削除済み・楽曲ページの一覧に見つからないなどで取りに行けなかった" if lost else ""
    if not rec.get("acq_round"):
        if lost:
            return f"要らなかった（取りに行ける読むべき動画には、もうコメントがあった。{lost_s}）"
        return "要らなかった（切り直した界隈の、読むべき動画にはコメントがあった）"
    job = deepen_job(a, rec["acq_round"])
    res = job.get("result") or {}
    if job.get("status") == "failed" and not res:
        return f"取得が止まり、取り足せなかった（{job.get('error') or '理由不明'}）。手元のコメントで書いた"
    s = f"{res.get('videos_new', 0)}本・コメント {res.get('comments_added', 0)}件（予定 {len(job.get('targets') or [])}本: {recut_short_targets(a, n)}）"
    if res.get("unreachable"):
        s += f"。楽曲ページの一覧で見つからなかった動画 {res['unreachable']}本は取れていない"
    if res.get("blocked") or res.get("error"):
        s += "。取得は途中で止まった（取れた分だけ使った）"
    if lost:
        s += f"。ほかに、{lost_s}"
    return s


def confirm_skipped_summary(a) -> str:
    """利用者が界隈の確認を省いたとき（proto_runner._auto_confirm）、完了の知らせで使った界隈を伝える。止めずに、何で進めたかは伝える"""
    c = pr._read_json(a.outputs("confirm_answer.json"), {}) or {}
    if not c.get("auto"):
        return ""
    tax = pr._taxonomy(a) or {}
    names = [pr._first_sentence(v, 40).rstrip("。") for k, v in tax.get("community", {}).items() if not k.startswith("_") and k != "unknown"]
    return "\n".join(["#### 界隈の確認を省いた（利用者の頼み）",
                      f"- 使った界隈（{len(names)}個）: " + "、".join(names),
                      "- 返答の 1 のあとに、界隈の確認を省いて AI の案のまま書いたことと、使った界隈の名前を1行で伝え、"
                      f"「分け方を変えたいときは『{a.title}のレポートの〇〇界隈を2つに分けて』のように言えば、界隈の切り直しで書き直せます」と添える"])


def recut_summary(a, n) -> str:
    """完了の知らせの頭: どう切り直し、何を取り足したか（返答の 1 に使う）"""
    rec = recut_record(a, n)
    tax = pr._taxonomy(a) or {}
    names = [f"{pr._first_sentence(v, 40)}（`{k}`）" for k, v in tax.get("community", {}).items()
             if not k.startswith("_") and k != "unknown"]
    lines = ["#### 今回の直し（界隈の切り直し）",
             f"- 利用者の頼み: {rec.get('instruction', '')}",
             f"- 切り直し: {rec.get('note_to_user', '')}",
             f"- 新しい界隈（{len(names)}個）: " + "、".join(names),
             "- 顔ぶれが変わった界隈: " + ("、".join(f"`{k}`" for k in rec.get("changed") or []) or "なし") +
             ("（なくなった界隈: " + "、".join(f"`{k}`" for k in rec["removed"]) + "）" if rec.get("removed") else ""),
             f"- コメントの取り足し: {recut_fetched_note(a, n)}",
             "- 返答の 1 では、拡散の流れに加えて、界隈の分け方を変えて何が見えるようになったかを1〜3文で伝える。前の版は残してある"]
    return "\n".join(lines)


def recut_write_note(a, n, ch) -> str:
    """切り直しのあとの執筆に添える: 利用者の頼み・前の版の章の読み方・前の版で利用者が頼んだ直し"""
    rec = recut_record(a, n)
    revs = []
    d = a.outputs("revisions")
    for p in sorted(d.glob("[0-9][0-9][0-9].json")) if d.exists() else []:
        r = pr._read_json(p, {}) or {}
        if r.get("instruction"):
            revs.append(f"- 「{str(r['instruction'])[:300]}」（前の版の `{r.get('chapter')}` を直した）")
    s = ("#### 界隈の切り直し（この版を書く理由）\n"
         f"- 利用者の頼み: {rec.get('instruction', '')}\n- 切り直し: {rec.get('note_to_user', '')}\n"
         f"- 前の版の同じ章は read の `prev:{ch}` で読める（あれば）。界隈の分け方が古いので、構成・界隈の話・界隈の名前は写さない。"
         "前の版にしかない利用者の考察や、界隈に関わらない事実の書き方を活かすときだけ使う")
    if revs:
        s += "\n- 前の版で利用者が頼んだ直し（新しい版でも当てはまるものは活かす）:\n" + "\n".join(revs)
    return s


# --- 構成案（2026-10-03〜） ---
# --- ウェブで調べる・時代背景の材料（2026-10-06〜。docs/WEB_RESEARCH.md）---
# ユーザー「『ここは人が…』の箇所を極限まで無くしたい」「時代背景は、似た位置付けの楽曲のレポートをいくつか参照して」
# 「TikTok 外の指標も YouTube の MV 再生数・サブスク・チャート・ライブ動員くらい AI が調べて足せる」
RESEARCH_KINDS = {"song": ("fact",), "outside": ("metric", "value", "as_of"), "people": ("who", "fact")}
RESEARCH_HEADS = {"song": "曲の情報", "outside": "TikTok の外の指標", "people": "要のアカウントの素性"}
ERA_HEADS = ("### この曲の型", "### 似た位置づけの曲", "### 同じ時期の流行", "### この曲の立ち位置")


def research(a):
    """ウェブで調べたこと（research.json）。この工程が無かった分析（前の形）は None"""
    return pr._read_json(a.outputs("research.json"), None)


def research_ids(a) -> set:
    return {int(it["id"][1:]) for it in (research(a) or {}).get("items") or []}


def research_ready(a) -> bool:
    return a.outputs("research.json").exists() and a.outputs("era.md").exists()


def research_line(it: dict) -> str:
    if it["kind"] == "outside":
        body = f"{it['metric']}: {it['value']}（{it['as_of']} 時点" + (f"。{it['note']}" if it.get("note") else "") + "）"
    elif it["kind"] == "people":
        body = f"{it['who']}: {it['fact']}" + (f"（{it['note']}）" if it.get("note") else "")
    else:
        body = it["fact"] + (f"（{it['note']}）" if it.get("note") else "")
    src = " ".join(x for x in (it.get("source_name"), it.get("source")) if x)
    return f"[{it['id']}] {body} — 出どころ: {src}"


def research_md(res: dict) -> str:
    """read の `research`（構成案・執筆が読む）"""
    out = [f"## ウェブで調べたこと（{str(res.get('at', ''))[:10]}、AI が調べた）", ""]
    if not res.get("web_search"):
        return "\n".join(out + ["AI のウェブ検索が使えなかったため、調べていない（記事は TikTok の中のデータだけで書く）"]) + "\n"
    for kind, head in RESEARCH_HEADS.items():
        xs = [it for it in res["items"] if it["kind"] == kind]
        out += [f"### {head}", ""] + ([f"- {research_line(it)}" for it in xs] or ["（なし）"]) + [""]
    if res.get("not_found"):
        out += ["### 探したが見つからなかったもの", ""] + [f"- {x}" for x in res["not_found"]] + [""]
    return "\n".join(out)


def research_block(a, kinds, head) -> str:
    """執筆の材料: ウェブで調べたことのうち、その章で使う種類（見つからなかったものは渡さない。無いことを本文に書かせないため）"""
    res = research(a)
    if not res:
        return f"#### {head}\n（ウェブで調べる工程が無い分析。TikTok の中のデータだけで書き、外のことには触れない）"
    if not res.get("web_search"):
        return f"#### {head}\n（AI のウェブ検索が使えず、調べていない。TikTok の中のデータだけで書き、外のことには触れない）"
    xs = [it for it in res["items"] if it["kind"] in kinds]
    return (f"#### {head}（{str(res.get('at', ''))[:10]} にウェブで調べた。使ったら文末に [W番号]）\n" +
            ("\n".join(f"- {research_line(it)}" for it in xs) or "（見つかったものは無い。外のことには触れない）"))


def key_accounts(a, n=25) -> list:
    """ウェブで調べる材料: 経路の要の動画を投稿した、公に活動していそうなアカウント（本人・公式・認証・フォロワー10万以上・大手）"""
    recs, labs, pl = records(a), labels(a), pool(a)
    ov = (pr._read_json(a.outputs("phases.json"), {}) or {}).get("overlooked") or []
    seqs = {s for p in phases(a) for s in p.get("reps") or []} | set(ov)
    seqs |= set(sorted(recs, key=lambda s: -recs[s]["plays"])[:20])
    seqs |= {s for s, l in labs.items() if l.get("tier") in ("official_artist", "official_brand")}
    seqs |= {s for s, r in recs.items() if re.search(r"artist|official", (pl.get(r["video_id"]) or {}).get("reasons", ""))}
    out, seen = [], set()
    for s in sorted((s for s in seqs if s in recs), key=lambda s: -recs[s]["plays"]):
        au = recs[s].get("author") or {}
        aid = au.get("id")
        if not aid or aid in seen:
            continue
        tier = (labs.get(s) or {}).get("tier", "")
        fol = au.get("followers") if isinstance(au.get("followers"), int) else 0
        if not (au.get("verified") or fol >= 100000 or tier in ("official_artist", "official_brand", "large_creator")):
            continue
        seen.add(aid)
        bio = re.sub(r"\s+", " ", au.get("bio") or "")[:80]
        out.append(f"- @{aid}（{au.get('nickname') or '-'}、フォロワー {fol:,}{'、認証あり' if au.get('verified') else ''}、"
                   f"界隈 {(labs.get(s) or {}).get('community', '-')}）" + (f" bio: {bio}" if bio else "") + f" 代表の投稿: {vline(recs[s])}")
        if len(out) >= n:
            break
    return out


def research_materials(a) -> str:
    s = a.meta.get("song") or {}
    recs = records(a)
    rel = release_info(a)
    first = min(recs.values(), key=lambda r: r["date"]) if recs else None
    u = ugc_total(a)
    stick = collections.Counter(x for r in recs.values() for x in (r.get("sticker_texts") or []))
    return (f"#### 曲\n- 曲名: {s.get('title', '')}／アーティスト: {s.get('artist', '')}\n" +
            (f"- TikTok の楽曲ページが作られた日: {rel['date']}\n" if rel else "") +
            (f"- TikTok で最初の投稿: {first['date']}\n" if first else "") +
            (f"- TikTok の UGC 数: {fmt_count(u['n'])}（{u['at']} 時点）\n" if u else "") +
            f"- 分析の時点: {datetime.date.today().isoformat()}\n"
            "\n#### よく使われた部分（動画の画面に載った文字の上位。歌詞の構成を調べる手掛かり）\n" +
            ("\n".join(f"- {w[:40]}（{n}本）" for w, n in stick.most_common(10)) or "（なし）") +
            "\n\n#### 要のアカウント（公に活動していそうなもの。素性を調べてよいのは、この中の本人・公式・芸能人・事務所所属のタレントだけ）\n" +
            ("\n".join(key_accounts(a)) or "（なし）") +
            "\n\n拡散の流れは read の `pathway`（拡散経路の下書き）、界隈ごとの反応は `synthesis`。")


def glossary_types() -> str:
    """著者のバズ曲の分類（用語集の B 章「バズの定義と分類」。新しい記事から足した語は read の kb:glossary の末尾）"""
    p = KB_DIR / "distilled" / "GLOSSARY.md"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    m = re.search(r"(?ms)^## B\..*?(?=^## )", text)
    return m.group(0).strip() if m else "（用語集が読めない。read の kb:glossary の B 章）"


def era_materials(a) -> str:
    """時代背景の材料づくりに渡すもの: 分析の要約・使われ方・調べた曲の情報・分類・楽曲分析の記事の一覧・同じ時期の月報"""
    cs = cards(a)
    rel = release_info(a)
    recs = records(a)
    lo = rel["date"] if rel else min(r["date"] for r in recs.values())
    hi = datetime.date.today().isoformat()
    monthly = sorted((c_ for c_ in cs if c_.get("kind") == "流行曲Report"), key=lambda c_: c_["date"])
    near = [c_ for c_ in monthly if lo <= c_["date"] <= hi]
    if len(near) < 2:   # 新しい曲で月報がまだ無い: 直前のものを足す
        near = [c_ for c_ in monthly if c_["date"] < lo][-(3 - len(near)):] + near
    songs = [c_ for c_ in cs if c_.get("kind") == "楽曲分析"]
    def card(c_):
        return (f"- `{c_['file'].replace('.md', '')}`（{c_.get('date')}）{c_.get('song') or c_.get('title')} / {c_.get('artist') or '-'}"
                f" — 型: {c_.get('buzz_type') or '-'}。経路: {str(c_.get('pathway') or '-')[:80]}")
    return ("#### この曲の分析の要約\n" + analysis_summary(a)[:6000] + "\n\n" + music_materials(a) + "\n\n" +
            research_block(a, ("song",), "ウェブで調べた曲の情報") +
            "\n\n#### 著者のバズ曲の分類（用語集の B 章）\n" + glossary_types() +
            "\n\n#### 楽曲分析の記事の一覧（似た位置づけの曲を選ぶ。読むときは `note:<名前>`）\n" + "\n".join(card(c_) for c_ in songs) +
            f"\n\n#### 同じ時期の月報（この曲の公開 {lo} から分析の時点まで。足りなければ直前のもの。読むときは `note:<名前>`）\n" +
            ("\n".join(f"- `{c_['file'].replace('.md', '')}`（{c_.get('date')}）{c_.get('title')}: {str(c_.get('song') or '')[:120]}"
                       for c_ in near) or "（なし）"))


def era_text(a) -> str:
    p = a.outputs("era.md")
    return p.read_text(encoding="utf-8").strip() if p.exists() else "（時代背景の材料は無い。前の形の分析）"


def _section_loose(md: str, head: str) -> str:
    """「### 見出し（添え書き）」も拾う md_section"""
    m = re.search(rf"(?m)^{re.escape(head)}.*$", md)
    if not m:
        return ""
    nxt = re.search(r"(?m)^#{1,3} ", md[m.end():])
    return md[m.end():m.end() + nxt.start() if nxt else len(md)].strip()


def accept_research(a, raw: str) -> list:
    obj, err = pr._parse_json(raw)
    if err:
        return [err]
    if not isinstance(obj, dict) or not isinstance(obj.get("web_search"), bool):
        return ['{"web_search": true, "song": [...], "outside": [...], "people": [...], "not_found": [...]} の形にしてください'
                '（ウェブ検索が使えないなら {"web_search": false} だけ）']
    nf = [str(x).strip() for x in obj.get("not_found") or [] if str(x).strip()] if isinstance(obj.get("not_found"), list) else []
    items, errs = [], []
    if obj["web_search"]:
        for kind, need in RESEARCH_KINDS.items():
            xs = obj.get(kind) or []
            if not isinstance(xs, list):
                errs.append(f"{kind} は配列にしてください")
                continue
            for i, x in enumerate(xs, 1):
                if not isinstance(x, dict):
                    errs.append(f"{kind} の {i} 件目は {{...}} の形に")
                    continue
                it = {"kind": kind, **{k: str(v).strip() for k, v in x.items()
                                       if k not in ("kind", "id") and isinstance(v, (str, int, float)) and str(v).strip()}}
                miss = [k for k in need if not it.get(k)]
                if miss:
                    errs.append(f"{kind} の {i} 件目に {'・'.join(miss)} がありません")
                if not re.fullmatch(r"https?://\S+", it.get("source", "")):
                    errs.append(f"{kind} の {i} 件目の source（出どころの URL を1つ）がありません")
                if kind == "outside" and it.get("as_of") and not re.search(r"\d{4}", it["as_of"]):
                    errs.append(f"outside の {i} 件目の as_of は値の時点（YYYY-MM-DD）に")
                items.append(it)
        if not items and not nf:
            errs.append("調べたことが1件もありません。探して見つからなかったなら、何を探したかを not_found に"
                        "（ウェブ検索が使えないなら web_search を false に）")
        if len(items) > 60:
            errs.append(f"{len(items)} 件あります。記事に使えるものを 40 件までに")
    if errs:
        return errs[:20]
    for i, it in enumerate(items, 1):
        it["id"] = f"W{i}"
    res = {"web_search": obj["web_search"], "at": pr._now(), "items": items, "not_found": nf}
    pr._write_json(a.outputs("research.json"), res)
    pr._write_text(a.outputs("research.md"), research_md(res))
    return []


def accept_era(a, raw: str) -> list:
    md = pr._strip_fence(raw).strip()
    errs = [f"見出し `{h}` がありません" for h in ERA_HEADS if h not in md]
    ok = {c_["file"].replace(".md", "") for c_ in cards(a)}
    bad = sorted(f for f in set(KB_FILE_RE.findall(md)) if f not in ok)
    if bad:
        errs.append(f"一覧に無い記事の名前があります: {bad[:5]}（材料の一覧にある名前だけ。分析する曲を扱った記事は使わない）")
    if len(set(KB_FILE_RE.findall(_section_loose(md, "### 似た位置づけの曲")))) < 2:
        errs.append("「似た位置づけの曲」に、読んだ記事の名前（`2025-04-03_n…` の形）を2本以上添えてください")
    if len(md) > 6000:
        errs.append(f"{len(md)} 字あります。3,000 字程度に")
    if errs:
        return errs
    pr._write_text(a.outputs("era.md"), md + "\n")
    return []


def common_materials(a, base) -> str:
    """執筆・構成案の材料の頭: 曲全体の UGC 数を先に。集めた本数は「記事に書かない手掛かり」として"""
    recs = records(a)
    labs = labels(a)
    ph = phases(a)
    u = ugc_total(a)
    total_plays = sum(r["plays"] for r in recs.values())
    if u and u["parts"]:
        head = (f"- **この曲の UGC 数**（同じ曲の楽曲ページ {len(u['parts'])} つの表示の合計、{u['at']} 時点）: {fmt_count(u['n'])}"
                f"（内訳: {ugc_parts_line(u)}）。記事の数字はこの合計で語る（内訳に触れるなら「原曲と sped up 版などを合わせて」のように、"
                "読者に分かる言い方で）" + ("。一部のページは数が読めず、合計に入っていない" if u["partial"] else ""))
    elif u:
        head = f"- **この曲の UGC 数**（楽曲ページの表示、{u['at']} 時点）: {fmt_count(u['n'])}（表示「{u['text']}」）。記事の数字はこれで語る"
    else:
        head = "- この曲の UGC 数: 取得していない（記事では UGC 数に触れず、再生数・期間・界隈の広がりで語る）"
    n_comm = sum(len(v) for v in comment_seqs(a).values())
    rel = release_info(a)
    if rel:
        head += (f"\n- 曲の公開日（楽曲ページが作られた日）: {rel['date']}。これより前の日付の投稿は、あとから音源が付いたものとして"
                 "すべて除いてある。曲がこれより前から使われていたとは書かない")
    return (head + "\n"
            f"- 投稿の期間: {base['period']}\n"
            f"- 経路を調べた手掛かり（**記事の本文に本数を書かない**。付録にサービスが書く）: 楽曲ページに並んだ投稿 {len(recs)}本"
            f"（再生の合計 {fmt_plays(total_plays)}）、そのうち分類した {len(labs)}本、コメントを読んだ {n_comm or len(video_analyses(a))}本\n"
            "- 段階: " + " ／ ".join(f"{p['id']} {p['name']}（{p['start']}〜{p['end']}）" for p in ph))


def analysis_summary(a) -> str:
    """参考記事を選ぶときに渡す、ここまでの分析の要約"""
    tax = pr._taxonomy(a)
    ph = phases(a)
    labs = labels(a)
    recs = records(a)
    first = {}
    for s, l in labs.items():
        k = l["community"]
        if k != "unknown" and (k not in first or recs[s]["date"] < recs[first[k]]["date"]):
            first[k] = s
    comm = "\n".join(f"- {pr._first_sentence(tax['community'].get(k, k))}（初出 {recs[s]['date']}）"
                     for k, s in sorted(first.items(), key=lambda kv: recs[kv[1]]["date"]))
    path = a.outputs("pathway.md").read_text(encoding="utf-8") if a.outputs("pathway.md").exists() else ""
    u = ugc_total(a)
    return ((f"- この曲の UGC 数: {fmt_count(u['n'])}（{u['at']} 時点"
             + (f"。同じ曲の楽曲ページ {len(u['parts'])} つの合計: {ugc_parts_line(u)}" if u["parts"] else "") + "）\n"
             if u else "") +
            "#### 段階\n" + "\n".join(f"- {p['name']}（{p['start']}〜{p['end']}）: {p.get('summary', '')}" for p in ph) +
            "\n\n#### 界隈（初出の順）\n" + comm + "\n\n#### 拡散経路の下書き\n" + path[:4000])


def outline_materials(a, base) -> str:
    ph = phases(a)
    recs = records(a)
    ov = (pr._read_json(a.outputs("phases.json"), {}) or {}).get("overlooked") or []
    top = sorted(recs.values(), key=lambda r: -r["plays"])[:8]
    return (common_materials(a, base) + "\n\n#### 段階の要約と代表\n" +
            "\n".join(f"- {p['id']} {p['name']}: {p.get('summary', '')} 代表: " +
                      "、".join(vline(recs[s]) for s in p.get("reps", []) if s in recs) for p in ph) +
            "\n\n#### 再生数順では落ちるが経路上重要な動画\n" + ("\n".join(f"- {vline(recs[s])}" for s in ov if s in recs) or "（なし）") +
            "\n\n#### 再生上位\n" + "\n".join(f"- {vline(r)}" for r in top) +
            "\n\n" + intro_materials(a) +
            "\n\n" + research_block(a, ("outside",), "ウェブで調べた TikTok の外の指標（曲の情報・人物は read の `research`）") +
            "\n\n#### 時代背景の材料の結論（全体は read の `era`）\n" +
            (_section_loose(era_text(a), "### この曲の型") + "\n" + _section_loose(era_text(a), "### この曲の立ち位置")
             if a.outputs("era.md").exists() else "（無い）"))


def outline(a):
    return pr._read_json(a.outputs("outline.json"))


def outline_md(o: dict, chs: list) -> str:
    """構成案を読む形に（read の outline と、執筆の指示書に使う）"""
    by = {c_["id"]: c_ for c_ in o.get("chapters", [])}
    out = [f"**記事全体の主張**: {o.get('thesis', '')}", f"**題名の案**: {o.get('title_idea', '')}", ""]
    for cid in chs:
        c_ = by.get(cid) or {}
        out.append(f"#### `{cid}` — {c_.get('role', '')}")
        for cl in c_.get("claims") or []:
            ev = "、".join(f"seq {e['seq']}" if "seq" in e else f"cid {e['cid']}" if "cid" in e else
                          f"[{e['web']}]" if "web" in e else str(e.get("number", ""))
                          for e in cl.get("evidence") or [] if isinstance(e, dict))
            out.append(f"- {cl.get('claim', '')}" + ("（推測）" if cl.get("guess") else "") + (f" ／ 根拠: {ev}" if ev else ""))
        if c_.get("bridge"):
            out.append(f"- 次へのつなぎ: {c_['bridge']}")
        out.append("")
    return "\n".join(out)


def plan_block(a, ch, st) -> str:
    """執筆の指示書に入れる構成案: 記事全体の主張・この章の計画・前後の章の役割"""
    o = outline(a)
    if not o:
        return "（構成案は無い。前の形の分析）"
    chs = chapter_order(a, st)
    by = {c_["id"]: c_ for c_ in o.get("chapters", [])}
    me = outline_md({**o, "chapters": [by.get(ch, {"id": ch})]}, [ch]).split("\n", 3)[-1]
    i = chs.index(ch) if ch in chs else -1
    prev = by.get(chs[i - 1]) if i > 0 else None
    nxt = by.get(chs[i + 1]) if 0 <= i < len(chs) - 1 else None
    return (f"- **記事全体の主張**: {o.get('thesis', '')}\n" +
            (f"- 前の章（`{prev['id']}`）の役割: {prev.get('role', '')}。つなぎ: {prev.get('bridge', '')}\n" if prev else "") +
            (f"- 次の章（`{nxt['id']}`）の役割: {nxt.get('role', '')}\n" if nxt else "") +
            "\n**この章の計画**\n\n" + me + "\n構成案の全体は read の `outline`。")


# --- 執筆 ---
def chapter_order(a, st) -> list:
    """レポートの章の並び（冒頭 → 拡大経路（段階ごと）→ 分岐点 → 音楽 → 結果）"""
    return ["intro"] + [f"path_{p['id']}" for p in phases(a)] + REPORT_ORDER_FIXED


def first_line(p: Path) -> str:
    if not p.exists():
        return "（まだ無い）"
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return line.strip()
    return ""


def heading_of(a, ch: str) -> str:
    ph = {p["id"]: p for p in phases(a)}
    if ch == "intro":
        s = a.meta.get("song", {})
        return f"# 【{s.get('title', '')} / {s.get('artist', '')}】Hitの理由分析レポート"
    if ch.startswith("path_"):
        p = ph[ch[5:]]
        n = list(ph).index(p["id"]) + 1
        return f"### ({n}) {p['name']}（{p['start']}〜{p['end']}）"
    return {"branch": "### ここまでの拡大経路をまとめると", "music": "## 3. 楽曲の音楽的特徴（内的要因）",
            "result": "## 6. バズった結果得られたもの"}[ch]


def write_prompt(a, t, base, st):
    ch = t["params"]["chapter"]
    c = cfg(a)
    recs = records(a)
    labs = labels(a)
    ph = phases(a)
    phmap = {p["id"]: p for p in ph}
    cat = []
    first_write = t["params"].get("first", False)
    # 利用者の設定「著者の文体ガイドを使わない」のときは、文体ガイド（kb:style）を読ませず、指示書の「著者の文体で」も外す
    # （用語集は分析の言葉なので読む。2026-10-06 通し試験: 設定の欄と指示が食い違っていた）
    guide = style_guide_on(a)
    style = ("→ `kb:style`", "と文体", "と文体ガイド（`kb:style`）", ", kb:style", "・文体ガイド") if guide else ("", "", "", "", "")
    if a.outputs("outline.json").exists():   # 構成案のある形（2026-10-03〜）: 過去記事は構成案の前に読んだ
        if first_write:
            kb = (f"### 最初に読むもの（執筆の最初の仕事だけ）\n\n著者の分析の言語{style[1]}を身につける: read の `kb:glossary`（全ページ）{style[0]}。"
                  "内容を会話に書き写さない。以降の執筆の仕事では読み直さなくてよい（必要なら読み直してよい）。")
            cat += [pr._catalog_line("kb:glossary", "著者の用語集（A〜J 章）")]
            if guide:
                cat.append(pr._catalog_line("kb:style", "文体ガイド（章立て・定型）"))
        else:
            kb = f"著者の用語集（`kb:glossary`）{style[2]}は最初の執筆の仕事で読んだ。必要なら read で読み直してよい。"
            cat.append(pr._catalog_line(f"kb:glossary{style[3]}", f"用語集{style[4]}（読み直すとき）"))
        cat.append(pr._catalog_line("outline", "構成案の全体"))
    elif first_write:
        kb = ("### 最初に読むもの（執筆の最初の仕事だけ）\n\n"
              f"著者の分析の言語を身につける: read の `kb:readme` → `kb:glossary`（全ページ）{style[0] + ' ' if guide else ''}→ `kb:cards`（全ページ）。\n"
              f"`kb:cards` から、この楽曲に近い過去レポート（同じ型のバズ・似た界隈・似た経路）を**最大{c['extra_past_reports']}本**選び、"
              "`note:<file の .md を除いた名前>` で全文を読む（軸の提案で読んだ `past:1`・`past:2` も読み直してよい）。"
              "選んだ理由は書かなくてよい。内容を会話に書き写さない。\n以降の執筆の仕事では読み直さなくてよい（必要なら読み直してよい）。")
        for nm, d in (("kb:readme", "知識ベースの説明"), ("kb:glossary", "用語集（A〜J 章）"), ("kb:style", "文体ガイド（章立て・定型）"),
                      ("kb:cards", "過去レポート82本のカード（1行1本）"), ("note:<名前>", "過去レポートの全文")):
            if guide or nm != "kb:style":
                cat.append(pr._catalog_line(nm, d))
    else:
        kb = f"著者の用語集（`kb:glossary`）{style[2]}は最初の執筆の仕事で読んだ。必要なら read で読み直してよい。"
        cat.append(pr._catalog_line(f"kb:glossary{style[3]}", f"用語集{style[4]}（読み直すとき）"))
    common = common_materials(a, base)
    if ch.startswith("path_"):
        p = phmap[ch[5:]]
        in_ph = [s for s in labs if phase_of(recs[s]["date"], ph) == p["id"]]
        by_c = collections.defaultdict(list)
        for s in in_ph:
            by_c[labs[s]["community"]].append(s)
        blocks = []
        for k, ss in sorted(by_c.items(), key=lambda kv: min(recs[s]["date"] for s in kv[1])):
            top = sorted(ss, key=lambda s: -recs[s]["plays"])[:4]
            first = min(ss, key=lambda s: recs[s]["date"])
            picks = [first] + [s for s in top if s != first]
            blocks.append(f"- **{k}**: 手掛かりの動画 {len(ss)}本（本数は記事に書かない）。" + "、".join(vline(recs[s]) for s in picks[:4]))
        va = video_analyses(a)
        va_lines = []
        for s in sorted(s for s in va if phase_of(recs[s]["date"], ph) == p["id"]):
            v = va[s]
            q = "／".join(f"『{x.get('text', '')[:60]}』（{x.get('likes')} いいね、cid {x.get('cid')}）" for x in (v.get("quotes") or [])[:3])
            va_lines.append(f"- {vline(recs[s])} [{v.get('community')}] {v.get('why_this_song')} 引用: {q}")
        materials = (f"{common}\n\n#### この段階（{p['id']}）\n- 名前: {p['name']}（{p['start']}〜{p['end']}）\n- 下書きの要約: {p.get('summary')}\n"
                     f"- 下書きの代表: " + "、".join(vline(recs[s]) for s in p.get("reps", []) if s in recs) +
                     f"\n\n#### この段階の界隈（初出の順。最初期・再生上位の動画）\n" + "\n".join(blocks) +
                     (f"\n\n#### この段階の代表動画のコメント分析（{len(va_lines)}本）\n" + "\n".join(va_lines) if va_lines else "") +
                     "\n\n" + research_block(a, ("people",), "ウェブで調べた、要のアカウントの素性（所属・経歴は推測せず、ここにあれば言い切る）") +
                     "\n\n界隈ごとのコメント分析（採用文脈と反応）は read の `synthesis`。拡散経路の下書き全体は `pathway`。")
        cat += [pr._catalog_line("synthesis", "界隈ごとの統合（コメント分析のまとめ）"), pr._catalog_line("pathway", "拡散経路の下書き")]
        spec = ("この段階で何が起きたかを書く。界隈を丸数字で並べ（①…②…）、界隈ごとに**【この界隈に使われた理由（インサイト）】**と"
                "**【動画が伸びた理由】**を対で書く。代表動画は「投稿日＋投稿者＋再生数」を本文に書き、seq を添える。"
                "コメントが示す動機は cid つきで引用する。観測（データで言えること）と推測を分けて書く。")
        if p["id"] == ph[0]["id"]:
            spec = ("**章の頭に** `## 1. バズの拡大経路` の見出しと、文体ガイドの定型の導入（UGC は界隈を渡って広がる → イノベーター理論で分析 → "
                    "ざっくり以下の区切りで分析する、と段階と期間を宣言）を置き、そのあとに見出し `" + heading_of(a, ch) + "` の段階を書く。") + spec
            heading = "## 1. バズの拡大経路"
        else:
            heading = heading_of(a, ch)
        length = "2,000〜5,000字"
    elif ch == "branch":
        ov = (pr._read_json(a.outputs("phases.json"), {}) or {}).get("overlooked") or []
        materials = (f"{common}\n\n#### 段階の要約\n" + "\n".join(f"- {p['id']} {p['name']}: {p.get('summary')}" for p in ph) +
                     "\n\n#### 再生数順では落ちるが経路上重要な動画\n" + ("\n".join(f"- {vline(recs[s])}" for s in ov if s in recs) or "（なし）") +
                     "\n\n" + research_block(a, ("people",), "ウェブで調べた、要のアカウントの素性") +
                     "\n\n拡大経路の章は read の `chapter:path_P1` など、界隈ごとの統合は `synthesis`、下書きは `pathway`。")
        cat += [pr._catalog_line("chapter:path_P1 …", "書き終えた拡大経路の章"), pr._catalog_line("synthesis", "界隈ごとの統合"),
                pr._catalog_line("pathway", "拡散経路の下書き")]
        spec = ("まず `### ここまでの拡大経路をまとめると` で段階の流れを矢印などで短く再掲する。次に `## 2. バズの分岐点とその理由` を立て、"
                "分岐点（0→1、界隈を越えた瞬間、公式の合流など）を3〜6個、`### 分岐点① …` の形で、それぞれ理由をデータで書く。"
                "最後に `### 【重要な補足】この曲で起きなかったこと`（よく言われる通説〔知識ベースの J 章〕がこの曲には当てはまらない点など。"
                "記事に出どころは書かない）。")
        heading, length = heading_of(a, ch), "2,000〜4,000字"
    elif ch == "music":
        materials = (music_materials(a) + "\n\n" + research_block(a, ("song",), "ウェブで調べた曲の情報（作り手・BPM・ジャンル・歌詞の構成・本人の言葉）") +
                     "\n\n#### 時代背景の材料（この曲の型と、似た位置づけの曲・同じ時期の流行。記事の file 名は本文に書かない）\n" + era_text(a) +
                     "\n\n" + common)
        cat += [pr._catalog_line("research", "ウェブで調べたこと（全部）"), pr._catalog_line("era", "時代背景の材料")]
        spec = ("`## 3. 楽曲の音楽的特徴（内的要因）`・`## 4. 楽曲構成の整理（切り出し箇所）`・`## 5. 時代背景における本楽曲の立ち位置` の3つの見出しを立て、"
                "**3つとも書き切る**（2026-10-06〜。人が書き足す場所を残さない）。\n"
                "- 3章: 曲そのものの特徴を、ウェブで調べた曲の情報（作り手・編曲・BPM・ジャンル・歌詞・本人や作り手の言葉）と、使われ方"
                "（画面の文字・検索候補・尺・元音源の割合）、コメントの言及で書く。音の特徴は、出どころ（調べた情報かコメント）のあることだけを言い切る\n"
                "- 4章: よく使われた部分（画面の文字・台詞）が曲のどこ（イントロ・Aメロ・サビ・台詞）にあたるかを、調べた歌詞の構成と突き合わせて書き、"
                "なぜそこが切り出されたかを使われ方で説明する\n"
                "- 5章: 時代背景の材料に沿って著者の型で書く: この曲の型 → 似た位置づけの曲（曲名・アーティスト・時期を具体的に）→ その型の流れ → "
                "その流れの中で、この曲は何が同じで何が新しいか。型の名前は書いてよいが、教材・過去の記事・月報といった出どころには触れない\n"
                "調べても分からなかったことには触れない。「音源を分析していない」「ここは人の考察」のような断り書きを書かない。")
        heading, length = heading_of(a, ch), "2,500〜5,000字"
    elif ch == "result":
        materials = (result_materials(a) + "\n\n" + research_block(a, ("outside",), "ウェブで調べた TikTok の外の指標") +
                     "\n\n" + common + "\n\n分岐点の章は read の `chapter:branch`。")
        cat += [pr._catalog_line("chapter:branch", "書き終えた分岐点の章"), pr._catalog_line("research", "ウェブで調べたこと（全部）")]
        spec = ("`## 6. バズった結果得られたもの`・`## 7. 今回のヒットの核`（一文で言い切る）・`## 8. 再現性のある要素`（著者の型: 自分でコントロール下に置ける要素だけを"
                "①②③で3〜4項目、見出しは命令形・名詞句、運の部分は「ここは運」と明示）の3つの見出しを立てる。\n"
                "6章は、TikTok の中で起きたこと（材料の数字）に加えて、ウェブで調べた TikTok の外の指標（YouTube・サブスク・チャート・出演・ライブなど）を、"
                "いつの値かを添えて書く。TikTok が伸びた時期と外の動きの前後関係が分かれば、TikTok から外へどう流れたか（流れなかったか）を書く（著者の型）。"
                "外の指標が材料に無ければ、TikTok の中のことだけで書き切る（「扱えない」「分からない」とは書かない）。")
        heading, length = heading_of(a, ch), "1,500〜4,000字"
    elif ch == "intro":
        materials = (common + "\n\n" + intro_materials(a) + "\n\n" + research_block(a, ("outside",), "ウェブで調べた TikTok の外の指標") +
                     "\n\n書き終えた章は read の `chapter:branch`・`chapter:result`（結論の先出しに使う）。")
        cat += [pr._catalog_line("chapter:branch, chapter:result", "書き終えた章")]
        spec = (f"題名 `{heading_of(a, ch)} 〜TikTok今週の1曲` の行で始め（号数は付けない）、文体ガイドの冒頭（挨拶＋対象宣言）→ `## 本楽曲に着目すべき理由`"
                "（**この曲の UGC 数**・期間・逆説の数字。TikTok の外の指標に目立つもの（チャート・MV の再生数など）があれば使ってよい。"
                "「※UGCとは…」の定型注記。3パターンの型判定「本楽曲は②…に該当します。何故なら…」）→ `### 先に結論`"
                "（構成案の記事全体の主張を2〜3行の箇条書きで）→ `## 分析方針`（章立ての予告と「それではいきましょう。」）。"
                "UGC 数は材料の「この曲の UGC 数」を使う。こちらが集めた動画の本数は書かない（方法に触れるなら、"
                "「楽曲ページの投稿から、広がり方の手掛かりになるものを選んで調べた」のように本数を出さずに一言）。")
        heading, length = heading_of(a, ch), "1,000〜2,000字"
    else:
        raise pr.RunnerError(f"知らない章です: {ch}")
    if t["params"].get("recut"):     # 界隈の切り直しのあとの書き直し（前の版の利用者の考察を落とさない）
        n_rc = t["params"]["recut"]
        materials += "\n\n" + recut_write_note(a, n_rc, ch)
        if (recut_prev_dir(a, n_rc) / "chapters" / f"{ch}.md").exists():
            cat.append(pr._catalog_line(f"prev:{ch}", "前の版（界隈を切り直す前）のこの章"))
    if t["params"].get("rewrite"):   # 界隈の掘り下げの書き直し（前の原稿を生かす）
        k = t["params"].get("community")
        kb = f"この章は書き終えてある。文体・言い回しは前の原稿に合わせる。用語集（`kb:glossary`）{style[2]}は、必要なら read で読む。"
        spec = (f"**書き直し（界隈 `{k}` の掘り下げ）**: 前の原稿を read の `chapter:{ch}` で読み、次の点を直した**章の全文**を出す: "
                f"{t['params'].get('why') or '掘り下げた分析に合わせる'}\n"
                f"掘り下げた分析は read の `synthesis` の `### {k}`（利用者の頼み: {t['params'].get('instruction', '')}）。"
                "この界隈に関わらない部分は、前の原稿をなるべくそのまま残す（構成・引用・数字）。\n\n"
                "章の書き方の決まり（最初に書いたときと同じ）: " + spec)
        cat = [pr._catalog_line(f"chapter:{ch}", "この章の前の原稿"),
               pr._catalog_line("synthesis", "界隈ごとのコメント分析（掘り下げた界隈は書き直したもの）")] + \
            [x for x in cat if "synthesis" not in x]
    title = CHAPTER_TITLES.get(ch) or f"拡大経路 {phmap[ch[5:]]['name']}"
    text = pr._fill(tpl("write.md"), {**base, "chapter_title": title, "i": t["params"]["i"], "n": t["params"]["n"],
                                      "kb_block": kb, "chapter_spec": spec, "materials": materials, "heading": heading,
                                      "plan": plan_block(a, ch, st),
                                      "voice": "**枠組みと文体で**" if guide else "**枠組みで**（文体は下の「利用者の設定」のとおり）",
                                      "tone": "著者の後期のトーン（著者本人）" if guide else "下の「利用者の設定」のとおり（著者の文体ガイドには合わせない）",
                                      "length": length, "settings": _settings(a, ["style", "focus"], t)})
    return text, cat


def style_guide_on(a) -> bool:
    """利用者の設定で、著者の文体ガイドを使うか（既定は使う）"""
    try:
        cur = user_settings.get(a.owner) if a.owner else user_settings.DEFAULT
    except user_settings.SettingsError:
        return True
    return bool((cur.get("style") or {}).get("use_guide", True))


def music_materials(a) -> str:
    recs = records(a)
    dur = sorted(r["duration_s"] for r in recs.values() if isinstance(r.get("duration_s"), (int, float)) and r["duration_s"])
    orig = [r for r in recs.values() if r.get("uses_original_sound") is not None]
    sug = collections.Counter(w for r in recs.values() for w in (r.get("suggested_words") or []))
    stick = collections.Counter(s for r in recs.values() for s in (r.get("sticker_texts") or []))
    va = video_analyses(a)
    music_q = []
    for s, v in sorted(va.items()):
        for q in v.get("quotes") or []:
            if re.search(r"曲|サビ|歌|音|リズム|メロ|OP|オープニング|アニメ|song|music|beat|lyrics", q.get("text", ""), re.I):
                music_q.append(f"- seq {s}: 『{q.get('text', '')[:80]}』（{q.get('likes')} いいね、cid {q.get('cid')}）")
    if not va:   # 界隈ごとのコメント分析の形: コメント全体から、音・曲に触れたもの（いいね順）
        music_q = music_comments(a)
    return ("#### 音と使われ方のデータ\n"
            f"- 尺: 中央値 {dur[len(dur) // 2] if dur else '?'}秒（{len(dur)}本）\n"
            f"- 元の音源（楽曲の公式音源）を使った動画: {sum(1 for r in orig if r['uses_original_sound'])} / {len(orig)}本\n"
            "- TikTok の提案語（検索・コメント由来）の上位: " + "、".join(f"{w}（{n}）" for w, n in sug.most_common(15)) + "\n"
            "- 画面上の文字の上位: " + "、".join(f"{w[:30]}（{n}）" for w, n in stick.most_common(10)) + "\n\n"
            "#### コメントで音・曲・原作に触れた引用（コメント分析から）\n" + ("\n".join(music_q[:20]) or "（なし）"))


def result_materials(a) -> str:
    recs = records(a)
    pl = pool(a)
    by_vid = {r["video_id"]: r for r in recs.values()}
    off = [by_vid[v] for v, p in pl.items() if v in by_vid and re.search(r"artist|official", p.get("reasons", ""))]
    top = sorted(recs.values(), key=lambda r: -r["plays"])[:10]
    weekly = open(a.derived("weekly.tsv"), encoding="utf-8").read().splitlines()[1:]
    return ("#### 本人・公式（プールの判定: 音源の作者名に一致するアカウント・公式企画のタグを使った認証アカウント）\n" +
            ("\n".join(f"- {vline(r)}" for r in sorted(off, key=lambda r: r['date'])) or "（見つからなかった）") +
            "\n\n#### 再生上位10本\n" + "\n".join(f"- {vline(r)}" for r in top) +
            "\n\n#### 週ごとの本数と再生（全動画）\n" + "\n".join(f"- {w.split(chr(9))[0]}: {w.split(chr(9))[1]}本、{fmt_plays(w.split(chr(9))[2])}" for w in weekly))


def intro_materials(a) -> str:
    recs = records(a)
    first = min(recs.values(), key=lambda r: r["date"])
    top = max(recs.values(), key=lambda r: r["plays"])
    return (f"#### 冒頭の数字\n- 最初の投稿: {vline(first)}\n- 最大再生: {vline(top)}\n"
            f"- 分析の時点: {a.meta.get('created_at', '')[:10]}")


# --- 仕上げ ---
def mentioned_seqs(a, chapters) -> list:
    out = []
    for c_ in chapters:
        p = a.outputs("chapters", f"{c_}.md")
        if p.exists():
            out += [int(x) for x in re.findall(r"seq\s*(\d+)", p.read_text(encoding="utf-8"))]
    return sorted(set(out))


def finish_materials(a, t, st) -> dict:
    ch = t["params"]["chapter"]
    md = a.outputs("chapters", f"{ch}.md").read_text(encoding="utf-8")
    recs = records(a)
    vs = videos(a)
    tax = pr._taxonomy(a)
    lst = []
    for s in sorted({int(x) for x in re.findall(r"seq\s*(\d+)", md)}):
        if s not in recs:
            continue
        r = recs[s]
        au = (r.get("author") or {}).get("id")
        alive = r.get("enriched")
        lst.append(f"- seq {s}: @{au}（{r['date']}、{fmt_plays(r['plays'])}再生）" +
                   (f" {url_of(vs.get(s, {}))}" if alive else " 削除済み（URL を貼らない）"))
    used = {int(x) for x in WEB_REF_RE.findall(md)}
    web = [f"- {research_line(it)}" for it in (research(a) or {}).get("items") or [] if int(it["id"][1:]) in used]
    return {"chapter_title": CHAPTER_TITLES.get(ch) or first_line(a.outputs("chapters", f"{ch}.md")), "i": t["params"]["i"],
            "n": t["params"]["n"], "chapter_md": md,
            "phase_names": "\n".join(f"- {p['id']} → {p['name']}（{p['start']}〜{p['end']}）" for p in phases(a)),
            "community_names": "\n".join(f"- `{k}` → {pr._first_sentence(v)}" for k, v in tax["community"].items() if not k.startswith("_")),
            "video_list": ("\n".join(lst) or "（この章に動画は出てこない）") +
                          ("\n\n### ウェブで調べた事実の出どころ（この章の [W番号]。印は消す）\n\n" + "\n".join(web) if web else ""),
            "settings": _settings(a, ["style"], t)}


def _safe_copy_materials(a) -> None:
    try:
        copy_materials(a)
    except Exception as e:   # 写せなくても本筋は止めない
        a.log(event="materials_error", error=f"{type(e).__name__}: {e}")


def done_materials(a) -> dict:
    ver = pr._read_json(a.outputs("verify.json"), {}) or {}
    links = []
    _safe_copy_materials(a)
    for name in ("NOTE_BODY.md", "REPORT.md", "EDITOR_NOTES.md", "data.zip"):
        if not a.outputs(name).exists():
            continue
        if name in SHOWN and REPORTS_DIR:
            links.append(f"- [{SHOWN[name][0]}]({dl_url(a, name)}) — {SHOWN[name][1]}")
        elif name in SHOWN:
            links.append(f"- [{SHOWN[name][0].removesuffix('.md')}（{name}）]({dl_url(a, name)}) — {SHOWN[name][1]}")
        else:
            links.append(f"- [Excel 用のデータ（{name}）]({dl_url(a, name)})")
    ph = phases(a)
    summary = "\n".join(f"- {p['name']}（{p['start']}〜{p['end']}）: {p.get('summary', '')}" for p in ph)
    errs = ver.get("errors") or []
    warns = ver.get("warnings") or []
    notes = []
    if errs or warns:
        notes.append(f"- 検算: 確かめきれなかった点が {len(errs) + len(warns)} 件ある（運営が確認する）。利用者には「数字の一部を運営が確認中」と一言だけ")
    res = research(a)
    if res is not None and not res.get("web_search"):
        notes.append("- ウェブ検索: AI のウェブ検索が使えず、曲の情報と TikTok の外の数字は入っていない。3 は「TikTok の中のデータで書き切ってある」と言い換え、"
                     "ウェブ検索をオンにして「" + a.title + "のレポートの6章に、TikTok の外の数字（YouTube・チャートなど）を調べて足して」と頼めば足せる、と一言添える")
    note = "\n".join(notes)
    if REPORTS_DIR and links:   # 先頭にフォルダ（2026-10-03 ユーザー「最終の返答に、成果物フォルダや成果物へのリンクを含んで欲しい」）
        links = folder_lines(a) + links + [f"- 週ごとの投稿数と再生（{WEEKLY_CSV}）: 同じフォルダ（Excel で開けます）"]
    head = "**成果物**（この Mac に保存しました）" if REPORTS_DIR else "**成果物**"
    return {"links": head + "\n\n" + ("\n".join(links) or "- （成果物のリンクを作れなかった）"),
            "summary": summary, "verify_note": note}


# ---------------------------------------------------------------------------
# 検査と取り込み
# ---------------------------------------------------------------------------
def check_examples(tax: dict, sample_seqs: set, strict=True) -> list:
    errs = []
    if not isinstance(tax, dict) or not isinstance(tax.get("community"), dict):
        return []   # 分類軸の形の誤りは check_taxonomy が返す
    ex = tax.get("examples")
    if not isinstance(ex, dict):
        return ["`examples`（界隈ごとの代表例の seq）を入れてください"] if strict else []
    comms = [k for k in tax.get("community", {}) if not k.startswith("_") and k != "unknown"]
    for k in comms:
        seqs = ex.get(k)
        if not seqs:
            if strict:
                errs.append(f"`examples` に界隈 `{k}` の代表例（2〜4本の seq）がありません")
            continue
        if not isinstance(seqs, list):
            errs.append(f"`examples.{k}` は seq の配列にしてください（例: [12, 40]。受け取った: {json.dumps(seqs, ensure_ascii=False)[:40]}）")
            continue
        bad = [s for s in seqs if not isinstance(s, int) or s not in sample_seqs]
        if bad:
            errs.append(f"`examples.{k}` の seq がサンプルにありません: {bad[:5]}")
    extra = [k for k in ex if k not in tax.get("community", {})]
    if extra:
        errs.append(f"`examples` に界隈に無い key があります: {extra[:5]}")
    return errs[:20]


def cited(md: str) -> tuple:
    seqs = {int(x) for x in re.findall(r"seq\s*(\d+)", md)}
    cids = set(re.findall(r"cid\s*[:：]?\s*(\d{10,})", md)) | set(re.findall(r"\[(\d{15,})\]", md))
    return seqs, cids


def check_chapter(a, md: str, heading: str) -> list:
    errs = []
    body = pr._strip_fence(md)
    first = next((l.strip() for l in body.splitlines() if l.strip()), "")
    if not first.startswith(heading.split("〜")[0].strip()):
        errs.append(f"1行目は `{heading}` の見出しにしてください（受け取った1行目: {first[:60]}）")
    if len(body) < 300:
        errs.append("短すぎます（見出しだけになっていないか）")
    seqs, cids = cited(body)
    recs = records(a)
    bad = sorted(s for s in seqs if s not in recs)
    if bad:
        errs.append(f"存在しない seq が出てきます: {bad[:10]}（材料にある動画だけ）")
    known = all_cids(a)
    badc = sorted(c for c in cids if c not in known)
    if badc:
        errs.append(f"入力に無い cid が出てきます: {badc[:10]}（コメント分析・統合にある cid だけ）")
    words = sorted({m.group(0) for m in READER_RE.finditer(body)})
    if words:
        errs.append(f"読者に見せない作業の言葉・集めた本数の言い方があります: {words[:10]}"
                    "（用語集などの出どころは書かない。数字は曲全体の UGC 数で語り、集めた本数を主語にしない）")
    errs += check_no_placeholder(body)
    badw = sorted({int(x) for x in WEB_REF_RE.findall(body)} - research_ids(a))
    if badw:
        errs.append(f"材料に無い出どころの番号があります: {['W' + str(x) for x in badw[:10]]}（ウェブで調べたことの [W番号] だけ）")
    kb = sorted(set(KB_FILE_RE.findall(body)))
    if kb:
        errs.append(f"記事の名前（時代背景の材料の出どころ）が本文にあります: {kb[:5]}（曲名・アーティスト名で書き、出どころには触れない）")
    return errs


def check_no_placeholder(body: str) -> list:
    hits = sorted({m.group(0) for m in PLACEHOLDER_RE.finditer(body)})
    if not hits:
        return []
    return [f"空けておく書き方があります: {hits[:5]}（人が書き足す前提の文や、書けないことの断り書きは書かない。"
            "材料・ウェブで調べたこと・時代背景の材料から言えることで書き切り、分からないことには触れない）"]


# 段階の ID（P1〜）は日本語に挟まれても拾う（\b は日本語の文字も語の文字とみなすので「P2の」「段階P2では」を見逃していた）
INTERNAL_RE = re.compile(r"seq\s*\d+|\bcid\b|(?<![A-Za-z0-9])P\d(?![A-Za-z0-9])|records\.jsonl|本データ|先行工程|\[W\d+\]|" + KB_FILE_RE.pattern, re.I)


def check_finished(a, md: str, allowed_urls: set) -> list:
    errs = []
    body = pr._strip_fence(md)
    hits = sorted(set(m.group(0) for m in INTERNAL_RE.finditer(body)))
    if hits:
        errs.append(f"内部の印が残っています: {hits[:10]}（消すか日本語に）")
    words = sorted({m.group(0) for m in READER_RE.finditer(body)})
    if words:
        errs.append(f"読者に見せない作業の言葉・集めた本数の言い方が残っています: {words[:10]}"
                    "（読者に向けた説明に言い換えるか消す。数字は曲全体の UGC 数で語る）")
    errs += check_no_placeholder(body)
    tax = pr._taxonomy(a)
    keys = [k for ax in ("community", "format", "motive") for k in tax.get(ax, {}) if "_" in k]
    left = sorted({k for k in keys if re.search(rf"(?<![A-Za-z0-9_]){re.escape(k)}(?![A-Za-z0-9_])", body)})
    if left:
        errs.append(f"界隈などの key が残っています: {left[:10]}（日本語の名前に）")
    for u in re.findall(r"https?://(?:www\.)?tiktok\.com/[^\s)）」、。]+", body):
        if u.split("?")[0] not in allowed_urls:
            errs.append(f"一覧に無い TikTok の URL があります: {u[:80]}")
    for line in body.splitlines():
        if re.search(r"https?://(?:www\.)?tiktok\.com/@", line) and not re.fullmatch(r"\s*https?://\S+\s*", line):
            errs.append(f"TikTok の動画の URL はそれだけの行に置いてください（note が埋め込みに変える）: {line.strip()[:80]}")
            break
    if len(body) < 150:
        errs.append("短すぎます")
    return errs[:20]


def _seq_int(v):
    """seq を数字に寄せる（AI が "12" と文字列で書いても受け取る）。数字でなければ None"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


_TYPE_NAMES = {"str": "文字列", "list": "配列", "dict": "オブジェクト", "int": "数字", "float": "数字", "NoneType": "null", "bool": "真偽値"}


def _shape_hint(e: Exception) -> str:
    """検査が落ちた例外から、出力のどこの形が違うかの手掛かり（AI に返す）"""
    msg = str(e)
    if isinstance(e, KeyError):
        return f"{msg} が無い"
    m = re.search(r"'(\w+)' object", msg)
    if m and m.group(1) in _TYPE_NAMES:
        return f"オブジェクト・配列・文字列の別が違う所に{_TYPE_NAMES[m.group(1)]}が入っている（{type(e).__name__}: {msg[:80]}）"
    return f"{type(e).__name__}: {msg[:80]}"


def accept(a, t: dict, raw: str, st: dict) -> list:
    """AI の出力を検査して取り込む（だめなら理由の一覧で差し戻す）。形の違う出力で検査そのものが落ちたときも、例外にせず差し戻す
    （例外だと道具は「サービス側のエラー」と返し、AI は同じ出力で呼び直して同じ所で落ち続ける。2026-10-06 通し試験）"""
    saved = copy.deepcopy(st)
    try:
        return _accept(a, t, raw, st)
    except pr.RunnerError:
        raise
    except Exception as e:   # noqa: BLE001
        # 途中で足した仕事・書き換えた params は戻す（差し戻しでも submit は仕事の列を書き戻すため）。t は同じものを使い続ける
        for i, x in enumerate(saved.get("tasks") or []):
            if x.get("task_id") == t.get("task_id"):
                t.clear()
                t.update(x)
                saved["tasks"][i] = t
        st.clear()
        st.update(saved)
        try:
            import traceback
            a.log(event="accept_error", task_id=t.get("task_id"), error=f"{type(e).__name__}: {e}",
                  where=traceback.format_exc(limit=-3)[-600:])
        except Exception:   # 記録できなくても差し戻しは返す
            pass
        return [f"出力の形が指示書の「出力の形」と違うため、検査できませんでした（{_shape_hint(e)}）。"
                "オブジェクト（{…}）・配列（[…]）・文字列・数字の別と、配列の各要素の形を「出力の形」の例に合わせて出し直してください"]


def _accept(a, t: dict, raw: str, st: dict) -> list:
    typ = t["type"]
    if typ in ("axes", "confirm"):
        sample = set(axes_sample(a))
        if typ == "axes":
            tax, err = pr._parse_json(raw)
            if err:
                return [err]
            errs = pr.check_taxonomy(tax, need_region=False) + check_examples(tax, sample)
            if not errs:
                pr._write_json(a.outputs("taxonomy_proposal.json"), tax)
            return errs
        obj, err = pr._parse_json(raw)
        if err:
            return [err + "（{\"user_answer\": \"利用者の言葉\", \"taxonomy\": {...}} の形）"]
        if not isinstance(obj, dict) or not str(obj.get("user_answer", "")).strip():
            return ["user_answer（利用者の答えをそのまま）を入れてください"]
        if obj.get("taxonomy") and not isinstance(obj["taxonomy"], dict):
            return ["taxonomy は分類軸の全体（{\"community\": {...}, \"format\": {...}, ...} のオブジェクト）にしてください。"
                    "利用者の答えで分類軸を直さないなら、taxonomy は省く"]
        prop = pr._taxonomy(a, confirmed=False)
        tax = obj.get("taxonomy") or prop
        errs = pr.check_taxonomy(tax, need_region=False) + check_examples(tax, sample, strict=False)
        if errs:
            return errs
        modified = json.dumps(tax, sort_keys=True, ensure_ascii=False) != json.dumps(prop, sort_keys=True, ensure_ascii=False)
        pr._write_json(a.outputs("taxonomy.json"), tax)
        pr._write_json(a.outputs("confirm_answer.json"), {"user_answer": obj["user_answer"], "modified": modified, "at": pr._now()})
        return []

    if typ == "label":
        tax = pr._taxonomy(a)
        errs, rows = pr.check_labels(raw, t["params"]["seqs"], tax, with_region=False)
        if errs:
            return errs
        head = ["seq", "community", "format", "motive", "tier", "conf", "reason"]
        order = {s: i for i, s in enumerate(t["params"]["seqs"])}
        rows.sort(key=lambda r: order[int(r["seq"])])
        pr._write_text(a.outputs("labels", f"batch_{t['params']['batch']:02d}.tsv"),
                       "\t".join(head) + "\n" + "".join("\t".join(r[h] for h in head) + "\n" for r in rows))
        merge_labels(a)
        return []

    if typ == "phases":
        obj, err = pr._parse_json(raw)
        if err:
            return [err]
        return accept_phases(a, obj)

    if typ == "comments":
        tax = pr._taxonomy(a)
        seq = t["params"]["seq"]
        comments, video_id = a.comment_text(seq)
        obj, err = pr._parse_json(raw)
        if err:
            return [err]
        errs = pr.check_video_analysis(obj, seq, video_id, comments, tax)
        if errs:
            return errs
        pr._write_json(a.outputs("video_analysis", f"{seq}.json"), obj)
        pr._merge_video_analysis(a)
        return []

    if typ == "synthesis":
        md = pr._strip_fence(raw)
        k = t["params"]["community"]
        errs = []
        if not md.lstrip().startswith(f"### {k}"):
            errs.append(f"1行目は `### {k}` にしてください")
        for sub in ("#### 取れた代表動画", "#### この界隈が曲を採用した文脈", "#### 説明力のある引用"):
            if sub not in md:
                errs.append(f"小見出し `{sub}` がありません")
        seqs, cids = cited(md)
        recs = records(a)
        if [s for s in seqs if s not in recs]:
            errs.append(f"存在しない seq: {[s for s in seqs if s not in recs][:5]}")
        known = all_cids(a)
        if [c for c in cids if c not in known]:
            errs.append(f"入力に無い cid: {[c for c in cids if c not in known][:5]}")
        if errs:
            return errs
        pr._write_text(a.outputs("synthesis", f"{k}.md"), md + "\n")
        return []

    if typ == "ccomments":
        md = pr._strip_fence(raw)
        k = t["params"]["community"]
        errs = []
        if not md.lstrip().startswith(f"### {k}"):
            errs.append(f"1行目は `### {k}` にしてください")
        for sub in ("#### コメントの全体傾向", "#### この界隈が曲を採用した文脈", "#### 具体例で確かめた引用"):
            if sub not in md:
                errs.append(f"小見出し `{sub}` がありません")
        seqs, cids = cited(md)
        recs = records(a)
        if [x for x in seqs if x not in recs]:
            errs.append(f"存在しない seq: {[x for x in seqs if x not in recs][:5]}")
        known = all_cids(a)
        if [c_ for c_ in cids if c_ not in known]:
            errs.append(f"入力に無い cid: {[c_ for c_ in cids if c_ not in known][:5]}（ccomments・comments:<seq> で読んだものだけ）")
        if not cids and "根拠薄" not in md:
            errs.append("具体例の引用（cid つき）がありません。コメントが少なくて引用できないなら「根拠薄」と書く")
        if errs:
            return errs
        pr._write_text(a.outputs("synthesis", f"{k}.md"), md + "\n")
        return []

    if typ == "dcomments":
        md = pr._strip_fence(raw)
        k = t["params"]["community"]
        errs = []
        if not md.lstrip().startswith(f"### {k}"):
            errs.append(f"1行目は `### {k}` にしてください")
        for sub in ("#### コメントの全体傾向", "#### この界隈が曲を採用した文脈", "#### 動画が伸びた理由",
                    "#### 具体例で確かめた引用", "#### 前回からの変化"):
            if sub not in md:
                errs.append(f"小見出し `{sub}` がありません")
        seqs, cids = cited(md)
        recs = records(a)
        if [x for x in seqs if x not in recs]:
            errs.append(f"存在しない seq: {[x for x in seqs if x not in recs][:5]}")
        known = all_cids(a)
        if [c_ for c_ in cids if c_ not in known]:
            errs.append(f"入力に無い cid: {[c_ for c_ in cids if c_ not in known][:5]}（dcomments・comments:<seq> で読んだものだけ）")
        if not cids and "根拠薄" not in md:
            errs.append("具体例の引用（cid つき）がありません。コメントが少なくて引用できないなら「根拠薄」と書く")
        if errs:
            return errs
        # 掘り下げで書き直す前の版（note 用の章・REPORT.md・NOTE_BODY.md も）を outputs/history/deepen_r<回>/ に残す（完了の知らせで「前の版は残してある」と伝える）
        keep_version(a, "deepen", t["params"]["round"])
        keep_history(a, a.outputs("synthesis", f"{k}.md"), f"r{t['params']['round']}")
        pr._write_text(a.outputs("synthesis", f"{k}.md"), md + "\n")
        return []

    if typ == "outline_revise":
        return accept_outline_revise(a, t, raw, st)

    if typ == "recut":
        return accept_recut(a, t, raw, st)

    if typ == "review":
        return accept_review(a, t, raw, st)

    if typ == "ref_select":
        obj, err = pr._parse_json(raw)
        if err:
            return [err]
        refs = (obj or {}).get("references") if isinstance(obj, dict) else None
        n = cfg(a)["n_refs"]
        if not isinstance(refs, list) or len(refs) != n:
            return [f"references を {n} 本の配列にしてください"]
        if not all(isinstance(r, dict) for r in refs):
            return ["references の各要素は {\"file\": \"kb:cards の file\", \"why\": \"選んだ理由\"} のオブジェクトにしてください（file の文字列だけにしない）"]
        ok = {c_["file"]: c_ for c_ in cards(a)}
        errs = []
        files = [str(r.get("file", "")).strip() for r in refs]
        for f in files:
            if f not in ok:
                errs.append(f"{f} は選べるカードにありません（kb:cards の file。分析する曲を扱った記事は選べない）")
        if len(set(files)) != len(files):
            errs.append("同じ記事を2回選んでいます")
        if errs:
            return errs
        idx = {x.get("file"): x for x in pr._read_json(KB_DIR / "INDEX.json", []) or []}
        pr._write_json(a.outputs("references", "selected.json"), {"references": refs, "at": pr._now()})
        dig = [x for x in st["tasks"] if x["type"] == "ref_digest"]
        for x, r, f in zip(dig, refs, files):
            title = (idx.get(f) or {}).get("name") or ok[f].get("title") or f
            x["params"].update({"file": f, "title": title, "date": ok[f].get("date", ""), "why": r.get("why", "")})
            x["title"] = f"参考記事の章立てと論理（{x['params']['i']}/{x['params']['n']}: {title[:30]}）"
        return []

    if typ == "ref_digest":
        md = pr._strip_fence(raw)
        errs = []
        if not md.lstrip().startswith("### "):
            errs.append("`### 参考N: 題` の見出しで始めてください")
        for sub in ("#### 章立てと各章の役割", "#### 論理の運び", "#### この曲の記事に使える組み立て"):
            if sub not in md:
                errs.append(f"小見出し `{sub}` がありません")
        if len(md) > 2500:
            errs.append(f"{len(md)} 字あります。抽象の組み立てだけにして 1,500 字程度に（言い回し・数字・固有名詞は写さない）")
        if errs:
            return errs
        pr._write_text(a.outputs("references", f"{t['params']['i']:02d}.md"), md + "\n")
        return []

    if typ == "research":
        return accept_research(a, raw)

    if typ == "era":
        return accept_era(a, raw)

    if typ == "outline":
        obj, err = pr._parse_json(raw)
        if err:
            return [err]
        if not isinstance(obj, dict) or not str(obj.get("thesis", "")).strip():
            return ["thesis（記事全体の主張）を入れてください"]
        chs = chapter_order(a, st)
        got = [c_.get("id") for c_ in obj.get("chapters") or [] if isinstance(c_, dict)]
        errs = []
        if got != chs:
            errs.append(f"chapters の id は、この順で全部: {chs}（受け取った: {got}）")
        recs = records(a)
        known = all_cids(a)
        for c_ in obj.get("chapters") or []:
            if not isinstance(c_, dict):
                continue
            cl = c_.get("claims")
            if not isinstance(cl, list) or not cl:
                errs.append(f"`{c_.get('id')}` の claims（主張と根拠）を1個以上")
                continue
            errs += _check_evidence(a, c_.get("id"), cl, recs, known)
        if errs:
            return errs[:20]
        pr._write_json(a.outputs("outline.json"), obj)
        pr._write_text(a.outputs("outline.md"), outline_md(obj, chs) + "\n")
        return []

    if typ == "write":
        ch = t["params"]["chapter"]
        ph = phases(a)
        heading = "## 1. バズの拡大経路" if ch == f"path_{ph[0]['id']}" else heading_of(a, ch)
        errs = check_chapter(a, raw, heading)
        if errs:
            return errs
        if t["params"].get("rewrite"):
            keep_history(a, a.outputs("chapters", f"{ch}.md"), f"deepen-r{t['params'].get('round')}")
        pr._write_text(a.outputs("chapters", f"{ch}.md"), pr._strip_fence(raw) + "\n")
        return []

    if typ == "finish":
        ch = t["params"]["chapter"]
        vs = videos(a)
        allowed = {url_of(v) for v in vs.values() if url_of(v)}
        errs = check_finished(a, raw, allowed)
        if errs:
            return errs
        pr._write_text(a.outputs("note_chapters", f"{ch}.md"), pr._strip_fence(raw) + "\n")
        return []

    if typ == "finish_title":
        obj, err = pr._parse_json(raw)
        if err:
            return [err]
        if not isinstance(obj, dict) or not str(obj.get("title", "")).strip() or not str(obj.get("guesses_md", "")).strip():
            return ["title と guesses_md（推測で書いたところ。無ければ「なし」）を入れてください"]
        if not all(isinstance(obj.get(k), str) for k in ("title", "guesses_md")) or \
                not isinstance(obj.get("changes_md") or "", str):
            return ["title・guesses_md（・changes_md）は文字列にしてください（guesses_md は Markdown の文字列）"]
        errs = check_no_placeholder(obj["title"])
        if re.search(r"No\.\s*—|No\.\s*-", obj["title"]):
            errs.append("題名に号数（No.—）を付けないでください（「【曲名 / アーティスト】Hitの理由分析レポート 〜TikTok今週の1曲」の形）")
        if errs:
            return errs
        pr._write_json(a.outputs("note_meta.json"), obj)
        return []

    if typ == "revise":
        obj, err = pr._parse_json(raw)
        if err:
            return [err]
        if not isinstance(obj, dict):
            return ["{\"chapter\": \"<章の id>\", \"markdown\": \"直した章の全文\", \"note_to_user\": \"...\"} のオブジェクト1つにしてください"
                    "（配列にしない。複数の章に当たる指示なら、いちばん当たる章を1つ）"]
        ch = obj.get("chapter")
        if ch not in chapter_order(a, st):
            return [f"chapter は章の一覧の id から: {chapter_order(a, st)}"]
        if not isinstance(obj.get("markdown"), str):
            return ["markdown は直した章の全文（Markdown の文字列）にしてください"]
        ph = phases(a)
        heading = "## 1. バズの拡大経路" if ch == f"path_{ph[0]['id']}" else heading_of(a, ch)
        errs = check_chapter(a, obj.get("markdown") or "", heading)
        if errs:
            return errs
        old = a.outputs("chapters", f"{ch}.md")
        if old.exists():
            hist = a.outputs("chapters", "history", f"{ch}_{datetime.datetime.now():%Y%m%d-%H%M%S}.md")
            hist.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, hist)
        pr._write_text(old, pr._strip_fence(obj["markdown"]) + "\n")
        pr._write_json(a.outputs("revisions", f"{t['n']:03d}.json"),
                       {"instruction": t["params"]["instruction"], "chapter": ch, "note": obj.get("note_to_user"), "at": pr._now()})
        # 続く仕上げの仕事にこの章を渡す
        i = st["tasks"].index(t)
        for nt in st["tasks"][i + 1:]:
            if nt["type"] == "finish" and nt["params"].get("chapter") is None:
                nt["params"]["chapter"] = ch
                nt["title"] = f"note 用に仕上げ直す（{ch}）"
                break
        return []

    return [f"この仕事（{typ}）には提出は要りません"]


def keep_history(a, p: Path, tag: str) -> None:
    """直す前の版を、同じ置き場の history/ に残す"""
    if p.exists():
        h = p.parent / "history" / f"{p.stem}_{tag}_{datetime.datetime.now():%Y%m%d-%H%M%S}{p.suffix}"
        h.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, h)


def _write_order(a) -> list:
    """執筆の順（service_plan と同じ。冒頭はまとめの章を読んでから最後に）"""
    return [f"path_{p['id']}" for p in phases(a)] + ["branch", "music", "result", "intro"]


def _rewrite_tasks(a, st, t, items: list, label: str) -> list:
    pm = t["params"]
    order = _write_order(a)
    items = sorted(items, key=lambda x: order.index(x["chapter"]))
    return [_task(st, "write", "ai", f"{label}（{i}/{len(items)}: {CHAPTER_TITLES.get(x['chapter']) or '拡大経路 ' + x['chapter'][5:]}）",
                  {"chapter": x["chapter"], "i": i, "n": len(items), "first": False, "rewrite": True, "why": x.get("why", ""),
                   "community": pm["community"], "round": pm["round"], "instruction": pm["instruction"]})
            for i, x in enumerate(items, 1)]


def _check_evidence(a, cid_: str, claims, recs=None, known=None) -> list:
    """構成案の claims（主張と根拠）の検査。根拠の seq は数字に寄せて書き戻す（"12" と文字列で書いても受け取る）"""
    errs = []
    recs = records(a) if recs is None else recs
    known = all_cids(a) if known is None else known
    web = research_ids(a)
    for x in claims:
        if not isinstance(x, dict):
            errs.append(f"`{cid_}` の claims の各要素は {{\"claim\": \"主張\", \"evidence\": [...]}} のオブジェクトにしてください"
                        f"（受け取った: {json.dumps(x, ensure_ascii=False)[:40]}）")
            continue
        ev = x.get("evidence") or []
        if not isinstance(ev, list):
            errs.append(f"`{cid_}` の evidence は配列（[{{\"seq\": 12}}, {{\"cid\": \"…\"}}]）にしてください")
            continue
        for e in ev:
            if isinstance(e, dict) and "seq" in e:
                s = _seq_int(e["seq"])
                if s is None:
                    errs.append(f"`{cid_}` の根拠の seq は数字にしてください: {json.dumps(e['seq'], ensure_ascii=False)[:20]}")
                elif s not in recs:
                    errs.append(f"`{cid_}` の根拠に存在しない seq {s}")
                else:
                    e["seq"] = s
            if isinstance(e, dict) and "cid" in e and str(e["cid"]) not in known:
                errs.append(f"`{cid_}` の根拠に入力に無い cid {e['cid']}")
            if isinstance(e, dict) and "web" in e:
                m = re.fullmatch(r"\[?W(\d+)\]?", str(e["web"]).strip())
                if not m or int(m.group(1)) not in web:
                    errs.append(f"`{cid_}` の根拠に、ウェブで調べたことに無い番号 {e['web']}（read の research の W番号）")
    return errs


def accept_outline_revise(a, t, raw, st) -> list:
    """構成案の直し: 直した章の計画を差し替え、書き直す章の仕事を足し、通し読みに直した章を渡す"""
    obj, err = pr._parse_json(raw)
    if err:
        return [err]
    if not isinstance(obj, dict) or not str(obj.get("thesis", "")).strip():
        return ["thesis（記事全体の主張。変えないなら今のまま写す）を入れてください"]
    chs = chapter_order(a, st)
    errs = []
    plans = [c_ for c_ in obj.get("chapters") or [] if isinstance(c_, dict)]
    recs, known = records(a), all_cids(a)
    for c_ in plans:
        if c_.get("id") not in chs:
            errs.append(f"chapters の id は章の一覧から: {c_.get('id')}（{chs}）")
            continue
        cl = c_.get("claims")
        if not isinstance(cl, list) or not cl:
            errs.append(f"`{c_['id']}` の claims（主張と根拠）を1個以上")
            continue
        errs += _check_evidence(a, c_["id"], cl, recs, known)
    rw = [x for x in obj.get("rewrite") or [] if isinstance(x, dict)]
    ids = [x.get("chapter") for x in rw]
    if not rw:
        errs.append("rewrite（書き直す章）を1つ以上入れてください")
    if [i for i in ids if i not in chs]:
        errs.append(f"rewrite の chapter は章の一覧から: {[i for i in ids if i not in chs]}（{chs}）")
    if len(set(ids)) != len(ids):
        errs.append("rewrite に同じ章が2回あります")
    miss = [c_["id"] for c_ in plans if c_.get("id") in chs and c_["id"] not in ids]
    if miss:
        errs.append(f"計画を直した章は rewrite にも入れてください: {miss}")
    if obj.get("thesis_changed") and "intro" not in ids:
        errs.append("記事全体の主張を変えたなら、rewrite に intro を入れてください")
    if errs:
        return errs[:20]
    pm = t["params"]
    old = outline(a) or {"thesis": "", "chapters": []}
    keep_history(a, a.outputs("outline.json"), f"deepen-r{pm['round']}")
    by = {c_["id"]: c_ for c_ in plans}
    have = {c_.get("id") for c_ in old.get("chapters") or []}
    o = {**old, "thesis": str(obj["thesis"]).strip(),
         "chapters": [by.get(c_.get("id"), c_) for c_ in old.get("chapters") or []] + [c_ for c_ in plans if c_["id"] not in have]}
    pr._write_json(a.outputs("outline.json"), o)
    pr._write_text(a.outputs("outline.md"), outline_md(o, chs) + "\n")
    insert_after(st, t, _rewrite_tasks(a, st, t, rw, "書き直し"))
    changed = [{"chapter": x["chapter"], "why": x.get("why", "")} for x in rw]
    for x in st["tasks"]:
        if x["type"] == "review" and x["params"].get("round") == pm["round"] and x["status"] != "done":
            x["params"].update({"changed": changed, "thesis_changed": bool(obj.get("thesis_changed"))})
    pr._write_json(deepen_record(a, pm["round"]),
                   {"community": pm["community"], "instruction": pm["instruction"], "at": pr._now(),
                    "outline_note": obj.get("note_to_user"), "thesis_changed": bool(obj.get("thesis_changed")), "changed": changed})
    return []


def accept_review(a, t, raw, st) -> list:
    """通し読み: ずれの直しの仕事を足し、直した章をまとめて note 用に仕上げ直す仕事を足す"""
    obj, err = pr._parse_json(raw)
    if err:
        return [err]
    if not isinstance(obj, dict) or not isinstance(obj.get("fixes"), list):
        return ["fixes（直す章の配列。ずれが無ければ空の配列）を入れてください"]
    chs = chapter_order(a, st)
    fixes = [x for x in obj["fixes"] if isinstance(x, dict)]
    ids = [x.get("chapter") for x in fixes]
    errs = []
    if [i for i in ids if i not in chs]:
        errs.append(f"fixes の chapter は章の一覧から: {[i for i in ids if i not in chs]}（{chs}）")
    if len(set(ids)) != len(ids):
        errs.append("fixes に同じ章が2回あります（1つにまとめる）")
    if [x for x in fixes if not str(x.get("what", "")).strip()]:
        errs.append("fixes の what（何がずれていて、どう直すか）を入れてください")
    if errs:
        return errs
    pm = t["params"]
    new = _rewrite_tasks(a, st, t, [{"chapter": x["chapter"], "why": "通し読みで見つけたずれ: " + x["what"]} for x in fixes],
                         "通し読みの直し") if fixes else []
    fin = sorted(set(x["chapter"] for x in pm.get("changed") or []) | set(ids), key=chs.index)
    new += [_task(st, "finish", "ai", f"note 用に仕上げ直す（{i}/{len(fin)}: {CHAPTER_TITLES.get(ch) or '拡大経路 ' + ch[5:]}）",
                  {"chapter": ch, "i": i, "n": len(fin)}) for i, ch in enumerate(fin, 1)]
    if pm.get("thesis_changed"):
        new.append(_task(st, "finish_title", "ai", "題名と確認メモ（記事全体の主張を変えたので）"))
    insert_after(st, t, new)
    rec = pr._read_json(deepen_record(a, pm["round"]), {}) or {}
    rec.update({"review_note": obj.get("note_to_user"), "fixes": fixes, "changed_all": fin, "reviewed_at": pr._now()})
    pr._write_json(deepen_record(a, pm["round"]), rec)
    return []


def merge_labels(a) -> None:
    """ラベルの束を合わせ、地域はデータ（投稿地域）から付ける（F4）。列は E2E の labels.tsv と同じ"""
    recs = records(a)
    rows = {}
    d = a.outputs("labels")
    for f in sorted(d.glob("batch_*.tsv")):
        lines = f.read_text(encoding="utf-8").splitlines()
        head = lines[0].split("\t")
        for line in lines[1:]:
            r = dict(zip(head, line.split("\t")))
            rows[int(r["seq"])] = r
    head = ["seq", "community", "format", "motive", "region", "tier", "conf", "reason"]
    with open(a.outputs("labels.tsv"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(head) + "\n")
        for s in sorted(rows):
            r = rows[s]
            r["region"] = (recs.get(s) or {}).get("location_created") or "unknown"
            f.write("\t".join(str(r.get(h, "")) for h in head) + "\n")


def accept_phases(a, obj) -> list:
    errs = []
    if not isinstance(obj, dict) or not isinstance(obj.get("phases"), list):
        return ["{\"phases\": [...], \"overlooked\": [...], \"pathway_md\": \"...\"} の形にしてください"]
    ph = obj["phases"]
    if not 2 <= len(ph) <= 6:
        errs.append(f"段階は3〜5個（2〜6まで受け付ける）。受け取った: {len(ph)}個")
    recs = records(a)
    labs = labels(a)
    dates = sorted(r["date"] for r in recs.values())
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for i, p in enumerate(ph, 1):
        if not isinstance(p, dict):
            errs.append("phases の各要素は {id, name, start, end, summary, reps}")
            continue
        if p.get("id") != f"P{i}":
            errs.append(f"{i}番目の id は P{i} にしてください")
        if not p.get("name"):
            errs.append(f"P{i} の name がありません")
        for kk in ("start", "end"):
            v = str(p.get(kk, ""))
            try:
                ok = bool(date_re.match(v)) and bool(datetime.date.fromisoformat(v))
            except ValueError:
                ok = False
            if not ok:
                errs.append(f"P{i} の {kk} は YYYY-MM-DD（ある日付）にしてください")
        reps = p.get("reps") or []
        if not isinstance(reps, list):
            errs.append(f"P{i} の reps は seq の配列にしてください（例: [12, 40]）")
            continue
        seqs = [_seq_int(s) for s in reps]   # "12" と文字列で書いても受け取る
        bad = [s for s, n in zip(reps, seqs) if n is None or n not in labs]
        if bad:
            errs.append(f"P{i} の reps にラベルの付いていない seq: {bad[:5]}")
        else:
            p["reps"] = seqs
    if errs:
        return errs[:20]
    if ph[0]["start"] > dates[0]:
        errs.append(f"最初の段階の start は {dates[0]} 以前にしてください")
    if ph[-1]["end"] < dates[-1]:
        errs.append(f"最後の段階の end は {dates[-1]} 以降にしてください")
    for p in ph:   # 1つ目の段階も（前は2つ目以降しか見ず、中身の無い段階 P1 とその章ができた）
        if p["start"] > p["end"]:
            errs.append(f"{p['id']} の start が end より後です")
    for p, q in zip(ph, ph[1:]):
        nxt = (datetime.date.fromisoformat(p["end"]) + datetime.timedelta(days=1)).isoformat()
        if q["start"] != nxt:
            errs.append(f"{q['id']} の start は {p['id']} の end の翌日（{nxt}）にしてください")
    if not isinstance(obj.get("pathway_md"), str) or not obj["pathway_md"].strip():
        errs.append("pathway_md（拡散経路の下書き。Markdown の文字列）を書いてください")
    ov = obj.get("overlooked") or []
    if not isinstance(ov, list):
        errs.append("overlooked は seq の配列にしてください（無ければ []）")
    if errs:
        return errs
    ov = [n for n in (_seq_int(s) for s in ov) if n in recs]
    pr._write_json(a.outputs("phases.json"), {"phases": ph, "overlooked": ov})
    pr._write_text(a.outputs("pathway.md"), obj["pathway_md"].strip() + "\n")
    return []


# ---------------------------------------------------------------------------
# サービスがやる工程
# ---------------------------------------------------------------------------
def run_service(a, t: dict, st: dict) -> dict:
    typ = t["type"]
    if typ == "reps":
        return service_reps(a, t, st)
    if typ == "plan":
        return service_plan(a, t, st)
    if typ == "recut_check":
        return service_recut_check(a, t, st)
    if typ == "assemble":
        return service_assemble(a, st)
    if typ == "verify":
        return service_verify(a, st)
    if typ == "export":
        return service_export(a)
    raise pr.RunnerError(f"知らないサービスの工程です: {typ}")


def service_reps(a, t: dict, st: dict) -> dict:
    """代表の選定（C4）: コメントが取れた動画の中から、界隈×段階のセルで数字で選ぶ。AI も人も使わない"""
    c = cfg(a)
    recs = records(a)
    labs = labels(a)
    ph = phases(a)
    got = fetched(a)
    pl = pool(a)
    cand = []
    for s, l in labs.items():
        r = recs[s]
        if l["community"] == "unknown" or r["video_id"] not in got:
            continue
        if not a.derived("comments", f"{s}_{r['video_id']}.md").exists():
            continue
        cand.append(s)
    cells = collections.defaultdict(list)
    for s in cand:
        cells[(labs[s]["community"], phase_of(recs[s]["date"], ph))].append(s)
    all_cells = collections.Counter((l["community"], phase_of(recs[s]["date"], ph)) for s, l in labs.items() if l["community"] != "unknown")
    chosen, why = [], {}

    def take(s, reason):
        if s not in why and len(chosen) < c["n_reps"]:
            chosen.append(s)
            why[s] = reason

    # 必ず入れる: 起点（最初期）・本人と公式・再生上位3本
    for s in sorted(cand, key=lambda s: recs[s]["date"])[:3]:
        take(s, "最初期")
    for s in cand:
        if re.search(r"artist|official", (pl.get(recs[s]["video_id"]) or {}).get("reasons", "")) or \
                labs[s].get("tier") in ("official_artist", "official_brand"):
            take(s, "本人・公式")
    for s in sorted(cand, key=lambda s: -recs[s]["plays"])[:3]:
        take(s, "再生上位")
    # 各セルに最低2本（最初期1＋最大再生1）
    for cell, ss in sorted(cells.items(), key=lambda kv: min(recs[s]["date"] for s in kv[1])):
        take(min(ss, key=lambda s: recs[s]["date"]), f"セル {cell[0]}×{cell[1]} の最初期")
        if c["reps_min_per_cell"] >= 2:
            take(max(ss, key=lambda s: recs[s]["plays"]), f"セル {cell[0]}×{cell[1]} の最大再生")
    # 残りはセルの規模（ラベル付きの本数）の平方根に比例
    rest = c["n_reps"] - len(chosen)
    if rest > 0:
        w = {cell: math.sqrt(all_cells[cell]) for cell in cells}
        tot = sum(w.values()) or 1
        for cell, ss in cells.items():
            k = int(round(rest * w[cell] / tot))
            for s in sorted(ss, key=lambda s: -recs[s]["plays"]):
                if k <= 0:
                    break
                if s not in why:
                    take(s, f"セル {cell[0]}×{cell[1]}（規模で配分）")
                    k -= 1
    missing = sorted((cell, n) for cell, n in all_cells.items() if cell not in cells)
    reps_sorted = sorted(chosen, key=lambda s: recs[s]["date"])
    a.derived("ai").mkdir(parents=True, exist_ok=True)
    with open(a.derived("ai", "reps.tsv"), "w", encoding="utf-8", newline="\n") as f:
        f.write("seq\tvideo_id\tcommunity\tphase\tdate\tplays\tusername\twhy\n")
        for s in reps_sorted:
            r = recs[s]
            f.write(f"{s}\t{r['video_id']}\t{labs[s]['community']}\t{phase_of(r['date'], ph)}\t{r['date']}\t{r['plays']}\t"
                    f"{(r.get('author') or {}).get('id')}\t{why[s]}\n")
    pr._write_json(a.derived("ai", "reps_summary.json"),
                   {"candidates": len(cand), "chosen": len(chosen), "cells_with_reps": len(cells),
                    "cells_without_comments": [{"community": cl[0], "phase": cl[1], "labeled": n} for cl, n in missing]})
    # 以降の仕事を足す
    new = []
    for i, s in enumerate(reps_sorted, 1):
        new.append(_task(st, "comments", "ai", f"コメント分析（{i}/{len(reps_sorted)}: seq {s}）",
                         {"seq": s, "i": i, "n": len(reps_sorted)}))
    comms = sorted({labs[s]["community"] for s in reps_sorted}, key=lambda k: min(recs[s]["date"] for s in reps_sorted if labs[s]["community"] == k))
    for i, k in enumerate(comms, 1):
        new.append(_task(st, "synthesis", "ai", f"界隈ごとの統合（{i}/{len(comms)}: {k}）", {"community": k, "i": i, "n": len(comms)}))
    chs = [f"path_{p['id']}" for p in ph] + ["branch", "music", "result", "intro"]
    for i, ch in enumerate(chs, 1):
        new.append(_task(st, "write", "ai", f"執筆（{i}/{len(chs)}: {CHAPTER_TITLES.get(ch) or '拡大経路 ' + ch[5:]}）",
                         {"chapter": ch, "i": i, "n": len(chs), "first": i == 1}))
    new.append(_task(st, "assemble", "service", "レポートの組み立て（サービス）"))
    order = ["intro"] + [f"path_{p['id']}" for p in ph] + REPORT_ORDER_FIXED
    for i, ch in enumerate(order, 1):
        new.append(_task(st, "finish", "ai", f"note 用に仕上げる（{i}/{len(order)}: {CHAPTER_TITLES.get(ch) or '拡大経路 ' + ch[5:]}）",
                         {"chapter": ch, "i": i, "n": len(order)}))
    new.append(_task(st, "finish_title", "ai", "題名と確認メモ"))
    new.append(_task(st, "assemble", "service", "note 用原稿の組み立て（サービス）"))
    new.append(_task(st, "verify", "service", "検算（サービス）"))
    new.append(_task(st, "export", "service", "Excel 用のデータ（サービス）"))
    new.append(_task(st, "done", "done", "完了"))
    insert_after(st, t, new)
    return {"reps": len(chosen), "candidates": len(cand), "cells": len(cells), "cells_without_comments": len(missing)}


def service_plan(a, t: dict, st: dict) -> dict:
    """以降の仕事を足す（2026-10-03〜の形）: 界隈ごとのコメント分析 → 参考記事 → 構成案 → 執筆 → 仕上げ"""
    c = cfg(a)
    recs = records(a)
    labs = labels(a)
    ph = phases(a)
    by_c = comment_seqs(a)
    all_cells = collections.Counter((l["community"], phase_of(recs[s]["date"], ph)) for s, l in labs.items() if l["community"] != "unknown")
    have = {(k, phase_of(recs[s]["date"], ph)) for k, ss in by_c.items() for s in ss}
    missing = sorted((cell, n) for cell, n in all_cells.items() if cell not in have)
    a.derived("ai").mkdir(parents=True, exist_ok=True)
    pr._write_json(a.derived("ai", "reps_summary.json"),
                   {"mode": "community", "comment_videos": sum(len(v) for v in by_c.values()), "communities": len(by_c),
                    "cells_without_comments": [{"community": cl[0], "phase": cl[1], "labeled": n} for cl, n in missing]})
    comms = sorted(by_c, key=lambda k: recs[by_c[k][0]]["date"])
    rc = t["params"].get("recut")    # 界隈の切り直しのあと（参考記事の章立ては界隈に依らないので、前の版のものを使い回す）
    reuse_refs = bool(rc) and refs_ready(a)
    new = []
    for i, k in enumerate(comms, 1):
        new.append(_task(st, "ccomments", "ai", f"コメント分析（界隈ごと {i}/{len(comms)}: {k}）", {"community": k, "i": i, "n": len(comms)}))
    if not reuse_refs:
        new.append(_task(st, "ref_select", "ai", "参考にする過去記事を選ぶ"))
        for i in range(1, c["n_refs"] + 1):
            new.append(_task(st, "ref_digest", "ai", f"参考記事の章立てと論理（{i}/{c['n_refs']}）", {"i": i, "n": c["n_refs"], "file": None}))
    # ウェブで調べる・時代背景の材料（2026-10-06〜）。切り直しのあとは、前の版のものを使い回す（曲の外のことは界隈に依らない）
    reuse_research = bool(rc) and research_ready(a)
    if not reuse_research:
        new.append(_task(st, "research", "ai", "ウェブで調べる（曲の情報・TikTok の外の指標・要のアカウント）"))
        new.append(_task(st, "era", "ai", "時代背景の材料（似た位置づけの曲の記事と、同じ時期の流行を読む）"))
    new.append(_task(st, "outline", "ai", "構成案（記事全体の主張と、章ごとの主張・根拠）"))
    chs = [f"path_{p['id']}" for p in ph] + ["branch", "music", "result", "intro"]
    for i, ch in enumerate(chs, 1):
        new.append(_task(st, "write", "ai", f"執筆（{i}/{len(chs)}: {CHAPTER_TITLES.get(ch) or '拡大経路 ' + ch[5:]}）",
                         {"chapter": ch, "i": i, "n": len(chs), "first": i == 1, **({"recut": rc} if rc else {})}))
    new.append(_task(st, "assemble", "service", "レポートの組み立て（サービス）"))
    order = ["intro"] + [f"path_{p['id']}" for p in ph] + REPORT_ORDER_FIXED
    for i, ch in enumerate(order, 1):
        new.append(_task(st, "finish", "ai", f"note 用に仕上げる（{i}/{len(order)}: {CHAPTER_TITLES.get(ch) or '拡大経路 ' + ch[5:]}）",
                         {"chapter": ch, "i": i, "n": len(order)}))
    new.append(_task(st, "finish_title", "ai", "題名と確認メモ"))
    new.append(_task(st, "assemble", "service", "note 用原稿の組み立て（サービス）"))
    new.append(_task(st, "verify", "service", "検算（サービス）"))
    new.append(_task(st, "export", "service", "Excel 用のデータ（サービス）"))
    new.append(_task(st, "done", "done", "完了（界隈の切り直し）" if rc else "完了", {"recut": rc} if rc else {}))
    insert_after(st, t, new)
    return {"communities": len(comms), "comment_videos": sum(len(v) for v in by_c.values()), "refs": c["n_refs"],
            "cells_without_comments": len(missing), "same_song_excluded": len(same_song_files(a)), "reuse_refs": reuse_refs,
            "reuse_research": reuse_research}


def refs_ready(a) -> bool:
    """参考記事の選択と章立ての抜き出しが揃っているか"""
    n = cfg(a)["n_refs"]
    return a.outputs("references", "selected.json").exists() and all(
        a.outputs("references", f"{i:02d}.md").exists() for i in range(1, n + 1))


def appendix(a) -> str:
    """付録（使ったデータと信頼度）。数字はサービスが原本から作る"""
    recs = records(a)
    labs = labels(a)
    acq = a.meta.get("acquisition") or {}
    steps = acq.get("steps") or {}
    cm = (steps.get("comments") or {}).get("detail") or {}
    va = video_analyses(a)
    summ = pr._read_json(a.derived("ai", "reps_summary.json"), {}) or {}
    conf = collections.Counter(l.get("conf") for l in labs.values())
    miss = summ.get("cells_without_comments") or []
    u = ugc_total(a)
    lines = ["## 9. 付録：使ったデータと信頼度", "",
             (f"- この曲の UGC 数（楽曲ページの表示）: {u['text']}（{u['at']} 時点）" if u and not u["parts"] else
              f"- この曲の UGC 数（同じ曲の楽曲ページ {len(u['parts'])} つの表示の合計）: {fmt_count(u['n'])}（{ugc_parts_line(u)}。{u['at']} 時点）"
              if u else "- この曲の UGC 数: 取得していない"),
             *([f"- 曲の公開日（楽曲ページが作られた日）: {rel['date']}。これより前の日付で楽曲ページに載っていた投稿 {rel['dropped']}本は、"
                "あとから音源が付いたものとして、すべての分析から除いた"] if (rel := release_info(a)) and rel["dropped"] else []),
             f"- 楽曲ページのグリッドから集めた動画: {len(recs)}本（取得 {str(acq.get('started_at', ''))[:10]}"
             + (f"。楽曲ページごと: " + "、".join(f"{x['label']} {m.get('links', '?')}本" for x, m in zip(u["parts"], music_pages(a)))
                if u and u["parts"] else "") + "）。"
             f"属性が取れた動画 {sum(1 for r in recs.values() if r.get('enriched'))}本（取れなかったものは写真投稿・削除済みなど）",
             f"- ラベルを付けた動画: {len(labs)}本（確信度 H {conf.get('H', 0)} / M {conf.get('M', 0)} / L {conf.get('L', 0)}）",
             f"- コメントを取った動画: {cm.get('videos_ok', '?')}本・{cm.get('comments', '?')}件"
             f"（候補 {cm.get('pool', '?')}本のうち。グリッドに無く差し替えた {cm.get('substituted', 0)}本、時間の上限で取らなかった {cm.get('not_fetched_time', 0)}本）",
             (f"- コメントを分析した代表: {len(va)}本" if va else
              f"- コメントを読んだ動画: {summ.get('comment_videos', '?')}本（界隈ごとにまとめて傾向を見て、具体の動画で確かめた）"),
             "- 数値（再生・いいね・本数）は取得時点の値。ここに書いた本数は経路を調べるために集めた投稿の数で、曲の UGC 数ではない",
             "- 楽曲ページのグリッドは推薦順で入れ替わるため、取得時にグリッドに出てこなかった動画（削除済み・古い投稿の一部。"
             "起点の動画を含むことがある）は扱えていない"]
    if miss:
        lines.append("- コメントが取れず根拠が薄い界隈×段階: " + "、".join(f"{m['community']}×{m['phase']}（{m['labeled']}本）" for m in miss[:15]))
    n_rc = recut_last_round(a)
    if n_rc:
        rec = recut_record(a, n_rc)
        lines.append(f"- 界隈の分け方: 初めの版のあと、利用者の指示で切り直した（{n_rc}回。最後は {str(rec.get('at', ''))[:10]}）。"
                     "切り直した界隈で、読むべき動画にコメントが無かったものは取り足した")
    res = research(a)
    if res is not None:
        if res.get("web_search"):
            lines += ["", f"### ウェブで調べたことの出どころ（{str(res.get('at', ''))[:10]} に AI が調べた。本文の [W番号]）", ""]
            lines += [f"- {research_line(it)}" for it in res.get("items") or []] or ["- （見つかったものは無い）"]
        else:
            lines.append("- ウェブで調べたこと: AI のウェブ検索が使えず、曲の情報と TikTok の外の指標は入っていない")
    return "\n".join(lines) + "\n"


def memo_md(a, meta: dict, chapters: list) -> str:
    """確認メモ（EDITOR_NOTES.md。利用者のフォルダでは「確認メモ.md」）: 公開の前に事実を確かめたいときのもの。書き足す場所の一覧ではない
    （2026-10-06〜。前は「書き足すところのメモ」だった。レポートは書き切る）"""
    if "guesses_md" not in meta and meta.get("editor_notes_md"):   # 前の形の仕上げ（2026-10-06 より前）: そのまま
        return meta["editor_notes_md"].strip() + "\n\n## 仕上げで変えたこと\n\n" + (meta.get("changes_md") or "").strip() + "\n"
    s = a.meta.get("song") or {}
    out = [f"# 確認メモ（{s.get('title', '')} / {s.get('artist', '')}）", "",
           "レポートは、そのまま読める・公開できる形に書き切ってあります。このメモは、公開の前に事実を確かめたいときに使ってください。", "",
           "## ウェブで調べたこと（出どころ）", ""]
    res = research(a)
    if res is None:
        out.append("この分析には、ウェブで調べる工程がありませんでした（UGC Analyzer 0.6.2 より前に始めた分析）。")
    elif not res.get("web_search"):
        out.append("AI のウェブ検索が使えなかったため、曲の情報と TikTok の外の数字（YouTube・チャートなど）は入っていません。"
                   "ウェブ検索をオンにして、AI に「" + a.title + "のレポートの6章に、TikTok の外の数字を調べて足して」と頼めば足せます。")
    else:
        out += [f"AI が {str(res.get('at', ''))[:10]} にウェブで調べた値です。数字は日々変わるので、公開の前に出どころを開いて確かめると確実です。"
                "（[W番号] は「レポート（根拠の番号つき）」の本文の印と同じです）", ""]
        for kind, head in RESEARCH_HEADS.items():
            xs = [it for it in res.get("items") or [] if it["kind"] == kind]
            if xs:
                out += [f"### {head}", ""] + [f"- {research_line(it)}" for it in xs] + [""]
        if res.get("not_found"):
            out += ["### 探したが見つからなかったもの（レポートには書いていません）", ""] + [f"- {x}" for x in res["not_found"]] + [""]
    out += ["", "## 推測で書いたところ", "",
            "本文で「〜と見ています」のように推測の形にした事実のうち、確かめれば言い切れるものです。", "",
            (meta.get("guesses_md") or "なし").strip(), ""]
    recs = records(a)
    gone = [x for x in mentioned_seqs(a, chapters) if x in recs and not recs[x].get("enriched")]
    if gone:
        out += ["## 削除済みで埋め込めなかった動画", ""] + [
            f"- @{(recs[x].get('author') or {}).get('id')}（{recs[x]['date']}）— 本文では「（現在は削除済み）」と書いています" for x in gone]
    return "\n".join(out).rstrip() + "\n"


def service_assemble(a, st: dict) -> dict:
    order = chapter_order(a, st)
    parts = []
    for ch in order:
        p = a.outputs("chapters", f"{ch}.md")
        if p.exists():
            parts.append(p.read_text(encoding="utf-8").strip())
    parts.append(appendix(a).strip())
    pr._write_text(a.outputs("REPORT.md"), "\n\n".join(parts) + "\n")
    notes = [a.outputs("note_chapters", f"{ch}.md") for ch in order]
    if all(p.exists() for p in notes):
        meta = pr._read_json(a.outputs("note_meta.json"), {}) or {}
        body = "\n\n".join(p.read_text(encoding="utf-8").strip() for p in notes)
        title = meta.get("title")
        if title and not body.lstrip().startswith("# "):
            body = f"# {title}\n\n{body}"
        pr._write_text(a.outputs("NOTE_BODY.md"), body + "\n")
        if meta.get("guesses_md") or meta.get("editor_notes_md"):
            pr._write_text(a.outputs("EDITOR_NOTES.md"), memo_md(a, meta, order))
    return {"report_chars": len(a.outputs("REPORT.md").read_text(encoding="utf-8")),
            "note": a.outputs("NOTE_BODY.md").exists()}


# 「seq N … 〇〇万再生」の照合。カンマ入りの数字（1,420万）を読み、ほかの動画（seq M）をまたいで数字を拾わない
# （2026-10-06: 掘り下げの試験で、元の版から同じ誤報3件が「数字の一部は運営が確認中」として利用者に出ていた）
PLAY_RE = re.compile(r"seq\s*(\d+)(?:(?!seq\s*\d)[^。\n]){0,60}?(?<![\d,.万億])(\d[\d,]*(?:\.\d+)?)\s*(万|億)?\s*(?:回)?再生")
# 「seq N、@投稿者、日付、再生 4,100,000」の形（執筆の指示どおりの書き方。前はこちらを照合していなかった。本番2曲の写しで 75・88 件、ずれ0）。
# 「再生数 50万」「再生回数50万回」「再生数は50万」も同じ形として照合する（2026-10-06 通し試験）
PLAY_RE_PRE = re.compile(r"seq\s*(\d+)(?:(?!seq\s*\d)[^。\n]){0,60}?再生(?:回?数)?\s*(?:は|[:：])?\s*(\d[\d,]*(?:\.\d+)?)\s*(万|億)?")
# 数字のあとの「突破・超え・以上」は下限（本文の数字 ≦ データなら一致）、「近く・弱」などは目安（データが本文の数字の少し下）
PLAY_BOUND_RE = re.compile(r"\s*(?:回)?\s*(?:再生)?\s*(?:を|が|に)?\s*(?:(突破|超え|超|越え|以上|オーバー)|(近く|近い|弱|足らず|手前|迫))")
# 数字のすぐあとに動画の「（seq M …）」が続くなら、その数字は M のもの（書き方「30.9万回再生（seq 28 …）」）。前の seq N の数字として照合しない
# （2026-10-06: きゃわの事例で、正しい本文から4〜6件の誤報が出て、完了の知らせに「数字の一部は運営が確認中」と出ていた）
FOLLOW_SEQ = re.compile(r"[^。、\n（(]{0,12}[（(]\s*seq\s*(\d+)")   # 数字と（seq M）の間は読点なし・12字まで（「〜まで伸びています（seq 39」）
PLAY_RE_POST = re.compile(r"(?<![\d,.万億])(\d[\d,]*(?:\.\d+)?)\s*(万|億)?\s*(?:回)?再生" + FOLLOW_SEQ.pattern)   # 「23万6,600回」の末尾だけは拾わない


def play_bound(text: str, pos: int):
    """数字（と万・億）のすぐあとの言い方: "low"（突破・超え・以上＝下限）・"about"（近く・弱など＝目安）・None（ちょうどの数）"""
    m = PLAY_BOUND_RE.match(text, pos)
    return None if not m else "low" if m.group(1) else "about"


def play_claims(text: str, bounds: bool = False) -> list:
    """本文が書いた動画の再生数を (seq, 数字の文字, 万・億, 抜き出し) で返す。3つの書き方:
    「seq N … 〇〇回再生」・「seq N、…、再生 数字」・「〇〇回再生（seq N …）」。
    bounds=True なら、5つ目に数字のあとの言い方（play_bound: 下限・目安・None）を足す"""
    out = {}
    for m in PLAY_RE.finditer(text):
        f = FOLLOW_SEQ.match(text, m.end())
        if f and int(f.group(1)) != int(m.group(1)):
            continue   # 数字は、あとに続く動画のもの（PLAY_RE_POST で照合する）
        if "再生" in text[m.end(1):m.start(2)]:
            continue   # seq N の再生数は（ ）の中で言い終えている。あとの数字は別の話（「〜と、100万回再生を超える投稿が続く」）
        out[m.start(2)] = (int(m.group(1)), m.group(2), m.group(3), m.group(0), play_bound(text, m.end(3) if m.group(3) else m.end(2)))
    for m in PLAY_RE_PRE.finditer(text):
        out[m.start(2)] = (int(m.group(1)), m.group(2), m.group(3), m.group(0), play_bound(text, m.end(3) if m.group(3) else m.end(2)))
    for m in PLAY_RE_POST.finditer(text):
        out[m.start(1)] = (int(m.group(3)), m.group(1), m.group(2), m.group(0), play_bound(text, m.end(2) if m.group(2) else m.end(1)))
    return [out[k] if bounds else out[k][:4] for k in sorted(out)]


def play_mismatch(num: str, unit_word, real: int, bound=None) -> bool:
    """本文の数字が実データとずれているか。6% か、書いた桁の丸めの幅（「2万」なら±5千、「30.9万」なら±500）の大きいほうまでは一致とみなす。
    bound="low"（「100万回再生を突破」）は本文の数字がデータ以下なら一致、bound="about"（「100万近く」「100万弱」）は
    データが本文の数字の8割から数字まで（丸めの幅を含む）なら一致"""
    digits = num.replace(",", "")
    unit = 10000 if unit_word == "万" else 100000000 if unit_word == "億" else 1
    step = 10 ** -len(digits.split(".")[1]) if "." in digits else 1
    claimed = float(digits) * unit
    tol = max(0.06 * real, step * unit / 2)
    if bound == "low":
        return claimed - real > tol
    if bound == "about":
        return real - claimed > tol or real < 0.8 * claimed - tol
    return abs(claimed - real) > tol


def service_verify(a, st: dict) -> dict:
    """検算（C9）: 引用した cid の実在・存在しない動画への言及・再生数の一致"""
    rep = a.outputs("REPORT.md").read_text(encoding="utf-8") if a.outputs("REPORT.md").exists() else ""
    recs = records(a)
    seqs, cids = cited(rep)
    known = all_cids(a)
    errors, warnings = [], []
    for s in sorted(seqs):
        if s not in recs:
            errors.append(f"存在しない動画への言及: seq {s}")
    for c_ in sorted(cids):
        if c_ not in known:
            errors.append(f"入力に無い cid: {c_}")
    for w in sorted({int(x) for x in WEB_REF_RE.findall(rep)} - research_ids(a)):
        errors.append(f"ウェブで調べたことに無い出どころの番号: W{w}")
    for s, num, unit, snip, bound in play_claims(rep, bounds=True):
        if s not in recs:
            continue
        real = recs[s]["plays"]
        if real and play_mismatch(num, unit, real, bound):
            warnings.append(f"seq {s} の再生数: 本文 {snip[-24:]} ／ データ {real:,}")
    note = a.outputs("NOTE_BODY.md").read_text(encoding="utf-8") if a.outputs("NOTE_BODY.md").exists() else ""
    if note:
        allowed = {url_of(v) for v in videos(a).values() if url_of(v)}
        for u in re.findall(r"https?://(?:www\.)?tiktok\.com/[^\s)）」、。]+", note):
            if u.split("?")[0] not in allowed:
                errors.append(f"note に一覧に無い URL: {u[:80]}")
        if INTERNAL_RE.search(note):
            warnings.append("note に内部の印が残っている: " + ", ".join(sorted({m.group(0) for m in INTERNAL_RE.finditer(note)})[:5]))
        if READER_RE.search(note):
            warnings.append("note に作業の言葉が残っている: " + ", ".join(sorted({m.group(0) for m in READER_RE.finditer(note)})[:5]))
    res = {"at": pr._now(), "seqs": len(seqs), "cids": len(cids), "errors": errors[:50], "warnings": warnings[:50]}
    pr._write_json(a.outputs("verify.json"), res)
    return {"errors": len(errors), "warnings": len(warnings), "seqs": len(seqs), "cids": len(cids)}


def service_export(a) -> dict:
    """Excel 用 ZIP（C10）: store.py で分析フォルダの原本から。SQLite は分析ごとの写し（derived/ugc.db）"""
    listing = a.derived("list.csv")
    comments = a.dir / "raw" / "comments.jsonl"
    if not listing.exists() or not comments.exists():
        return {"skipped": "list.csv か comments.jsonl が無い"}
    tmp = a.outputs("_zip")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    cmd = [sys.executable, str(BASE_DIR / "store.py"), "--db", str(a.derived("ugc.db")), "build",
           "--project", a.title, "--videos-csv", str(listing), "--comments-jsonl", str(comments),
           "--run-id", a.id, "--replace", "--out-dir", str(tmp)]
    r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONUTF8": "1"})
    zips = sorted(tmp.glob("*.zip"))
    if not zips:
        pr._write_text(a.outputs("export_error.txt"), (r.stdout or "") + "\n" + (r.stderr or ""))
        return {"error": "ZIP を作れなかった（export_error.txt）"}
    shutil.move(str(zips[-1]), str(a.outputs("data.zip")))
    shutil.rmtree(tmp, ignore_errors=True)
    n_lab = add_labels_to_zip(a, a.outputs("data.zip"))
    return {"zip": "data.zip", "check_ok": r.returncode == 0, "labels_rows": n_lab}


LABELS_README = """
labels.csv    1行 = 1動画（全動画。AI がラベルを付けていない動画はラベルの列が空）。分析で付けた区分:
                seq / video_id / URL / 投稿日 / 投稿者 / 再生   動画の特定（video_id で videos.csv と結合する）
                地域 / 言語            投稿地域と本文の言語（TikTok のデータからサービスが付けた。AI は付けていない）
                界隈 / 界隈の説明      誰の投稿か（確定した分類軸の key と、定義の最初の一文）
                型 / 採用文脈 / 規模   何をした投稿か / なぜこの曲か / 公式・大手・一般
                確からしさ / 根拠      H = 複数の手掛かりが一致、M = 1つ、L = 推測 / AI が書いた根拠
                段階                   拡散の段階（レポートの段階の名前）
"""


def add_labels_to_zip(a, zp: Path) -> int:
    """Excel 用 ZIP にラベルの表を足す（F4: 地域はサービスが付けた値。成果物の表に地域を入れる）。足した行数を返す"""
    import io
    import zipfile
    recs = records(a)
    labs = labels(a)
    if not recs or not labs:
        return 0
    vs = videos(a)
    tax = pr._taxonomy(a) or {}
    names = {k: pr._first_sentence(v).rstrip("。") for k, v in (tax.get("community") or {}).items()}
    ph = phases(a)
    ph_name = {p["id"]: p.get("name", "") for p in ph}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["seq", "video_id", "URL", "投稿日", "投稿者", "再生", "地域", "言語", "界隈", "界隈の説明",
                "型", "採用文脈", "規模", "確からしさ", "根拠", "段階"])
    for s in sorted(recs):
        r = recs[s]
        lab = labs.get(s) or {}
        url = url_of(vs.get(s, {}))
        m = re.search(r"/(?:video|photo)/(\d+)", url or "")
        com = lab.get("community") or ""
        w.writerow([s, m.group(1) if m else "", url, r.get("date"), (r.get("author") or {}).get("id"), r.get("plays"),
                    lab.get("region") or r.get("location_created") or "", r.get("text_language") or "",
                    com, names.get(com, "") if com else "", lab.get("format", ""), lab.get("motive", ""),
                    lab.get("tier", ""), lab.get("conf", ""), lab.get("reason", ""),
                    ph_name.get(phase_of(r["date"], ph), "") if ph and lab else ""])
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    tmp = zp.with_suffix(".tmp.zip")
    with zipfile.ZipFile(zp) as zi, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in zi.infolist():
            body = zi.read(item.filename)
            if item.filename.endswith("README.txt"):
                body = body + LABELS_README.encode("utf-8")
            zo.writestr(item, body)
        zo.writestr("labels.csv", data)
    tmp.replace(zp)
    return len(recs)


# ---------------------------------------------------------------------------
# read の資料（W1 で足した名前）
# ---------------------------------------------------------------------------
def units_of(a, name: str, st: dict):
    """read の名前 → ページに分ける前の単位の列。知らない名前なら None"""
    c = cfg(a)
    if name == "taxonomy_sample":
        head, body = a.entries("taxonomy_sample")
        seqs = axes_sample(a)
        note = (f"（軸の提案用に {len(body)} 本から {len(seqs)} 本に間引いた。再生上位・最初期は全部、各週の無作為は等間隔に）\n\n"
                if len(seqs) < len(body) else "")
        return [head + note] + [a.thumb_ref(body[s]) for s in seqs]
    if name.startswith("past:"):
        i = int(name.split(":", 1)[1]) - 1
        if not 0 <= i < len(c["past_reports"]):
            raise pr.RunnerError(f"{name} はありません")
        if c["past_reports"][i] in same_song_files(a):
            raise pr.RunnerError(f"{name} は分析する曲を扱った記事なので読まない（読まずに進める）")
        return _md_units(KB_DIR / "notes" / f"{c['past_reports'][i]}.md")
    if name.startswith("note:"):
        stem = re.sub(r"[^0-9A-Za-z_-]", "", name.split(":", 1)[1].replace(".md", ""))
        p = KB_DIR / "notes" / f"{stem}.md"
        if not p.exists():
            raise pr.RunnerError(f"{name} はありません（kb:cards の file から .md を除いた名前）")
        if stem in same_song_files(a):
            raise pr.RunnerError(f"{name} は分析する曲を扱った記事なので読めません")
        return _md_units(p)
    if name.startswith("kb:"):
        files = {"readme": "README.md", "glossary": "GLOSSARY.md", "style": "STYLE_GUIDE.md", "cards": "cards.jsonl",
                 "community": "COMMUNITY_GUIDE.md"}
        f = files.get(name.split(":", 1)[1])
        if not f:
            raise pr.RunnerError(f"{name} はありません（kb:readme / kb:glossary / kb:style / kb:cards / kb:community）")
        p = KB_DIR / "distilled" / f
        if not p.exists():
            raise pr.RunnerError("知識ベースが置かれていません（運営に連絡）")
        if f == "GLOSSARY.md":   # 新しい記事から足した語を後ろに付ける（kb_update.py）
            import kb_update
            return re.split(r"(?m)^(?=#{1,3} )", kb_update.glossary_text())
        if f == "COMMUNITY_GUIDE.md":   # 界隈の名付け方の手引き＋全レポートの例（分析する曲を扱った記事の例は外す）
            import kb_update
            return re.split(r"(?m)^(?=#{1,3} )", kb_update.community_guide_text(same_song_files(a)))
        if f.endswith(".jsonl"):   # カード: 分析する曲を扱った記事は外す
            return [json.dumps(c_, ensure_ascii=False) + "\n" for c_ in cards(a)]
        return _md_units(p)
    if name.startswith("chapter:"):
        ch = name.split(":", 1)[1]
        p = a.outputs("chapters", f"{ch}.md")
        if not p.exists():
            raise pr.RunnerError(f"{name} はまだありません")
        return _md_units(p)
    if name == "synthesis":
        d = a.outputs("synthesis")
        if not d.exists():
            raise pr.RunnerError("界隈ごとの統合はまだありません")
        return [f.read_text(encoding="utf-8") + "\n" for f in sorted(d.glob("*.md"))]
    if name.startswith("labeled:"):   # 界隈の切り直し: その界隈にラベルを付けた動画（再生の多い順）
        k = name.split(":", 1)[1]
        recs, labs, sheets = records(a), labels(a), sheet_index(a)
        ss = sorted((s for s, l in labs.items() if l["community"] == k and s in recs), key=lambda s: (-recs[s]["plays"], s))
        if not ss:
            have = sorted({l["community"] for l in labs.values()})
            raise pr.RunnerError(f"界隈 `{k}` にラベルの付いた動画はありません（今の界隈: {', '.join(have)}）")
        return [f"（界隈 `{k}` にラベルを付けた動画 {len(ss)}本。再生の多い順。各動画の最後の行が今のラベル）\n\n"] + [
            entry(a, recs[s], sheets).rstrip() + f"\n- 今のラベル: format={labs[s].get('format')} motive={labs[s].get('motive')} "
            f"tier={labs[s].get('tier')} conf={labs[s].get('conf')}（{labs[s].get('reason')}）\n\n" for s in ss]
    if name.startswith("prev:"):      # 界隈の切り直し: 前の版（切り直す前）の章
        n_rc = recut_last_round(a)
        p = recut_prev_dir(a, n_rc) / "chapters" / f"{name.split(':', 1)[1]}.md"
        if n_rc < 1 or not p.exists():
            raise pr.RunnerError(f"{name} はありません（前の版にこの章は無い）")
        return _md_units(p)
    if name.startswith("ccomments:"):
        return community_digest(a, name.split(":", 1)[1]) or ["（この界隈にはコメントを読める動画が無い）"]
    if name.startswith("dcomments:"):   # 界隈の掘り下げ: 1本あたりの件数を増やし、今回取り足した動画に印
        job = deepen_job(a)
        return (community_digest(a, name.split(":", 1)[1], c["deepen_digest_top"], deepen_seqs(a, job.get("round")))
                or ["（この界隈にはコメントを読める動画が無い）"])
    if name == "refs":
        d = a.outputs("references")
        fs = sorted(d.glob("[0-9][0-9].md")) if d.exists() else []
        if not fs:
            raise pr.RunnerError("参考記事の章立てと論理はまだありません")
        return [f.read_text(encoding="utf-8") + "\n" for f in fs]
    if name == "outline":
        if not a.outputs("outline.md").exists():
            raise pr.RunnerError("構成案はまだありません")
        return _md_units(a.outputs("outline.md"))
    if name == "research":
        if not a.outputs("research.md").exists():
            raise pr.RunnerError("ウェブで調べたことはありません（この分析にはその工程が無いか、まだ）")
        return _md_units(a.outputs("research.md"))
    if name == "era":
        if not a.outputs("era.md").exists():
            raise pr.RunnerError("時代背景の材料はありません（この分析にはその工程が無いか、まだ）")
        return _md_units(a.outputs("era.md"))
    if name.startswith("note_chapter:"):
        p = a.outputs("note_chapters", re.sub(r"[^0-9A-Za-z_]", "", name.split(":", 1)[1]) + ".md")
        if not p.exists():
            raise pr.RunnerError(f"{name} はまだありません（仕上げた章）")
        return _md_units(p)
    if name == "pathway":
        p = a.outputs("pathway.md")
        if not p.exists():
            raise pr.RunnerError("拡散経路の下書きはまだありません")
        return _md_units(p)
    return None


def _md_units(p: Path) -> list:
    return re.split(r"(?m)^(?=#{1,3} )", p.read_text(encoding="utf-8"))
