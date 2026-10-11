"""
道具 info の中身（読むだけ）。分析やアプリについての質問に、記録から答える。docs/PLAN_0_8.md 第3章。

2026-10-12 友達の会話: 「自己紹介（MBTI の歌詞の切り出し）の音源は入ってないよね？」に、Claude は道具で答えられず、
Bash でアプリのフォルダを覗いて「60秒の公式の音源に入っている可能性が高い」と推測で答えた（実際はファンの音源 38.9K に乗っていて、
その分析は、ファンの音源を探さない版で集めていた）。取得が終わると、どのページ・どの音源を取ったか・どの版で取ったかが、
道具から1つも見えなかった。ここでは、記録があれば数字で答え、無ければ「記録が無い」と、その理由まで言う。

about:
- overview: 楽曲ページ（取った・外した）・本数・コメント・設定・取得の日時と版・AI の進み
- sounds: 音源ごとの長さ・本数、ファンの音源を探したか・見つけたもの・入れたもの・外したもの（target に楽曲ページの URL なら、それが入っているか）
- video: 動画1本（target に URL・動画の番号・seq）。台帳にあるか・詳しく読んだか・コメントの件数・界隈
- report: レポートの版・検算・確認メモ・界隈ごとの本数・ウェブ検索・界隈の確認の答え
- app: アプリの版・道具の版・ずれ・TikTok のログイン・取得用の Chrome・最近のエラー・通知の出し方（runner.LOCAL.app_info）
"""
import json
import re
from pathlib import Path

FAN_SINCE = "0.6.7"      # 同じ曲のファンの音源を探すようになった版（docs/FAN_SOUNDS.md）
LEDGER_SINCE = "0.7.0"   # 一覧を台帳（raw/ledger.jsonl）にした版（docs/LIST_CUT.md 第6章）
FAN_JOIN_RATIO = 0.3


def _j(p: Path, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _rows(p: Path):
    """jsonl を1行ずつ（壊れた行は飛ばす）"""
    try:
        f = open(p, encoding="utf-8")
    except OSError:
        return
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield r


def _n(x) -> str:
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return "?"


def _id_of(url) -> str:
    return str(url or "").split("?")[0].rstrip("/").rsplit("-", 1)[-1]


def _date(s) -> str:
    return str(s or "")[:16].replace("T", " ")


def _ver(a) -> str:
    v = a.meta.get("app_version")
    return f"UGC Analyzer {v}" if v else "記録なし（0.8.0 より前の版で作った分析）"


def _steps(a) -> dict:
    return (a.meta.get("acquisition") or {}).get("steps") or {}


def _detail(a, step) -> dict:
    return (_steps(a).get(step) or {}).get("detail") or {}


def _pages(a) -> list:
    """取った楽曲ページ（目録の順）に、受け付け・取得のときに読んだ記録を重ねる"""
    urls = [u for u in (a.meta.get("music_urls") or [a.meta.get("music_url")]) if u]
    seen = {}
    for r in _j(a.dir / "raw" / "music_pages.json", []) or []:
        if isinstance(r, dict) and r.get("url"):
            seen[_id_of(r["url"])] = r
    fan = {_id_of(u) for u in a.meta.get("fan_music_urls") or []}
    out = []
    for i, u in enumerate(urls, 1):
        r = dict(seen.get(_id_of(u)) or {})
        r["url"] = u
        r["page"] = i
        if _id_of(u) in fan:
            r["kind"] = "fan"
        out.append(r)
    return out


def _page_line(r: dict) -> str:
    kind = "ファンが上げた同じ曲の音源" if r.get("kind") == "fan" else "公式の音源"
    bits = [f"『{r.get('title') or '題は記録なし'}』（{r.get('creator') or '作者は記録なし'}）", kind]
    if r.get("duration"):
        bits.append(f"{r['duration']}秒")
    if r.get("video_count_text") or r.get("video_count"):
        bits.append(f"UGC {r.get('video_count_text') or _n(r.get('video_count'))}")
    if r.get("ledger") is not None:
        bits.append(f"一覧（台帳）{_n(r['ledger'])}本")
    if r.get("links") is not None:
        bits.append(f"詳しく読んだ {_n(r['links'])}本")
    return "・".join(bits) + f"\n    {r['url']}"


def _settings_lines(a, runner) -> list:
    s = a.meta.get("acquisition_settings") or {}
    pool = _detail(a, "pool")
    out = []
    out.append("返信: " + ("取る" if runner.wants_replies(a.meta) else "取らない（既定）"))
    mp = pool.get("min_plays_weekly") or s.get("min_plays_weekly")
    out.append("週ごとに選ぶ動画の再生の下限: " + (f"{_n(mp)}" if mp is not None else "既定（10万）"))
    if "fan_sounds" in s:
        out.append("ファンの音源を探す: " + ("探す" if int(s.get("fan_sounds") or 0) else "探さない（利用者が楽曲ページを渡した分析）"))
    if s.get("read_select") == 0:
        out.append("詳しく読む動画: 台帳を全部")
    elif s.get("read_normal"):
        out.append(f"詳しく読む動画: 約{_n(s['read_normal'])}本（上限 {_n(s.get('read_cap'))}）")
    opts = _j(a.dir / "state" / "options.json", {}) or {}
    if opts.get("skip_confirm"):
        out.append("界隈の確認: 省く（利用者の頼み）")
    return out


# ---------------------------------------------------------------------------
def overview(a, runner) -> str:
    m = a.meta
    acq = m.get("acquisition") or {}
    L = [f"# {a.title}（{a.song_line}）", f"- 分析 ID: {a.id}", f"- 作った日時: {_date(m.get('created_at'))}・作った版: {_ver(a)}"]
    if m.get("superseded_by"):
        L.append(f"- この分析は取り直し済み（{_date(m.get('superseded_at'))}）。新しい分析: {m['superseded_by']}")
    if m.get("archived_at"):
        L.append(f"- この分析はしまってある（{_date(m.get('archived_at'))}）。archive_analysis（restore=true）で戻せる")
    st = acq.get("status")
    names = {"queued": "順番待ち", "running": "取得中", "done": "集め終わった", "failed": "止まった", "cancelled": "やめた"}
    line = f"- 取得: {names.get(st, st or '記録なし')}"
    if acq.get("started_at"):
        line += f"（{_date(acq.get('started_at'))} 開始"
        line += f"・{_date(acq.get('finished_at'))} 終了）" if acq.get("finished_at") else "）"
    L.append(line)
    vers = sorted({(v or {}).get("app_version") for v in _steps(a).values() if (v or {}).get("app_version")})
    if vers:
        L.append("- 取得した版: " + "・".join(f"UGC Analyzer {v}" for v in vers)
                 + ("（途中で版が変わった）" if len(vers) > 1 else ""))
    elif st == "done":
        L.append("- 取得した版: 記録なし（0.8.0 より前の版で取った）")
    if acq.get("error"):
        L.append(f"- 止まった理由: {acq['error']}")
    L.append("- 設定: " + "／".join(_settings_lines(a, runner)))

    pages = _pages(a)
    L.append(f"\n## 取った楽曲ページ（{len(pages)}つ）")
    for r in pages:
        L.append(f"- 音源{chr(64 + r['page']) if r['page'] <= 26 else r['page']}: {_page_line(r)}")
    choice = _j(a.dir / "raw" / "page_choice.json")
    L.append("\n## 受け付けのときに外した楽曲ページ")
    if choice is None:
        L.append("- 記録なし（0.8.0 より前の版で受け付けた分析。外したページは、受け付けのときの返事にだけ出ていた）")
    elif not choice.get("dropped"):
        L.append(f"- なし（{choice.get('how') or ''}{choice.get('found_via') or ''}）")
    else:
        for d in choice["dropped"]:
            L.append(f"- 『{d.get('title') or '?'}』（{d.get('creator') or '?'}）・UGC {d.get('video_count_text') or _n(d.get('video_count'))}"
                     f" … {d.get('why')}\n    {d.get('url')}")
    L.append("（ファンの音源で外したものは info の about=sounds）")

    lst, enr, pool, com = _detail(a, "list"), _detail(a, "enrich"), _detail(a, "pool"), _detail(a, "comments")
    L.append("\n## 集めた本数")
    if lst.get("ledger") is not None:
        L.append(f"- 一覧（台帳）: {_n(lst['ledger'])}本（楽曲ページのグリッドに並んだ動画。再生数・投稿日まで）")
        if lst.get("links") is not None:
            why = lst.get("reasons") or {}
            L.append(f"- 詳しく読んだ動画: {_n(lst['links'])}本" + (
                "（" + "・".join(f"{k} {v}" for k, v in why.items()) + "）" if why else ""))
    elif lst.get("links") is not None or st == "done":
        L.append(f"- 一覧: グリッドで集めた {_n(lst.get('links'))}本（台帳は無い。{LEDGER_SINCE} より前の取り方）")
    if lst.get("dropped_before_release"):
        L.append(f"- 曲の公開より前の投稿で外した: {_n(lst['dropped_before_release'])}本")
    if enr:
        L.append(f"- 属性: 取れた {_n(enr.get('ok'))}本・取れなかった {_n(enr.get('fail'))}本（写真の投稿・削除された動画など）")
    if pool:
        L.append(f"- コメントを取る動画: {_n(pool.get('n_pool'))}本")
    if com:
        L.append(f"- コメント: {_n(com.get('videos_ok'))}本の動画から {_n(com.get('comments'))}件（返信 {_n(com.get('replies'))}件）"
                 + ("・TikTok に一時止められた" if com.get("blocked") else ""))
    dh = (m.get("deepen_history") or []) + ([m["deepen"]] if m.get("deepen") else [])
    for d in dh:
        kind = "切り直し" if d.get("kind") == "recut" else f"掘り下げ（{d.get('community')}）"
        L.append(f"- 取り足し（{kind}・{_date(d.get('requested_at'))}）: {d.get('status')}・対象 {len(d.get('targets') or [])}本")

    L.append("\n## AI の作業")
    try:
        stt = runner._state(a)
    except Exception:
        stt = None
    if not stt:
        L.append("- まだ始まっていない" if st == "done" else "- 集め終わってから始まる")
    else:
        cur = runner._current(stt)
        L.append(f"- 進み具合: {runner._progress(stt)}" + (f"（次は「{cur['title']}」）" if cur and cur["kind"] != "done" else "（完成）"))
    return "\n".join(L)


def sounds(a, target: str = "") -> str:
    m = a.meta
    pages = _pages(a)
    ss = _j(a.dir / "raw" / "sound_search.json")
    fan_detail = _detail(a, "resolve").get("fan_sounds")
    s = m.get("acquisition_settings") or {}
    L = [f"# 音源: {a.title}（分析 ID: {a.id}・作った版: {_ver(a)}）", "", "## 取った音源"]
    snd = {_id_of(r.get("url")): r for r in _j(a.dir / "derived" / "llm_input" / "sounds.json", []) or [] if isinstance(r, dict)}
    counts = {}
    for r in _rows(a.dir / "derived" / "llm_input" / "records.jsonl"):
        counts[r.get("sound")] = counts.get(r.get("sound"), 0) + 1
    for r in pages:
        tag = chr(64 + r["page"]) if r["page"] <= 26 else str(r["page"])
        x = snd.get(_id_of(r["url"])) or {}
        if x.get("duration") and not r.get("duration"):
            r["duration"] = x["duration"]
        extra = f"・詳しく読んだ動画のうちこの音源 {counts[tag]}本" if counts.get(tag) else ""
        L.append(f"- 音源{tag}: {_page_line(r)}{extra}")
    L.append("\n## 同じ曲のファンの音源を探したか")
    if ss is None:
        if isinstance(fan_detail, dict) and fan_detail.get("error"):
            L.append(f"- 探そうとして失敗した（{fan_detail['error']}）。受け付けで決めたページだけで取った")
        elif "fan_sounds" in s and not int(s.get("fan_sounds") or 0):
            L.append("- 探していない（利用者が楽曲ページを渡した分析は、探さない決まりだった）")
        else:
            L.append(f"- **探していない**。この分析は、ファンの音源を探す前の版で集めた（ファンの音源探しは {FAN_SINCE} から。"
                     f"この分析: {_ver(a)}）。ファンが曲の別の部分を切り抜いて上げた音源は、1つも入っていない")
            L.append("- 入れるには、新しい版で集め直す（restart_analysis。ページの URL が分かっていれば add_music_urls に。"
                     "分からなければ何も付けずに「最初から」で、ファンの音源も探す）")
    else:
        main = ss.get("main_video_count")
        L.append(f"- 探した（{_date(ss.get('at'))}）: 語 {len(ss.get('words') or [])}個（{'・'.join(ss.get('words') or [])}）、"
                 f"動画 {_n(ss.get('videos'))}本の音源を数えた。同じ曲と分かった候補 {_n(ss.get('candidates'))}個")
        L.append(f"- 入れる線: 主の楽曲ページの UGC（{_n(main)}）の{int(FAN_JOIN_RATIO * 100)}%以上")
        checked = sorted((c for c in ss.get("checked") or [] if isinstance(c, dict)),
                         key=lambda c: -(c.get("video_count") or 0))
        for c in checked:
            pct = f"{(c.get('video_count') or 0) / main * 100:.0f}%" if main and c.get("video_count") else "?"
            if c.get("joined"):
                res = "入れた"
            elif c.get("unavailable"):
                res = "外した（日本の地域で使えない）"
            elif not c.get("video_count"):
                res = "外した（UGC 数が読めなかった）"
            else:
                res = f"外した（{int(FAN_JOIN_RATIO * 100)}%未満）"
            L.append(f"- {res}: 『{c.get('title')}』（{c.get('author')}）・{c.get('duration') or '?'}秒・UGC {c.get('video_count_text') or '?'}"
                     f"（主の {pct}）・見分け方 {c.get('match') or '?'}\n    {c.get('url')}")
        if not checked:
            L.append("- 同じ曲のほかの音源は見つからなかった")
    if target:
        tid = _id_of(target)
        L.append("\n## 聞かれた音源")
        if any(_id_of(r["url"]) == tid for r in pages):
            r = next(r for r in pages if _id_of(r["url"]) == tid)
            L.append(f"- 入っている（音源{chr(64 + r['page'])}）")
        elif ss and any(c.get("id") == tid or _id_of(c.get("url")) == tid for c in ss.get("checked") or []):
            L.append("- 探したときに見つけたが、入れていない（上の一覧の理由）。入れるなら restart_analysis の add_music_urls に")
        else:
            L.append("- 入っていない。" + ("探した範囲にも出てこなかった。" if ss else "この分析はファンの音源を探していない。")
                     + "入れるなら restart_analysis の add_music_urls にこの URL を")
    return "\n".join(L)


def video(a, target: str) -> str:
    t = str(target or "").strip()
    if not t:
        return "動画の URL か番号（動画 ID・seq）を target に入れてください"
    vid = None
    seq = None
    m_ = re.search(r"/(?:video|photo)/(\d+)", t)
    if m_:
        vid = m_.group(1)
    elif re.fullmatch(r"\d{15,}", t):
        vid = t
    elif re.fullmatch(r"(?:seq\s*)?\d{1,5}", t, re.I):
        seq = int(re.sub(r"\D", "", t))
    rec = None
    for r in _rows(a.dir / "derived" / "llm_input" / "records.jsonl"):
        if (vid and str(r.get("video_id")) == vid) or (seq is not None and r.get("seq") == seq):
            rec = r
            vid = str(r.get("video_id"))
            break
    L = [f"# 動画: {t}（分析 {a.title}・{a.id}）"]
    led = None
    if vid:
        for r in _rows(a.dir / "raw" / "ledger.jsonl"):
            if str(r.get("video_id")) == vid:
                led = r
                break
    pages = _pages(a)
    if led:
        src = int(led.get("source") or 1)
        pg = next((r for r in pages if r["page"] == src), {})
        L.append(f"- 一覧（台帳）にある: 音源{chr(64 + src)}（『{pg.get('title') or '?'}』）のページ・{led.get('create_time')}・再生 {_n(led.get('plays'))}")
    elif (a.dir / "raw" / "ledger.jsonl").exists():
        L.append("- 一覧（台帳）に無い（取った楽曲ページのグリッドに出てこなかったか、別の音源の動画）")
    if rec:
        L.append(f"- 詳しく読んだ（seq {rec.get('seq')}・{rec.get('date')}・再生 {_n(rec.get('plays'))}・音源{rec.get('sound') or '?'}"
                 f"・属性 {'取れた' if rec.get('enriched') else '取れなかった（写真の投稿・削除など）'}）")
        cm = None
        for r in _rows(a.dir / "raw" / "comments.jsonl"):
            if str(r.get("video_id")) == vid and r.get("status") == "ok":
                cm = r
        files = sorted((a.dir / "derived" / "comments").glob(f"{rec.get('seq')}_*.md"))
        if cm:
            L.append(f"- コメント: {_n(cm.get('fetched'))}件取った（この動画のコメントの総数 {_n(cm.get('total'))}）。"
                     + (f"read の comments:{rec.get('seq')} で読める" if files else ""))
        else:
            L.append("- コメント: 取っていない（コメントを取る動画に選ばれなかった）")
        labs = a.dir / "outputs" / "labels.tsv"
        if labs.exists():
            head, *body = labs.read_text(encoding="utf-8").splitlines()
            cols = head.split("\t")
            for line in body:
                f = line.split("\t")
                if f and f[0] == str(rec.get("seq")):
                    row = dict(zip(cols, f))
                    L.append(f"- 界隈: {row.get('community')}・投稿の型 {row.get('format')}・使った理由 {row.get('motive')}")
                    break
    elif led:
        L.append("- 詳しくは読んでいない（再生の少ない動画などは台帳にだけ残す。週ごとの本数には数えている）。"
                 "読ませたいなら restart_analysis の read_more")
    if not led and not rec:
        L.append("- この分析の記録には無い（取った楽曲ページの一覧に出てこなかった。別の音源の動画かもしれない。info の about=sounds で取った音源を確かめる）")
    return "\n".join(L)


def report(a, runner, versions=None) -> str:
    out = a.dir / "outputs"
    L = [f"# レポート: {a.title}（分析 ID: {a.id}）"]
    if versions:
        L.append("\n## 版")
        for v in versions:
            L.append(f"- {v.get('name')}: {_date(v.get('at'))} {v.get('what') or ''}".rstrip())
    elif (out / "REPORT.md").exists():
        hist = sorted(p.name for p in (out / "history").iterdir()) if (out / "history").is_dir() else []
        L.append("\n## 版\n- 第1版" + "".join(f"\n- {h}" for h in hist))
    else:
        L.append("- まだ完成していない")
    ver = _j(out / "verify.json")
    if isinstance(ver, dict):
        errs, warns = ver.get("errors") or [], ver.get("warnings") or []
        L.append(f"\n## 検算: 誤り {len(errs)}・注意 {len(warns)}")
        for w in (errs + warns)[:5]:
            L.append(f"- {w if isinstance(w, str) else json.dumps(w, ensure_ascii=False)[:160]}")
    res = _j(out / "research.json")
    if isinstance(res, dict):
        L.append(f"\n## ウェブで調べた: {'使えた' if res.get('web_search') else '使えなかった'}・調べた事実 {len(res.get('items') or [])}件"
                 f"・見つからなかったもの {len(res.get('not_found') or [])}件")
    ca = _j(out / "confirm_answer.json")
    if isinstance(ca, dict):
        ans = ca.get("user_answer") or ca.get("answer") or ca.get("text") or json.dumps(ca, ensure_ascii=False)[:200]
        L.append(f"\n## 界隈の確認の答え: {ans}" + ("（案を直した）" if ca.get("modified") else ""))
    labs = out / "labels.tsv"
    if labs.exists():
        tax = _j(out / "taxonomy.json", {}) or {}
        names = {}
        com = tax.get("community") or {}
        items = com.items() if isinstance(com, dict) else ((c.get("key"), c) for c in com if isinstance(c, dict))
        for k, c in items:
            if k and isinstance(c, dict):
                names[k] = c.get("short_name") or c.get("name") or c.get("label") or str(c.get("definition") or k)[:30]
            elif k:   # 定義の文（「本家（iLiFE!）。界隈ではなく…」）の頭を短い名前に
                names[k] = re.split(r"[。（(、]", str(c), maxsplit=1)[0][:20] or k
        cnt = {}
        head, *body = labs.read_text(encoding="utf-8").splitlines()
        ci = head.split("\t").index("community") if "community" in head.split("\t") else 1
        for line in body:
            f = line.split("\t")
            if len(f) > ci:
                cnt[f[ci]] = cnt.get(f[ci], 0) + 1
        L.append("\n## 界隈ごとの本数（ラベルを付けた動画）")
        for k, n in sorted(cnt.items(), key=lambda x: -x[1]):
            L.append(f"- {names.get(k, k)}（{k}）: {n}本")
    if (out / "EDITOR_NOTES.md").exists():
        L.append("\n確認メモ（出どころ・推測で書いたところ）は、利用者のフォルダの「確認メモ.md」。read の memo で読める")
    return "\n".join(L)


NOTIFY_TEXT = ("UGC Analyzer は署名していないアプリなので、Mac の通知センターは使えない（ログの「Notifications are not allowed」は想定どおり）。"
               "通知は「スクリプトエディタ」の名前で出る。届かないときは、システム設定の「通知」で「スクリプトエディタ」をオンにする。")


def app(info: dict | None) -> str:
    if info is None:
        return "アプリの状態は読めなかった（記録に無い）。\n- 通知: " + NOTIFY_TEXT
    L = ["# UGC Analyzer の状態"]
    disk, tool = info.get("app_version_disk"), info.get("tool_version")
    L.append(f"- 入っているアプリの版: {disk or '分からない'}・この会話の道具の版: {tool or '分からない'}")
    if info.get("mismatch"):
        L.append("- **版がずれている**: アプリを新しくしたあと、Claude（ChatGPT）を開き直していない。⌘Q で終了して開き直す（会話と分析は消えない）")
    L.append(f"- メニューバーのアプリ: {'動いている' if info.get('menu_app_running') else '動いていない（アプリケーションの「UGC Analyzer」を開く）'}")
    if info.get("login_wanted"):
        L.append("- TikTok: ログインを待っている（UGC Analyzer が開いた Chrome で、分析専用のサブアカウントでログイン）")
    elif info.get("logged_in_at"):
        L.append(f"- TikTok: ログイン済み（最後に確かめた {_date(info['logged_in_at'])}）")
    if info.get("chrome_port_open") is not None:
        L.append(f"- 取得用の Chrome: {'開いている' if info['chrome_port_open'] else '閉じている（取得のときに UGC Analyzer が開く。正常）'}")
    if info.get("disk_free_gb") is not None:
        L.append(f"- Mac の空き容量: 約{info['disk_free_gb']:.1f}GB（1曲で 0.2〜0.5GB 使う）")
    dup = info.get("duplicate_apps") or []
    if dup:
        L.append("- UGC Analyzer が2つ入っている: " + "・".join(map(str, dup)) + "（古いほうをゴミ箱へ）")
    ep = info.get("edited_prompts") or []
    if ep:
        L.append("- 利用者が直した指示書: " + "・".join(
            f"{e.get('name')}{'（直したあとに初期の指示書が新しくなった）' if e.get('default_changed') else ''}" for e in ep if isinstance(e, dict)))
    probs = info.get("recent_problems") or []
    L.append("- 最近の問題: " + ("なし" if not probs else ""))
    for p in probs:
        L.append(f"  - {p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)}")
    L.append("- 通知: " + (info.get("notifications") or NOTIFY_TEXT))
    return "\n".join(L)
