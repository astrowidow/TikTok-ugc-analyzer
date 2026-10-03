"""
知識ベースを新しくする（docs/ALL_IN_APP_PLAN.md 段4。2026-10-03 ユーザー「随時アップデートする仕組みを入れておかないと、
時間の経過とともにレガシー化していってしまう」「元は著者の note の新しい記事だけ」）。

知識ベースの置き場（環境変数 UGC_KB_DIR。flow_w1 と同じ）:
  notes/<日付>_<key>.md          著者の記事の本文（取り込んだもの）
  INDEX.json                      記事の一覧（key・題・日付・値段・ファイル名…）
  distilled/GLOSSARY.md          用語集（A〜J 章）
  distilled/GLOSSARY_ADDITIONS.json  新しい記事から足した語（章ごと。kb:glossary を読むと GLOSSARY.md の後ろに付く）
  distilled/STYLE_GUIDE.md       文体ガイド（最初の版では固定）
  distilled/cards.jsonl          記事1本1行のカード（新しい記事の分を足していく）
  update_state.json               最後に新着を見た時刻・取り込み待ちの記事・整理待ちの章

流れ:
  1. check_new(): 著者の note の公開の一覧を見て、知らない記事の本文を保存し「取り込み待ち」にする
     （取得アプリが週1回。利用者が「知識ベースを更新して」と言ったときも）
  2. 次に AI が next_task を呼んだとき、分析の仕事の合間に「取り込み」の仕事を渡す（proto_runner がはさむ）:
     記事1本 → カード1行と、用語集に足す語（0〜5件）
  3. 1つの章に足した語が増えたら（MERGE_AT 字）、その章を書き直す「整理」の仕事を1つはさむ（用語集が際限なく伸びないように）

TikTok には触らない。note への要求は、新着を見るときの一覧数ページと、新しい記事1本につき1回だけ。
"""
import contextlib
import datetime
import html
import json
import os
import re
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CREATOR = "keitarocomp"
LIST_URL = "https://note.com/api/v2/creators/{creator}/contents?kind=note&page={page}"
NOTE_URL = "https://note.com/api/v3/notes/{key}"
CHECK_EVERY_DAYS = 7
MAX_PAGES = 20                 # 新着を探す一覧のページの上限（1ページ6本前後）
ARTICLE_MAX_CHARS = 24_000     # 取り込みの仕事に載せる本文の上限（最長の記事は約2.3万字）
CARD_MAX_CHARS = 700           # カード1行（既存は最長 639字）
ADDITION_MAX_CHARS = 300       # 用語集に足す1件
ADDITIONS_PER_ARTICLE = 5
MERGE_AT = 2_500               # 章ごとの追記がこの字数を超えたら整理の仕事をはさむ
SECTIONS = {"A": "著者の分析の型", "B": "バズの定義と分類", "C": "拡散段階", "D": "界隈", "E": "フォーマット",
            "F": "採用文脈・動機", "G": "楽曲構造と切り出し箇所", "H": "SNS指標の読み方", "I": "因果パターン",
            "J": "否定された通説"}
CARD_KEYS = ["file", "title", "date", "kind", "song", "artist", "platform", "buzz_type", "pathway", "communities",
             "formats", "why_claims", "reproducible", "evidence_style", "coined_terms", "numbers", "reusable_insight"]
CARD_LISTS = {"communities", "formats", "why_claims", "reproducible", "coined_terms"}
KINDS = ["楽曲分析", "流行曲Report", "講座", "コラム", "その他"]
PLATFORMS = ["TikTok", "YouTube", "両方", "その他"]


class KbError(Exception):
    """利用者に返す、日本語の短いエラー"""


def kb_dir() -> Path:
    return Path(os.environ.get("UGC_KB_DIR", BASE_DIR / "output" / "knowledge"))


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _write_text(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _write_json(p: Path, data) -> None:
    _write_text(p, json.dumps(data, ensure_ascii=False, indent=1))


@contextlib.contextmanager
def _locked(timeout: float = 20):
    """取得アプリ（新着を見る）と AI の道具（取り込む）が別のプロセスで同じファイルを書くので、1つずつにする"""
    lock = kb_dir() / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 120:   # 持ち主が落ちて残ったもの
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() - t0 > timeout:
                raise KbError("知識ベースを別の処理が使っています。少し待ってからもう一度")
            time.sleep(0.2)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def state() -> dict:
    s = _read_json(kb_dir() / "update_state.json", {}) or {}
    s.setdefault("pending", [])
    s.setdefault("merge_pending", [])
    s.setdefault("ingested", [])
    return s


def _save_state(s: dict) -> None:
    _write_json(kb_dir() / "update_state.json", s)


def index() -> list:
    return _read_json(kb_dir() / "INDEX.json", []) or []


def ready() -> bool:
    return (kb_dir() / "distilled" / "GLOSSARY.md").exists()


# ---------------------------------------------------------------------------
# 1. 新着を見る
# ---------------------------------------------------------------------------
def html_to_text(body: str) -> str:
    """note の本文（HTML）を、既存の記事ファイルと同じ「1段落1行」のテキストにする"""
    t = body or ""
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", t)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|h[1-6]|li|blockquote|figcaption|pre|tr|div)>", "\n", t)
    t = re.sub(r"(?i)<li\b[^>]*>", "・", t)
    t = re.sub(r"(?i)</t[dh]>", "\t", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace(" ", " ")
    lines = [ln.rstrip() for ln in t.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln.strip():
            blank = True
            continue
        out.append(ln)
        blank = False
    return "\n".join(out).strip() + "\n"


def _get_json(url: str):
    import requests
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (UGC Analyzer knowledge update)",
                                               "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def due(s: dict | None = None) -> bool:
    s = s or state()
    last = s.get("last_check")
    if not last:
        return True
    try:
        age = datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(last)
    except ValueError:
        return True
    return age.days >= CHECK_EVERY_DAYS


def check_new(log=print, fetch=None, sleep: float = 1.0) -> dict:
    """著者の note の新着を見て、新しい記事を保存し、取り込み待ちにする。結果 {"new": [...], "checked_pages": n}"""
    fetch = fetch or _get_json
    if not ready():
        raise KbError("知識ベースが置かれていません")
    known = {x.get("key") for x in index()}
    new_items = []
    pages = 0
    for page in range(1, MAX_PAGES + 1):
        data = (fetch(LIST_URL.format(creator=CREATOR, page=page)) or {}).get("data") or {}
        pages += 1
        contents = data.get("contents") or []
        fresh = [c for c in contents if c.get("key") and c["key"] not in known]
        new_items += fresh
        unpinned = [c for c in contents if not c.get("isPinned")]
        # 新しい順に並んでいるので、固定表示以外が全部知っている記事のページまで来たら、その先は見ない
        if data.get("isLastPage") or not contents or (unpinned and all(c["key"] in known for c in unpinned)):
            break
        time.sleep(sleep)
    saved = []
    for c in new_items:
        try:
            d = (fetch(NOTE_URL.format(key=c["key"])) or {}).get("data") or {}
        except Exception as e:   # 1本取れなくても残りは続ける（次の確認でまた試す）
            log(f"知識ベース: 記事 {c['key']} の本文を取れませんでした: {e}")
            continue
        time.sleep(sleep)
        body = html_to_text(d.get("body") or c.get("body") or "")
        date = str(d.get("publish_at") or c.get("publishAt") or "")[:10]
        name = d.get("name") or c.get("name") or c["key"]
        fname = f"{date}_{c['key']}.md"
        url = c.get("noteUrl") or f"https://note.com/{CREATOR}/n/{c['key']}"
        text = f"# {name}\n\n{url}\n公開: {date}\n\n{body}"
        _write_text(kb_dir() / "notes" / fname, text)
        saved.append({"key": c["key"], "name": name, "date": date, "price": c.get("price") or 0,
                      "canRead": bool(d.get("can_read", c.get("canRead"))), "chars": len(body), "url": url,
                      "free_chars": len(body), "file": fname, "taboo_hits": 0, "taboo_terms": "",
                      "added_at": _now()})
    with _locked():
        idx = index()
        have = {x.get("key") for x in idx}
        added = [x for x in saved if x["key"] not in have]
        if added:
            _write_json(kb_dir() / "INDEX.json", idx + added)
        s = state()
        s["pending"] += [x["key"] for x in added if x["key"] not in s["pending"]]
        s["last_check"] = _now()
        s["last_result"] = {"at": s["last_check"], "new": [x["file"] for x in added], "pages": pages}
        _save_state(s)
    if added:
        log(f"知識ベース: 新しい記事 {len(added)} 本を保存しました（取り込み待ち）: " + ", ".join(x["file"] for x in added))
    return {"new": [x["file"] for x in added], "checked_pages": pages}


# ---------------------------------------------------------------------------
# 2. 取り込み・整理の仕事（proto_runner の next_task / submit がはさむ）
# ---------------------------------------------------------------------------
def additions() -> dict:
    return _read_json(kb_dir() / "distilled" / "GLOSSARY_ADDITIONS.json", {}) or {}


def glossary_text() -> str:
    """kb:glossary で読ませる用語集（GLOSSARY.md と、新しい記事から足した語）"""
    base = (kb_dir() / "distilled" / "GLOSSARY.md").read_text(encoding="utf-8")
    add = additions()
    if not any(add.values()):
        return base
    parts = [base.rstrip(), "", "---", "", "## 追記（新しい記事から足した語。上の定義と食い違うときは、こちらが新しい見方）", ""]
    for sec in SECTIONS:
        items = add.get(sec) or []
        if not items:
            continue
        parts.append(f"### {sec}. {SECTIONS[sec]}")
        parts += [f"- **{x['term']}** — {x['text']}｜出典: {x.get('title') or x.get('file')}（{x.get('date', '')}）" for x in items]
        parts.append("")
    return "\n".join(parts) + "\n"


def pending_count() -> int:
    s = state()
    return len(s["pending"]) + len(s["merge_pending"])


def next_task():
    """次の取り込み・整理の仕事（無ければ None）"""
    if not ready():
        return None
    s = state()
    if s["merge_pending"]:
        sec = s["merge_pending"][0]
        return {"task_id": f"kb/merge-{sec}", "kind": "ai", "type": "kb_merge", "section": sec,
                "title": f"知識ベースの整理（用語集の {sec} 章）"}
    for key in list(s["pending"]):
        item = next((x for x in index() if x.get("key") == key), None)
        if item and (kb_dir() / "notes" / item["file"]).exists():
            return {"task_id": f"kb/card-{key}", "kind": "ai", "type": "kb_card", "key": key, "item": item,
                    "title": f"知識ベースの取り込み（{item['name'][:40]}）"}
    return None


def _section_span(text: str, sec: str):
    m = re.search(rf"(?m)^## {sec}\. .*$", text)
    if not m:
        return None
    nxt = re.search(r"(?m)^## [A-Z]\. ", text[m.end():])
    end = m.end() + nxt.start() if nxt else len(text)
    # 最後の章の後ろの区切り（---）は章に含めない
    return m.start(), end


def render(t: dict) -> str:
    left = pending_count()
    head = (f"# 知識ベースの仕事: {t['title']}\n\n- task_id: `{t['task_id']}`\n- kind: `ai`\n"
            f"- 残り: この仕事を含めて {left} 件（分析の仕事の前にはさんでいる。済んだら分析の続きに戻る）\n\n")
    if t["type"] == "kb_card":
        it = t["item"]
        body = (kb_dir() / "notes" / it["file"]).read_text(encoding="utf-8")
        cut = ""
        if len(body) > ARTICLE_MAX_CHARS:
            body, cut = body[:ARTICLE_MAX_CHARS], f"\n（本文が長いので {ARTICLE_MAX_CHARS} 字で切った）\n"
        sample = next(iter(reversed((kb_dir() / "distilled" / "cards.jsonl").read_text(encoding="utf-8").splitlines())), "")
        return head + f"""## 指示書

著者（山本慶太朗）の note の新しい記事を、知識ベースに取り込む。知識ベースは、分析レポートを書くときに AI が読む「著者の分析の言語」。
記事を読み、(1) カード1行と、(2) 用語集に足す語（0〜{ADDITIONS_PER_ARTICLE}件）を作る。

- カードは今のカードと同じ形（下の例）。中身は要約で、数字・言い回しは記事にあるものだけ。記事に無いことを足さない
- 用語集に足すのは、この記事で**初めて出た語**か、**定義や見方が変わった語**だけ。どの章（{', '.join(f'{k} {v}' for k, v in SECTIONS.items())}）に入るかを決める。
  見方が変わった語は、text に「（以前は〜。この記事から〜）」と変化を書く。足す語が無ければ空の配列でよい
- 用語集の今の中身は read の `kb:glossary` で読める（同じ語を二重に足さないため、迷ったら見る）

### 記事（{it['file']}、公開 {it['date']}）

{body}{cut}
### 今のカードの例（形を合わせる）

```json
{sample}
```

### 出力の形（JSON 1つ）

```json
{{"card": {{"title": "短い題", "kind": "{'|'.join(KINDS)}", "song": "曲名か null", "artist": "アーティストか null",
  "platform": "{'|'.join(PLATFORMS)}", "buzz_type": "文字列か null", "pathway": "拡大経路の要約",
  "communities": ["…"], "formats": ["…"], "why_claims": ["…"], "reproducible": ["…"], "evidence_style": "…",
  "coined_terms": ["…"], "numbers": "主な数字（短く）", "reusable_insight": "他の曲にも使える見方"}},
 "glossary": [{{"section": "D", "term": "語", "text": "定義・使い方（{ADDITION_MAX_CHARS}字まで）"}}]}}
```

- カード1行（JSON にしたとき）は {CARD_MAX_CHARS} 字まで。file と date はサービスが入れる
"""
    sec = t["section"]
    text = (kb_dir() / "distilled" / "GLOSSARY.md").read_text(encoding="utf-8")
    span = _section_span(text, sec)
    cur = text[span[0]:span[1]].rstrip() if span else f"## {sec}. {SECTIONS[sec]}\n"
    add = additions().get(sec) or []
    lines = "\n".join(f"- **{x['term']}** — {x['text']}｜出典: {x.get('title') or x.get('file')}（{x.get('date', '')}）" for x in add)
    limit = _merge_limit(cur)
    return head + f"""## 指示書

知識ベースの用語集の {sec} 章（{SECTIONS[sec]}）に、新しい記事から足した語がたまった。章を1つに書き直す。

- 今の章の語は全部残す。足した語は、同じ語なら今の項目に統合し、新しい語なら項目を足す
- 見方が変わった語は「初期→後期」の形で書き、後期を今の見方にする（用語集の凡例と同じ）
- 書き方は今の章の書き方に合わせる（`**用語**（別表記）— 定義｜初出・根拠: 記事名（日付）｜例`）
- 長さは {limit} 字まで（用語集を読む枠を食い潰さないため。古い言い回しや重複を削って収める）

### 今の章

{cur}

### 足した語（{len(add)} 件）

{lines}

### 出力の形（Markdown）

`## {sec}. ` で始まる章の全文だけ（前置き・説明は書かない）。
"""


def _merge_limit(cur: str) -> int:
    return max(int(len(cur) * 1.1), len(cur) + 800)


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", t, re.S)
    return m.group(1).strip() if m else t


def check_card(obj) -> list:
    errs = []
    if not isinstance(obj, dict):
        return ["JSON 1つ（{\"card\": …, \"glossary\": […]}）で返してください"]
    card = obj.get("card")
    if not isinstance(card, dict):
        errs.append("`card` がありません")
    else:
        for k in CARD_KEYS:
            if k in ("file", "date"):
                continue
            if k not in card:
                errs.append(f"card に `{k}` がありません")
            elif k in CARD_LISTS and not isinstance(card[k], list):
                errs.append(f"card の `{k}` は配列にしてください")
        if card.get("kind") not in KINDS:
            errs.append(f"card の kind は {' / '.join(KINDS)} のどれか")
        if card.get("platform") not in PLATFORMS:
            errs.append(f"card の platform は {' / '.join(PLATFORMS)} のどれか")
    gl = obj.get("glossary", [])
    if not isinstance(gl, list):
        errs.append("`glossary` は配列にしてください（足す語が無ければ []）")
        gl = []
    if len(gl) > ADDITIONS_PER_ARTICLE:
        errs.append(f"用語集に足す語は {ADDITIONS_PER_ARTICLE} 件まで（今 {len(gl)} 件）")
    for i, g in enumerate(gl, 1):
        if not isinstance(g, dict) or g.get("section") not in SECTIONS or not str(g.get("term") or "").strip() \
                or not str(g.get("text") or "").strip():
            errs.append(f"glossary の {i} 件目は section（A〜J）・term・text がそろっていません")
        elif len(str(g["text"])) > ADDITION_MAX_CHARS:
            errs.append(f"glossary の {i} 件目（{g['term']}）の text が {len(str(g['text']))} 字です（{ADDITION_MAX_CHARS}字まで）")
    return errs


def accept(task_id: str, raw: str) -> list:
    """取り込み・整理の結果を検査して保存する。差し戻しの理由（空なら受け取った）"""
    t = next_task()
    if t is None or t["task_id"] != task_id:
        raise KbError(f"{task_id} は今の知識ベースの仕事ではありません。next_task を呼んでください")
    if t["type"] == "kb_card":
        try:
            obj = json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            return [f"JSON として読めません（{e.msg}、{e.lineno}行目）。JSON 1つだけを返してください"]
        errs = check_card(obj)
        it = t["item"]
        card = {"file": it["file"], "date": it["date"], **{k: v for k, v in (obj.get("card") or {}).items()
                                                           if k not in ("file", "date")}}
        card = {k: card.get(k) for k in CARD_KEYS} | {k: v for k, v in card.items() if k not in CARD_KEYS}
        line = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        if not errs and len(line) > CARD_MAX_CHARS:
            errs.append(f"カード1行が {len(line)} 字です（{CARD_MAX_CHARS}字まで）。要約を短くしてください")
        if errs:
            return errs
        with _locked():
            p = kb_dir() / "distilled" / "cards.jsonl"
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and json.loads(ln).get("file") != it["file"]]
            _write_text(p, "\n".join(lines + [line]) + "\n")
            add = additions()
            for g in obj.get("glossary") or []:
                add.setdefault(g["section"], []).append({"term": str(g["term"]).strip(), "text": str(g["text"]).strip(),
                                                         "file": it["file"], "title": it["name"][:60], "date": it["date"]})
            _write_json(kb_dir() / "distilled" / "GLOSSARY_ADDITIONS.json", add)
            s = state()
            s["pending"] = [k for k in s["pending"] if k != t["key"]]
            s["ingested"].append({"key": t["key"], "file": it["file"], "at": _now(),
                                  "glossary": len(obj.get("glossary") or [])})
            for sec, items in add.items():
                if sum(len(x["text"]) + len(x["term"]) for x in items) > MERGE_AT and sec not in s["merge_pending"]:
                    s["merge_pending"].append(sec)
            _save_state(s)
        return []
    # 整理
    sec = t["section"]
    md = _strip_fence(raw)
    gp = kb_dir() / "distilled" / "GLOSSARY.md"
    text = gp.read_text(encoding="utf-8")
    span = _section_span(text, sec)
    cur = text[span[0]:span[1]].rstrip() if span else ""
    errs = []
    if not re.match(rf"^## {sec}\. ", md):
        errs.append(f"`## {sec}. ` で始まる章の全文だけを返してください")
    if re.search(r"(?m)^## (?!" + sec + r"\. )[A-Z]\. ", md):
        errs.append("ほかの章の見出しが入っています。この章だけを返してください")
    limit = _merge_limit(cur) if cur else 6000
    if len(md) > limit:
        errs.append(f"{len(md)} 字あります（{limit} 字まで）。重複や古い言い回しを削ってください")
    if cur and len(md) < len(cur) * 0.6:
        errs.append(f"今の章（{len(cur)} 字）より大きく短くなっています（{len(md)} 字）。今の章の語は全部残してください")
    if errs:
        return errs
    with _locked():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        _write_text(gp.with_name(f"GLOSSARY.md.bak-{stamp}"), text)
        new = (text[:span[0]] + md.rstrip() + "\n\n" + text[span[1]:].lstrip("\n")) if span else text.rstrip() + "\n\n" + md + "\n"
        _write_text(gp, new)
        add = additions()
        add.pop(sec, None)
        _write_json(kb_dir() / "distilled" / "GLOSSARY_ADDITIONS.json", add)
        s = state()
        s["merge_pending"] = [x for x in s["merge_pending"] if x != sec]
        s.setdefault("merged", []).append({"section": sec, "at": _now(), "chars": len(md)})
        _save_state(s)
    return []


def summary() -> str:
    """状態の短い説明（取得アプリのメニューと AI の道具で使う）"""
    if not ready():
        return "知識ベースが置かれていません"
    s = state()
    n = len(index())
    last = (s.get("last_check") or "")[:10] or "まだ"
    pend = len(s["pending"]) + len(s["merge_pending"])
    return (f"記事 {n} 本・最後に新着を見たのは {last}"
            + (f"・取り込み待ち {pend} 件（次に「〇〇の分析を続けて」と言ったときに片付ける）" if pend else ""))
