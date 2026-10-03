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
2026-10-03 に、代表の選定（reps）→ 1本ずつのコメント分析（comments）→ 界隈ごとの統合（synthesis）を、界隈ごとのコメント分析（ccomments）に
まとめ、執筆の前に参考記事・構成案を足した（ユーザー）。前の形で始めた分析は、前の形のまま最後まで回る（reps・comments・synthesis を残してある）。
"""
import collections
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
}
# 読者（Web の記事）に見せない作業の言葉。執筆と仕上げの検査で止める（2026-10-03 ユーザー）
READER_RE = re.compile(r"用語集|知識ベース|文体ガイド|因果パターン|過去レポート|過去記事のカード|カードの|カードに|"
                       r"今回集めた|集めた動画|グリッド|ラベル付き|ラベルを付け|ラベル上|\d+\s*本中")
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


def ugc_total(a):
    """楽曲ページに出ている UGC 数（取得のときに読んだもの）。{"n", "text", "at"} か None"""
    m = pr._read_json(a.dir / "raw" / "music_page.json", {}) or {}
    if not m.get("video_count"):
        return None
    return {"n": m["video_count"], "text": m.get("video_count_text") or "", "at": str(m.get("at") or "")[:10]}


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
        past = [f"past:{i}" for i in range(1, len(c["past_reports"]) + 1)]
        cat.append(pr._catalog_line(", ".join(past), "別の曲の過去レポート（著者の分析）。**最初に全ページ読む**"))
        cat.append(pr._catalog_line("taxonomy_sample", f"分類軸を考えるためのサンプル {len(body)} 本（page=1〜{len(pages)}）"))
        if sheets:
            cat.append(pr._catalog_line(", ".join(sheets), f"サムネイルの一覧画像 {len(sheets)} 枚（サンプルの動画が載っているもの）"))
        text = pr._fill(tpl("axes.md"), {**base, "n_sample": len(body), "n_pages": len(pages), "n_sheets": len(sheets),
                                         "sheet_list": "、".join(sheets) or "なし",
                                         "past_reports": "、".join(past),
                                         "settings": _settings(a, ["community_policy"], t)})

    elif typ == "confirm":
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
        text = pr._fill(tpl("label.md"), {**base, "batch": t["params"]["batch"], "n_batches": t["params"]["n_batches"],
                                          "n": len(seqs), "seq_list": ", ".join(map(str, seqs)), "entries": entries,
                                          "taxonomy_json": json.dumps(tax_view, ensure_ascii=False, indent=1),
                                          "settings": _settings(a, ["community_policy"], t)})

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

    elif typ == "outline":
        cat += [pr._catalog_line("refs", "参考記事の章立てと論理の運び"), pr._catalog_line("synthesis", "界隈ごとのコメント分析"),
                pr._catalog_line("pathway", "拡散経路の下書き")]
        lines = [f"- `{c_}`: {heading_of(a, c_)}（{CHAPTER_TITLES.get(c_) or '拡大経路の段階'}）" for c_ in chapter_order(a, st)]
        text = pr._fill(tpl("outline.md"), {**base, "n_refs": cfg(a)["n_refs"], "chapters": "\n".join(lines),
                                             "materials": outline_materials(a, base)})

    elif typ == "write":
        text, cat = write_prompt(a, t, base, st)

    elif typ == "finish":
        text = pr._fill(tpl("finish.md"), {**base, **finish_materials(a, t, st)})

    elif typ == "finish_title":
        ch = chapter_order(a, st)
        heads = "\n".join(f"- {first_line(a.outputs('note_chapters', f'{c_}.md'))}" for c_ in ch
                          if a.outputs("note_chapters", f"{c_}.md").exists())
        music = a.outputs("chapters", "music.md")
        deleted = [f"seq {s}" for s in mentioned_seqs(a, ch) if not records(a).get(s, {}).get("enriched")]
        text = pr._fill(tpl("finish_title.md"), {**base, "headings": heads,
                                                 "music_md": music.read_text(encoding="utf-8") if music.exists() else "（なし）",
                                                 "deleted": "、".join(deleted) or "なし"})

    elif typ == "revise":
        chs = chapter_order(a, st)
        lines = [f"- `{c_}`: {first_line(a.outputs('chapters', f'{c_}.md'))}" for c_ in chs]
        cat.append(pr._catalog_line("chapter:<id>", "章の今の原稿"))
        text = pr._fill(tpl("revise.md"), {**base, "instruction": t["params"]["instruction"], "chapters": "\n".join(lines),
                                           "settings": _settings(a, ["style", "focus"], t)})

    elif typ == "done":
        text = pr._fill(tpl("done.md"), {**base, **done_materials(a)})

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
            f"  （場所: `{shown}`。取得アプリのメニュー「レポートのフォルダを開く」でも開けます）"]


def dl_url(a, name: str) -> str:
    if REPORTS_DIR:
        from urllib.parse import quote
        src = a.dir / DELIVERABLES[name]
        dst = report_folder(a) / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.copy2(src, dst)
        return "file://" + quote(str(dst))
    return f"{PUBLIC_URL}/dl/{download_key(a)}/{name}"


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


def community_digest(a, k) -> list:
    """read の ccomments:<界隈>: 動画ごとに、見出しと、いいねの多いコメント（上位 digest_top 件）"""
    c = cfg(a)
    recs = records(a)
    ph = phases(a)
    units = []
    for s in comment_seqs(a).get(k, []):
        text, _ = a.comment_text(s)
        lines = text.splitlines()
        head = [ln for ln in lines[:12] if ln.startswith(("- 投稿者", "- 再生", "- 説明文"))]
        head = [ln if len(ln) < 160 else ln[:160] + "…" for ln in head]
        top = [ln for ln in lines if ln.startswith("- [")][:c["digest_top"]]
        top = [ln if len(ln) <= c["digest_chars"] else ln[:c["digest_chars"]] + "…" for ln in top]
        r = recs[s]
        units.append(f"### seq {s}（{r['date']}、段階 {phase_of(r['date'], ph) or '?'}）\n" + "\n".join(head) +
                     "\n" + "\n".join(top) + "\n\n")
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


# --- 構成案（2026-10-03〜） ---
def common_materials(a, base) -> str:
    """執筆・構成案の材料の頭: 曲全体の UGC 数を先に。集めた本数は「記事に書かない手掛かり」として"""
    recs = records(a)
    labs = labels(a)
    ph = phases(a)
    u = ugc_total(a)
    total_plays = sum(r["plays"] for r in recs.values())
    head = (f"- **この曲の UGC 数**（楽曲ページの表示、{u['at']} 時点）: {fmt_count(u['n'])}（表示「{u['text']}」）。記事の数字はこれで語る"
            if u else "- この曲の UGC 数: 取得していない（記事では UGC 数に触れず、再生数・期間・界隈の広がりで語る）")
    n_comm = sum(len(v) for v in comment_seqs(a).values())
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
    return ((f"- この曲の UGC 数: {fmt_count(u['n'])}（{u['at']} 時点）\n" if u else "") +
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
            "\n\n" + intro_materials(a))


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
            ev = "、".join(f"seq {e['seq']}" if "seq" in e else f"cid {e['cid']}" if "cid" in e else str(e.get("number", ""))
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
    if a.outputs("outline.json").exists():   # 構成案のある形（2026-10-03〜）: 過去記事は構成案の前に読んだ
        if first_write:
            kb = ("### 最初に読むもの（執筆の最初の仕事だけ）\n\n著者の分析の言語と文体を身につける: read の `kb:glossary`（全ページ）→ `kb:style`。"
                  "内容を会話に書き写さない。以降の執筆の仕事では読み直さなくてよい（必要なら読み直してよい）。")
            cat += [pr._catalog_line("kb:glossary", "著者の用語集（A〜J 章）"), pr._catalog_line("kb:style", "文体ガイド（章立て・定型）")]
        else:
            kb = "著者の用語集（`kb:glossary`）と文体ガイド（`kb:style`）は最初の執筆の仕事で読んだ。必要なら read で読み直してよい。"
            cat.append(pr._catalog_line("kb:glossary, kb:style", "用語集・文体ガイド（読み直すとき）"))
        cat.append(pr._catalog_line("outline", "構成案の全体"))
    elif first_write:
        kb = ("### 最初に読むもの（執筆の最初の仕事だけ）\n\n"
              "著者の分析の言語を身につける: read の `kb:readme` → `kb:glossary`（全ページ）→ `kb:style` → `kb:cards`（全ページ）。\n"
              f"`kb:cards` から、この楽曲に近い過去レポート（同じ型のバズ・似た界隈・似た経路）を**最大{c['extra_past_reports']}本**選び、"
              "`note:<file の .md を除いた名前>` で全文を読む（軸の提案で読んだ `past:1`・`past:2` も読み直してよい）。"
              "選んだ理由は書かなくてよい。内容を会話に書き写さない。\n以降の執筆の仕事では読み直さなくてよい（必要なら読み直してよい）。")
        for nm, d in (("kb:readme", "知識ベースの説明"), ("kb:glossary", "用語集（A〜J 章）"), ("kb:style", "文体ガイド（章立て・定型）"),
                      ("kb:cards", "過去レポート82本のカード（1行1本）"), ("note:<名前>", "過去レポートの全文")):
            cat.append(pr._catalog_line(nm, d))
    else:
        kb = "著者の用語集（`kb:glossary`）と文体ガイド（`kb:style`）は最初の執筆の仕事で読んだ。必要なら read で読み直してよい。"
        cat.append(pr._catalog_line("kb:glossary, kb:style", "用語集・文体ガイド（読み直すとき）"))
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
                     "\n\n拡大経路の章は read の `chapter:path_P1` など、界隈ごとの統合は `synthesis`、下書きは `pathway`。")
        cat += [pr._catalog_line("chapter:path_P1 …", "書き終えた拡大経路の章"), pr._catalog_line("synthesis", "界隈ごとの統合"),
                pr._catalog_line("pathway", "拡散経路の下書き")]
        spec = ("まず `### ここまでの拡大経路をまとめると` で段階の流れを矢印などで短く再掲する。次に `## 2. バズの分岐点とその理由` を立て、"
                "分岐点（0→1、界隈を越えた瞬間、公式の合流など）を3〜6個、`### 分岐点① …` の形で、それぞれ理由をデータで書く。"
                "最後に `### 【重要な補足】この曲で起きなかったこと`（よく言われる通説〔知識ベースの J 章〕がこの曲には当てはまらない点など。"
                "記事に出どころは書かない）。")
        heading, length = heading_of(a, ch), "2,000〜4,000字"
    elif ch == "music":
        materials = music_materials(a) + "\n\n" + common
        spec = ("`## 3. 楽曲の音楽的特徴（内的要因）`・`## 4. 楽曲構成の整理（切り出し箇所）`・`## 5. 時代背景における本楽曲の立ち位置` の3つの見出しを立てる。"
                "このデータには音源・歌詞・年代別のリスナーが無い。**データで言えること（尺・元音源の割合・提案語・画面の文字・コメントの言及）だけを短く書き、"
                "言えない部分は「ここは人の考察を入れる場所」と書いて空ける。埋めない。**")
        heading, length = heading_of(a, ch), "800〜2,000字"
    elif ch == "result":
        materials = result_materials(a) + "\n\n" + common + "\n\n分岐点の章は read の `chapter:branch`。"
        cat.append(pr._catalog_line("chapter:branch", "書き終えた分岐点の章"))
        spec = ("`## 6. バズった結果得られたもの`（TikTok 内のデータで言えることだけ。TikTok 外の指標は「本データでは扱えない」と明記）・"
                "`## 7. 今回のヒットの核`（一文で言い切る）・`## 8. 再現性のある要素`（著者の型: 自分でコントロール下に置ける要素だけを①②③で3〜4項目、"
                "見出しは命令形・名詞句、運の部分は「ここは運」と明示）の3つの見出しを立てる。")
        heading, length = heading_of(a, ch), "1,500〜3,500字"
    elif ch == "intro":
        materials = common + "\n\n" + intro_materials(a) + "\n\n書き終えた章は read の `chapter:branch`・`chapter:result`（結論の先出しに使う）。"
        cat += [pr._catalog_line("chapter:branch, chapter:result", "書き終えた章")]
        spec = (f"題名 `{heading_of(a, ch)} 〜TikTok今週の1曲 [No.— - YY/MM-K]` の行で始め、文体ガイドの冒頭（挨拶＋対象宣言）→ `## 本楽曲に着目すべき理由`"
                "（**この曲の UGC 数**・期間・逆説の数字。「※UGCとは…」の定型注記。3パターンの型判定「本楽曲は②…に該当します。何故なら…」）→ `### 先に結論`"
                "（構成案の記事全体の主張を2〜3行の箇条書きで）→ `## 分析方針`（章立ての予告と「それではいきましょう。」）。"
                "UGC 数は材料の「この曲の UGC 数」を使う。こちらが集めた動画の本数は書かない（方法に触れるなら、"
                "「楽曲ページの投稿から、広がり方の手掛かりになるものを選んで調べた」のように本数を出さずに一言）。")
        heading, length = heading_of(a, ch), "1,000〜2,000字"
    else:
        raise pr.RunnerError(f"知らない章です: {ch}")
    title = CHAPTER_TITLES.get(ch) or f"拡大経路 {phmap[ch[5:]]['name']}"
    text = pr._fill(tpl("write.md"), {**base, "chapter_title": title, "i": t["params"]["i"], "n": t["params"]["n"],
                                      "kb_block": kb, "chapter_spec": spec, "materials": materials, "heading": heading,
                                      "plan": plan_block(a, ch, st),
                                      "length": length, "settings": _settings(a, ["style", "focus"], t)})
    return text, cat


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
    return {"chapter_title": CHAPTER_TITLES.get(ch) or first_line(a.outputs("chapters", f"{ch}.md")), "i": t["params"]["i"],
            "n": t["params"]["n"], "chapter_md": md,
            "phase_names": "\n".join(f"- {p['id']} → {p['name']}（{p['start']}〜{p['end']}）" for p in phases(a)),
            "community_names": "\n".join(f"- `{k}` → {pr._first_sentence(v)}" for k, v in tax["community"].items() if not k.startswith("_")),
            "video_list": "\n".join(lst) or "（この章に動画は出てこない）",
            "settings": _settings(a, ["style"], t)}


def done_materials(a) -> dict:
    ver = pr._read_json(a.outputs("verify.json"), {}) or {}
    links = []
    for name, label in (("REPORT.md", "分析レポート"), ("NOTE_BODY.md", "note 用の原稿"),
                        ("EDITOR_NOTES.md", "書き足すところのメモ"), ("data.zip", "Excel 用のデータ")):
        if (a.outputs(name)).exists():
            links.append(f"- [{label}（{name}）]({dl_url(a, name)})")
    ph = phases(a)
    summary = "\n".join(f"- {p['name']}（{p['start']}〜{p['end']}）: {p.get('summary', '')}" for p in ph)
    errs = ver.get("errors") or []
    warns = ver.get("warnings") or []
    note = ""
    if errs or warns:
        note = f"- 検算: 確かめきれなかった点が {len(errs) + len(warns)} 件ある（運営が確認する）。利用者には「数字の一部を運営が確認中」と一言だけ"
    if REPORTS_DIR and links:   # 先頭にフォルダ（2026-10-03 ユーザー「最終の返答に、成果物フォルダや成果物へのリンクを含んで欲しい」）
        links = folder_lines(a) + links
    head = "**成果物**（この Mac に保存しました）" if REPORTS_DIR else "**成果物**"
    return {"links": head + "\n\n" + ("\n".join(links) or "- （成果物のリンクを作れなかった）"),
            "summary": summary, "verify_note": note}


# ---------------------------------------------------------------------------
# 検査と取り込み
# ---------------------------------------------------------------------------
def check_examples(tax: dict, sample_seqs: set, strict=True) -> list:
    errs = []
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
    return errs


INTERNAL_RE = re.compile(r"seq\s*\d+|\bcid\b|\bP\d\b|records\.jsonl|本データ|先行工程", re.I)


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


def accept(a, t: dict, raw: str, st: dict) -> list:
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

    if typ == "ref_select":
        obj, err = pr._parse_json(raw)
        if err:
            return [err]
        refs = (obj or {}).get("references") if isinstance(obj, dict) else None
        n = cfg(a)["n_refs"]
        if not isinstance(refs, list) or len(refs) != n:
            return [f"references を {n} 本の配列にしてください"]
        ok = {c_["file"]: c_ for c_ in cards(a)}
        errs = []
        files = [str((r or {}).get("file", "")).strip() for r in refs]
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
            for x in cl:
                for e in (x or {}).get("evidence") or []:
                    if isinstance(e, dict) and "seq" in e and e["seq"] not in recs:
                        errs.append(f"`{c_.get('id')}` の根拠に存在しない seq {e['seq']}")
                    if isinstance(e, dict) and "cid" in e and str(e["cid"]) not in known:
                        errs.append(f"`{c_.get('id')}` の根拠に入力に無い cid {e['cid']}")
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
        if not isinstance(obj, dict) or not obj.get("title") or not obj.get("editor_notes_md"):
            return ["title と editor_notes_md を入れてください"]
        pr._write_json(a.outputs("note_meta.json"), obj)
        return []

    if typ == "revise":
        obj, err = pr._parse_json(raw)
        if err:
            return [err]
        ch = (obj or {}).get("chapter")
        if ch not in chapter_order(a, st):
            return [f"chapter は章の一覧の id から: {chapter_order(a, st)}"]
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
            if not date_re.match(str(p.get(kk, ""))):
                errs.append(f"P{i} の {kk} は YYYY-MM-DD にしてください")
        bad = [s for s in p.get("reps") or [] if s not in labs]
        if bad:
            errs.append(f"P{i} の reps にラベルの付いていない seq: {bad[:5]}")
    if errs:
        return errs[:20]
    if ph[0]["start"] > dates[0]:
        errs.append(f"最初の段階の start は {dates[0]} 以前にしてください")
    if ph[-1]["end"] < dates[-1]:
        errs.append(f"最後の段階の end は {dates[-1]} 以降にしてください")
    for p, q in zip(ph, ph[1:]):
        nxt = (datetime.date.fromisoformat(p["end"]) + datetime.timedelta(days=1)).isoformat()
        if q["start"] != nxt:
            errs.append(f"{q['id']} の start は {p['id']} の end の翌日（{nxt}）にしてください")
        if q["start"] > q["end"]:
            errs.append(f"{q['id']} の start が end より後です")
    if not obj.get("pathway_md"):
        errs.append("pathway_md（拡散経路の下書き）を書いてください")
    if errs:
        return errs
    pr._write_json(a.outputs("phases.json"), {"phases": ph, "overlooked": [s for s in obj.get("overlooked") or [] if s in recs]})
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
    new.append(_task(st, "finish_title", "ai", "題名と執筆メモ"))
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
    new = []
    for i, k in enumerate(comms, 1):
        new.append(_task(st, "ccomments", "ai", f"コメント分析（界隈ごと {i}/{len(comms)}: {k}）", {"community": k, "i": i, "n": len(comms)}))
    new.append(_task(st, "ref_select", "ai", "参考にする過去記事を選ぶ"))
    for i in range(1, c["n_refs"] + 1):
        new.append(_task(st, "ref_digest", "ai", f"参考記事の章立てと論理（{i}/{c['n_refs']}）", {"i": i, "n": c["n_refs"], "file": None}))
    new.append(_task(st, "outline", "ai", "構成案（記事全体の主張と、章ごとの主張・根拠）"))
    chs = [f"path_{p['id']}" for p in ph] + ["branch", "music", "result", "intro"]
    for i, ch in enumerate(chs, 1):
        new.append(_task(st, "write", "ai", f"執筆（{i}/{len(chs)}: {CHAPTER_TITLES.get(ch) or '拡大経路 ' + ch[5:]}）",
                         {"chapter": ch, "i": i, "n": len(chs), "first": i == 1}))
    new.append(_task(st, "assemble", "service", "レポートの組み立て（サービス）"))
    order = ["intro"] + [f"path_{p['id']}" for p in ph] + REPORT_ORDER_FIXED
    for i, ch in enumerate(order, 1):
        new.append(_task(st, "finish", "ai", f"note 用に仕上げる（{i}/{len(order)}: {CHAPTER_TITLES.get(ch) or '拡大経路 ' + ch[5:]}）",
                         {"chapter": ch, "i": i, "n": len(order)}))
    new.append(_task(st, "finish_title", "ai", "題名と執筆メモ"))
    new.append(_task(st, "assemble", "service", "note 用原稿の組み立て（サービス）"))
    new.append(_task(st, "verify", "service", "検算（サービス）"))
    new.append(_task(st, "export", "service", "Excel 用のデータ（サービス）"))
    new.append(_task(st, "done", "done", "完了"))
    insert_after(st, t, new)
    return {"communities": len(comms), "comment_videos": sum(len(v) for v in by_c.values()), "refs": c["n_refs"],
            "cells_without_comments": len(missing), "same_song_excluded": len(same_song_files(a))}


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
             (f"- この曲の UGC 数（楽曲ページの表示）: {u['text']}（{u['at']} 時点）" if u else "- この曲の UGC 数: 取得していない"),
             f"- 楽曲ページのグリッドから集めた動画: {len(recs)}本（取得 {str(acq.get('started_at', ''))[:10]}）。"
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
    return "\n".join(lines) + "\n"


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
        if meta.get("editor_notes_md"):
            pr._write_text(a.outputs("EDITOR_NOTES.md"), meta["editor_notes_md"].strip() + "\n\n## 仕上げで変えたこと\n\n" +
                           (meta.get("changes_md") or "").strip() + "\n")
    return {"report_chars": len(a.outputs("REPORT.md").read_text(encoding="utf-8")),
            "note": a.outputs("NOTE_BODY.md").exists()}


PLAY_RE = re.compile(r"seq\s*(\d+)[^。\n]{0,60}?(\d+(?:\.\d+)?)\s*(万|億)?\s*(?:回)?再生")


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
    for m in PLAY_RE.finditer(rep):
        s = int(m.group(1))
        if s not in recs:
            continue
        val = float(m.group(2)) * (10000 if m.group(3) == "万" else 100000000 if m.group(3) == "億" else 1)
        real = recs[s]["plays"]
        if real and abs(val - real) / real > 0.06:
            warnings.append(f"seq {s} の再生数: 本文 {m.group(0)[-20:]} ／ データ {real:,}")
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
        files = {"readme": "README.md", "glossary": "GLOSSARY.md", "style": "STYLE_GUIDE.md", "cards": "cards.jsonl"}
        f = files.get(name.split(":", 1)[1])
        if not f:
            raise pr.RunnerError(f"{name} はありません（kb:readme / kb:glossary / kb:style / kb:cards）")
        p = KB_DIR / "distilled" / f
        if not p.exists():
            raise pr.RunnerError("知識ベースが置かれていません（運営に連絡）")
        if f == "GLOSSARY.md":   # 新しい記事から足した語を後ろに付ける（kb_update.py）
            import kb_update
            return re.split(r"(?m)^(?=#{1,3} )", kb_update.glossary_text())
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
    if name.startswith("ccomments:"):
        return community_digest(a, name.split(":", 1)[1]) or ["（この界隈にはコメントを読める動画が無い）"]
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
    if name == "pathway":
        p = a.outputs("pathway.md")
        if not p.exists():
            raise pr.RunnerError("拡散経路の下書きはまだありません")
        return _md_units(p)
    return None


def _md_units(p: Path) -> list:
    return re.split(r"(?m)^(?=#{1,3} )", p.read_text(encoding="utf-8"))
