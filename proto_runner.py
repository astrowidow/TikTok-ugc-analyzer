"""
AI の仕事の列（試作・段1）。

サービスが「型」（仕事の順番・指示書・検査・状態）を持ち、利用者の AI は
「次の仕事を聞く → 指示書どおりにやる → 返す」を繰り返すだけ（docs/BLUEPRINT.md 転換3）。
接続口（mcp_proto.py）の4つの道具 status / next_task / submit / read の中身はここにある。

試作の仕事は13個で固定（docs/IMPLEMENTATION_HANDOVER.md 4-6）:
  1 軸の提案 → 2 界隈の確認（利用者に聞く）→ 3〜9 ラベル付け（30本ずつ）→ 10〜12 コメント分析 → 13 完了

分析フォルダ（output/analyses/<分析ID>/）:
  analysis.json  目録（利用者・楽曲・試作の設定）
  raw/ fetch_log/ derived/  原本・取得の記録・計算物（読むだけ）
  outputs/       AI と人が作ったもの（submit の結果）と、渡した指示書の写し
  eval/          評価（試作ではラベルの一致率）
  state/         仕事の状態（tasks.json）と計測（task_log.jsonl）、提出の原文（submissions/）

どこで止まっても続きから: 仕事の状態はファイルにあり、next_task は常に「まだ済んでいない最初の仕事」を返す。
"""
import datetime
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ANALYSES_DIR = Path(os.environ.get("UGC_ANALYSES_DIR", BASE_DIR / "output" / "analyses"))
PROMPTS_DIR = BASE_DIR / "service_prompts" / "proto"

# 渡したまま返ってこない仕事を「止まっている」とみなすまでの秒数（status の表示に使う）
STALE_AFTER_S = 30 * 60
# read の1ページの大きさ（バイト）。1回の道具の結果を2万トークン程度までに抑える
PAGE_BYTES = 30_000
# ラベル付けの1回の本数
LABEL_BATCH = 30

AXIS_KEY_RE = re.compile(r"^[a-z0-9_]+$")
REACTION_TYPES = ["本家探し", "懐かしさ", "難易度", "比較", "キャラ愛", "ツッコミ", "投稿者ファン", "界隈外からの流入", "その他"]
TIERS_DEFAULT = ["official_artist", "official_brand", "large_creator", "general", "unknown"]

# 繰り返し方。接続の instructions・各道具の説明文・next_task の末尾の3か所に同じものを書く（引き継ぎ書 4-4）
REPEAT_RULE = (
    "利用者が「〇〇の分析を続けて」と言ったら、next_task を呼び、返ってきた指示書どおりに作業して submit する。"
    "kind が done か wait になるまで、利用者に確認せずに繰り返す。"
    "kind が ask_user のときだけ、その内容を利用者に見せて答えを待つ。"
    "submit が差し戻されたら、理由を読んで直して出し直す。"
    "途中経過や入力の中身を会話に書き写さない（利用枠を節約するため）。"
)

_lock = threading.RLock()


class RunnerError(Exception):
    """利用者の AI に返す、日本語の短いエラー"""


# ---------------------------------------------------------------------------
# 小道具
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _strip_fence(text: str) -> str:
    """AI が ``` で囲んで返してきても受け付ける"""
    t = (text or "").strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", t, re.S)
    return m.group(1).strip() if m else t


def _parse_json(text: str):
    t = _strip_fence(text)
    try:
        return json.loads(t), None
    except json.JSONDecodeError as e:
        return None, f"JSON として読めません（{e.msg}、{e.lineno}行目 {e.colno}文字目）。JSON 1つだけを返してください"


def _template(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _fill(template: str, values: dict) -> str:
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


# ---------------------------------------------------------------------------
# 分析フォルダ
# ---------------------------------------------------------------------------
class Analysis:
    def __init__(self, analysis_id: str):
        if not re.match(r"^[A-Za-z0-9_-]+$", analysis_id or ""):
            raise RunnerError("分析 ID の形が正しくありません")
        self.id = analysis_id
        self.dir = ANALYSES_DIR / analysis_id
        self.meta = _read_json(self.dir / "analysis.json")
        if self.meta is None:
            raise RunnerError(f"分析 {analysis_id} が見つかりません")

    # --- 場所 ---
    @property
    def state_path(self) -> Path:
        return self.dir / "state" / "tasks.json"

    @property
    def log_path(self) -> Path:
        return self.dir / "state" / "task_log.jsonl"

    def derived(self, *parts) -> Path:
        return self.dir.joinpath("derived", *parts)

    def outputs(self, *parts) -> Path:
        return self.dir.joinpath("outputs", *parts)

    # --- 目録の中身 ---
    @property
    def owner(self) -> str:
        return self.meta.get("owner", "")

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.id

    @property
    def song_line(self) -> str:
        s = self.meta.get("song", {})
        line = f"{s.get('artist', '')}「{s.get('title', '')}」"
        if s.get("note"):
            line += f"（{s['note']}）"
        return line

    def log(self, **row) -> None:
        row = {"ts": _now(), **row}
        _append_jsonl(self.log_path, row)

    # --- 入力データ ---
    def sheet_index(self) -> dict:
        return _read_json(self.derived("llm_input", "sheets", "index.json"), {}) or {}

    def sheet_names(self) -> list:
        return sorted({v for v in self.sheet_index().values()})

    def entries(self, name: str) -> tuple:
        """taxonomy_sample.md / label_set.md を (冒頭, {seq: 本文}) に分ける"""
        text = self.derived("llm_input", f"{name}.md").read_text(encoding="utf-8")
        parts = re.split(r"(?m)^(?=### seq \d+)", text)
        head = parts[0]
        body = {}
        for p in parts[1:]:
            seq = int(re.match(r"### seq (\d+)", p).group(1))
            body[seq] = p
        return head, body

    def thumb_ref(self, text: str) -> str:
        """「サムネイル: sheets 内の seq N」を、read で取れる名前に書き換える"""
        idx = self.sheet_index()

        def rep(m):
            seq = m.group(1)
            sheet = idx.get(seq)
            if not sheet:
                return "サムネイル: なし"
            return f"サムネイル: {_sheet_read_name(sheet)} の「seq {seq}」の枠"

        return re.sub(r"サムネイル: sheets 内の seq (\d+)", rep, text)

    def comment_text(self, seq: int) -> tuple:
        """コメントファイル（E2E の取得結果）。見出しの E2E のラベルは消して渡す"""
        files = sorted(self.derived("comments").glob(f"{seq}_*.md"))
        if not files:
            raise RunnerError(f"seq {seq} のコメントがありません")
        text = files[0].read_text(encoding="utf-8")
        text = re.sub(r"(?m)^(# seq \d+ \| \d+ \| [0-9-]+) \| 界隈=\S+ 段階=\S+$", r"\1", text)
        video_id = files[0].stem.split("_", 1)[1]
        return text, video_id


def _sheet_read_name(sheet_file: str) -> str:
    m = re.match(r"sheet_(\d+)\.jpg$", sheet_file)
    return f"sheet:{m.group(1)}" if m else sheet_file


def list_analyses(user_id: str) -> list:
    out = []
    if not ANALYSES_DIR.exists():
        return out
    for d in sorted(ANALYSES_DIR.iterdir()):
        meta = _read_json(d / "analysis.json")
        if meta and meta.get("owner") == user_id:
            out.append(Analysis(d.name))
    return out


def _is_active(a: Analysis) -> bool:
    """まだ終わっていない分析か（取得中・AI の仕事が残っている）"""
    acq = a.meta.get("acquisition")
    if acq and acq.get("status") != "done":
        return True
    st = _state(a)
    if st is None:
        return bool(acq)          # 取得は済んだが AI の仕事はまだ始まっていない
    return not _all_done(st)


def _pick(cands: list) -> Analysis:
    """候補が複数なら、進行中のものを優先し、それでも複数なら新しいもの"""
    if len(cands) == 1:
        return cands[0]
    act = [a for a in cands if _is_active(a)]
    return max(act or cands, key=lambda a: a.meta.get("created_at", ""))


def resolve(user_id: str, ref: str | None) -> Analysis:
    """分析 ID・曲名（部分一致）・省略のどれでも引く。複数当たれば進行中で新しいもの"""
    mine = list_analyses(user_id)
    if not mine:
        raise RunnerError("あなたの分析はまだありません")
    if ref:
        ref_n = ref.strip().lower()
        for a in mine:
            if a.id.lower() == ref_n:
                return a
        hits = [a for a in mine if ref_n in a.title.lower() or ref_n in a.song_line.lower()]
        if not hits:
            raise RunnerError(f"「{ref}」に当たる分析がありません。status で一覧を見てください")
        return _pick(hits)
    return _pick(mine)


# ---------------------------------------------------------------------------
# 仕事の列
# ---------------------------------------------------------------------------
def build_tasks(a: Analysis) -> list:
    """試作の13個の仕事を作る（分析フォルダの中身から）"""
    _, label_entries = a.entries("label_set")
    seqs = sorted(label_entries)
    batches = [seqs[i:i + LABEL_BATCH] for i in range(0, len(seqs), LABEL_BATCH)]
    comment_seqs = a.meta.get("proto", {}).get("comment_seqs", [])

    tasks = [
        {"type": "propose_axes", "kind": "ai", "title": "分類軸（界隈など）の案を作る", "params": {}},
        {"type": "confirm_axes", "kind": "ask_user", "title": "界隈の確認（利用者に聞く）", "params": {}},
    ]
    for i, b in enumerate(batches, 1):
        tasks.append({"type": "label", "kind": "ai", "title": f"ラベル付け（{i}/{len(batches)}）",
                      "params": {"batch": i, "n_batches": len(batches), "seqs": b}})
    for i, s in enumerate(comment_seqs, 1):
        tasks.append({"type": "comments", "kind": "ai", "title": f"コメント分析（{i}/{len(comment_seqs)}: seq {s}）",
                      "params": {"seq": s, "i": i, "n": len(comment_seqs)}})
    tasks.append({"type": "done", "kind": "done", "title": "完了", "params": {}})

    for n, t in enumerate(tasks, 1):
        t.update({"n": n, "task_id": f"{a.id}/{n:02d}-{t['type']}", "status": "pending",
                  "first_issued_at": None, "issued_at": None, "issue_count": 0,
                  "done_at": None, "rejects": 0})
    return tasks


def _state(a: Analysis):
    return _read_json(a.state_path)


def _all_done(state: dict) -> bool:
    return all(t["status"] == "done" or t["kind"] == "done" for t in state["tasks"])


def is_w1(a: Analysis) -> bool:
    """試作（analysis.json に proto）以外は W1 の流れ（flow_w1.py）"""
    return not a.meta.get("proto")


def _load_state(a: Analysis) -> dict:
    st = _state(a)
    if st is None:
        if is_w1(a):
            import flow_w1
            st = flow_w1.build_tasks(a)
        else:
            st = {"analysis_id": a.id, "created_at": _now(), "tasks": build_tasks(a)}
        _write_json(a.state_path, st)
    return st


def _run_services(a: Analysis, st: dict):
    """今の仕事がサービスの工程（代表の選定・組み立て・検算・ZIP）なら、ここで片付けて次へ進む"""
    import flow_w1
    t = _current(st)
    while t is not None and t["kind"] == "service":
        t0 = time.time()
        try:
            detail = flow_w1.run_service(a, t, st)
        except RunnerError:
            raise
        except Exception as e:
            a.log(event="service_error", task_id=t["task_id"], error=f"{type(e).__name__}: {e}")
            raise RunnerError(f"サービス側の準備（{t['title']}）で問題が起きました。運営が確認します。"
                              "少し待ってから「続けて」と言ってください") from e
        t["status"] = "done"
        t["done_at"] = _now()
        t["detail"] = detail
        a.log(event="service", task_id=t["task_id"], seconds=round(time.time() - t0, 1), detail=detail)
        _write_json(a.state_path, st)
        t = _current(st)
    return t


def _current(st: dict):
    for t in st["tasks"]:
        if t["status"] != "done":
            return t
    return None


def _progress(st: dict) -> str:
    total = len(st["tasks"])
    done = sum(1 for t in st["tasks"] if t["status"] == "done")
    return f"{done}/{total} 済み"


def reset(analysis_id: str) -> str:
    """仕事の状態を初めに戻す。消さずに state/・outputs/・評価の結果を archive/ へ移す
    （eval/ の比べる相手 reference_labels.tsv は分析フォルダの一部なので残す）"""
    with _lock:
        a = Analysis(analysis_id)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = a.dir / "archive" / stamp
        moved = []
        for rel in ("state", "outputs", "eval/label_agreement.json"):
            src = a.dir / rel
            if src.exists():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest / rel))
                moved.append(rel)
        for name in ("outputs", "eval", "state"):
            (a.dir / name).mkdir(exist_ok=True)
        return f"{a.id}: {', '.join(moved) or '（なし）'} を archive/{stamp}/ に移して初めに戻しました"


# ---------------------------------------------------------------------------
# 前の仕事の結果
# ---------------------------------------------------------------------------
def _taxonomy(a: Analysis, confirmed: bool = True):
    return _read_json(a.outputs("taxonomy.json" if confirmed else "taxonomy_proposal.json"))


def _labels(a: Analysis) -> dict:
    """済んだラベルの束を合わせて {seq: 行の辞書}"""
    out = {}
    d = a.outputs("labels")
    if not d.exists():
        return out
    for f in sorted(d.glob("batch_*.tsv")):
        rows = f.read_text(encoding="utf-8").splitlines()
        head = rows[0].split("\t")
        for r in rows[1:]:
            cols = r.split("\t")
            row = dict(zip(head, cols))
            out[int(row["seq"])] = row
    return out


# ---------------------------------------------------------------------------
# 指示書を作る（渡す時点で前の結果を読んで差し込む）
# ---------------------------------------------------------------------------
def _catalog_line(name: str, desc: str) -> str:
    return f"- `{name}` … {desc}"


def _render(a: Analysis, t: dict, st: dict | None = None) -> dict:
    """仕事1つを、指示書（Markdown）と読む資料の目録にする"""
    if is_w1(a):
        import flow_w1
        return flow_w1.render(a, t, st or _load_state(a))
    typ = t["type"]
    s = a.meta.get("song", {})
    base = {"song": a.song_line, "song_short": f"{s.get('artist', '')}「{s.get('title', '')}」",
            "n_videos": a.meta.get("n_videos", "?"), "period": a.meta.get("period", "")}
    catalog = []

    if typ == "propose_axes":
        head, body = a.entries("taxonomy_sample")
        pages = _pages_of(a, "taxonomy_sample")
        sheets = [_sheet_read_name(s) for s in a.sheet_names()]
        catalog.append(_catalog_line("taxonomy_sample", f"分類軸を考えるためのサンプル {len(body)} 本（page=1〜{len(pages)} の {len(pages)} ページ。全部読む）"))
        catalog.append(_catalog_line(f"{sheets[0]}〜{sheets[-1]}", f"サムネイルの一覧画像 {len(sheets)} 枚（全部見る）"))
        text = _fill(_template("axes.md"), {**base, "n_sample": len(body), "n_pages": len(pages),
                                            "n_sheets": len(sheets), "first_sheet": sheets[0], "last_sheet": sheets[-1]})

    elif typ == "confirm_axes":
        prop = _taxonomy(a, confirmed=False)
        if prop is None:
            raise RunnerError("軸の案がまだありません（前の仕事が済んでいない）")
        text = _fill(_template("confirm.md"), {**base, "present": _present_axes(prop),
                                               "proposal_json": json.dumps(prop, ensure_ascii=False, indent=1)})

    elif typ == "label":
        tax = _taxonomy(a)
        if tax is None:
            raise RunnerError("確定した分類軸がまだありません（界隈の確認が済んでいない）")
        _, body = a.entries("label_set")
        seqs = t["params"]["seqs"]
        entries = "".join(a.thumb_ref(body[s]) for s in seqs).rstrip() + "\n"
        idx = a.sheet_index()
        sheets = sorted({_sheet_read_name(idx[str(s)]) for s in seqs if str(s) in idx})
        if sheets:
            catalog.append(_catalog_line(", ".join(sheets), "この回の動画のサムネイルが載っているシート"))
        text = _fill(_template("label.md"), {**base, "batch": t["params"]["batch"], "n_batches": t["params"]["n_batches"],
                                             "n": len(seqs), "taxonomy_json": json.dumps(tax, ensure_ascii=False, indent=1),
                                             "entries": entries, "seq_list": ", ".join(map(str, seqs))})

    elif typ == "comments":
        tax = _taxonomy(a)
        if tax is None:
            raise RunnerError("確定した分類軸がまだありません")
        seq = t["params"]["seq"]
        comments, video_id = a.comment_text(seq)
        lab = _labels(a).get(seq)
        label_line = (f"community={lab['community']} / format={lab['format']} / motive={lab['motive']} / "
                      f"region={lab['region']} / tier={lab['tier']}（conf={lab['conf']}。根拠: {lab['reason']}）"
                      if lab else "（この動画にはまだラベルがありません。community は下の界隈から選ぶ）")
        community = "\n".join(f"- `{k}`: {_first_sentence(v)}" for k, v in tax["community"].items() if not k.startswith("_"))
        catalog.append(_catalog_line("glossary", "反応の読み方の語彙（採用文脈・動機、指標の読み方）。任意。一度読めば次の動画では読まなくてよい"))
        text = _fill(_template("comments.md"), {**base, "i": t["params"]["i"], "n": t["params"]["n"], "seq": seq,
                                                "video_id": video_id, "label_line": label_line,
                                                "community_list": community, "comments": comments.rstrip(),
                                                "reaction_types": "|".join(REACTION_TYPES)})

    elif typ == "done":
        text = _fill(_template("done.md"), {**base, "summary": _done_summary(a)})

    else:
        raise RunnerError(f"知らない仕事の種類です: {typ}")

    return {"text": text, "catalog": catalog}


def _first_sentence(s: str, limit: int = 90) -> str:
    s = str(s).strip()
    cut = s.split("。")[0]
    if len(cut) > limit:
        cut = cut[:limit] + "…"
    return cut + ("。" if not cut.endswith("…") else "")


def _present_axes(tax: dict) -> str:
    """利用者に見せる界隈の案（短く）"""
    marks = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    lines = []
    comm = [(k, v) for k, v in tax["community"].items() if not k.startswith("_")]
    for i, (k, v) in enumerate(comm):
        mark = marks[i] if i < len(marks) else f"({i + 1})"
        lines.append(f"{mark} {_first_sentence(v)}（`{k}`）")
    fmt = ", ".join(k for k in tax.get("format", {}) if not k.startswith("_"))
    mot = ", ".join(k for k in tax.get("motive", {}) if not k.startswith("_"))
    lines.append("")
    lines.append(f"（参考）投稿の型: {fmt}")
    lines.append(f"（参考）この曲を使った理由の型: {mot}")
    return "\n".join(lines)


def _done_summary(a: Analysis) -> str:
    lines = []
    tax = _taxonomy(a)
    if tax:
        lines.append(f"- 界隈 {sum(1 for k in tax['community'] if not k.startswith('_'))} 個で確定（`outputs/taxonomy.json`）")
    labs = _labels(a)
    if labs:
        lines.append(f"- ラベル {len(labs)} 本（`outputs/labels.tsv`）")
    va = a.outputs("video_analysis.jsonl")
    if va.exists():
        lines.append(f"- コメント分析 {len(va.read_text(encoding='utf-8').splitlines())} 本（`outputs/video_analysis.jsonl`）")
    return "\n".join(lines) or "- （成果物なし）"


# ---------------------------------------------------------------------------
# 検査（だめなら理由の一覧を返して差し戻す）
# ---------------------------------------------------------------------------
def check_taxonomy(tax, need_region: bool = True) -> list:
    errs = []
    if not isinstance(tax, dict):
        return ["分類軸は JSON のオブジェクト（{...}）で返してください"]
    for axis in ("community", "format", "motive"):
        v = tax.get(axis)
        if not isinstance(v, dict):
            errs.append(f"`{axis}` が無いか、{{key: 定義}} の形になっていません")
            continue
        keys = [k for k in v if not k.startswith("_")]
        if len(keys) < 3:
            errs.append(f"`{axis}` の区分が {len(keys)} 個です。3個以上にしてください")
        if "unknown" not in v:
            errs.append(f"`{axis}` に `unknown` を入れてください")
        bad = [k for k in keys if not AXIS_KEY_RE.match(k)]
        if bad:
            errs.append(f"`{axis}` の key は半角英小文字・数字・_ だけにしてください: {bad[:5]}")
        empty = [k for k in keys if k != "unknown" and (not isinstance(v[k], str) or len(v[k].strip()) < 5)]
        if empty:
            errs.append(f"`{axis}` の定義が空か短すぎます（見分けるシグナルと例を書く）: {empty[:5]}")
    comm = tax.get("community")
    if isinstance(comm, dict) and len([k for k in comm if not k.startswith("_")]) > 20:
        errs.append("`community` が多すぎます（8〜14 程度）")
    for axis in (("region", "tier") if need_region else ("tier",)):
        v = tax.get(axis)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or "unknown" not in v:
            errs.append(f"`{axis}` は文字列の配列で、`unknown` を含めてください")
    return errs


def _axis_keys(tax: dict, axis: str) -> set:
    v = tax.get(axis, {})
    if isinstance(v, dict):
        return {k for k in v if not k.startswith("_")}
    return set(v)


def check_labels(tsv: str, seqs: list, tax: dict, with_region: bool = True) -> tuple:
    """TSV を検査して (理由の一覧, 行の一覧)"""
    errs = []
    rows_out = []
    lines = [ln for ln in _strip_fence(tsv).splitlines() if ln.strip()]
    want_head = ["seq", "community", "format", "motive", "region", "tier", "conf", "reason"]
    if not with_region:   # W1: 地域はサービスが投稿地域のデータで付ける（F4）
        want_head.remove("region")
    if not lines:
        return ["TSV が空です"], []
    head = [h.strip() for h in lines[0].split("\t")]
    if head != want_head:
        return ["1行目は次のヘッダにしてください（タブ区切り）: " + "\t".join(want_head)
                + f"。受け取ったヘッダ: {lines[0][:120]}"], []
    allowed = {ax: _axis_keys(tax, ax) for ax in ("community", "format", "motive", "tier")}
    seen = {}
    for i, ln in enumerate(lines[1:], 2):
        cols = ln.split("\t")
        if len(cols) != len(want_head):
            errs.append(f"{i}行目: 列が {len(cols)} 個です（{len(want_head)}列。reason の中にタブを入れない）")
            continue
        row = dict(zip(want_head, [c.strip() for c in cols]))
        try:
            seq = int(row["seq"])
        except ValueError:
            errs.append(f"{i}行目: seq が数字ではありません: {row['seq']}")
            continue
        if seq in seen:
            errs.append(f"seq {seq} が2回あります")
        seen[seq] = row
        for ax, keys in allowed.items():
            if row[ax] not in keys:
                errs.append(f"seq {seq}: {ax}=`{row[ax]}` は確定した分類軸にありません")
        if with_region and not (row["region"] == "unknown" or re.match(r"^[A-Z]{2}$", row["region"])):
            errs.append(f"seq {seq}: region は2文字の地域コードか unknown にしてください: {row['region']}")
        if row["conf"] not in ("H", "M", "L"):
            errs.append(f"seq {seq}: conf は H / M / L のどれか: {row['conf']}")
        if len(row["reason"]) < 4:
            errs.append(f"seq {seq}: reason（根拠）を1行で書いてください")
        rows_out.append(row)
    missing = [s for s in seqs if s not in seen]
    extra = [s for s in seen if s not in set(seqs)]
    if missing:
        errs.append(f"足りない seq: {missing}")
    if extra:
        errs.append(f"この回の対象でない seq: {extra}")
    return errs[:30], rows_out


def _cids(comments_text: str) -> set:
    return set(re.findall(r"\[(\d{8,})\]", comments_text))


def check_video_analysis(obj, seq: int, video_id: str, comments_text: str, tax: dict) -> list:
    errs = []
    if not isinstance(obj, dict):
        return ["JSON のオブジェクト（{...}）1つで返してください"]
    need = ["seq", "video_id", "community", "n_comments", "n_replies", "why_this_song", "reaction_types",
            "quotes", "creator_engagement", "reply_threads", "languages", "top20_vs_rest", "notable"]
    missing = [k for k in need if k not in obj]
    if missing:
        errs.append(f"欄が足りません: {missing}（値が無いときは null）")
    if str(obj.get("seq")) != str(seq):
        errs.append(f"seq は {seq} にしてください")
    if str(obj.get("video_id")) != str(video_id):
        errs.append(f"video_id は \"{video_id}\" にしてください（文字列）")
    if obj.get("community") not in _axis_keys(tax, "community"):
        errs.append(f"community=`{obj.get('community')}` は確定した界隈にありません")
    cids = _cids(comments_text)
    quotes = obj.get("quotes")
    if not isinstance(quotes, list) or not (2 <= len(quotes) <= 5):
        errs.append("quotes は 2〜5 件の配列にしてください")
    else:
        for q in quotes:
            if not isinstance(q, dict):
                errs.append("quotes の各要素は {cid, text, likes, why} にしてください")
                continue
            if str(q.get("cid")) not in cids:
                errs.append(f"引用の cid {q.get('cid')} が入力のコメントにありません")
            if not q.get("why"):
                errs.append(f"引用 {q.get('cid')} の why（何を示す引用か）を書いてください")
    rts = obj.get("reaction_types")
    if not isinstance(rts, list) or not rts:
        errs.append("reaction_types を1件以上の配列にしてください")
    else:
        for r in rts:
            if not isinstance(r, dict):
                errs.append("reaction_types の各要素は {type, share, evidence_cids} にしてください")
                continue
            if r.get("type") not in REACTION_TYPES:
                errs.append(f"reaction_types の type は次から: {'|'.join(REACTION_TYPES)}（受け取った: {r.get('type')}）")
            if r.get("share") not in ("多", "中", "少"):
                errs.append(f"reaction_types の share は 多 / 中 / 少: {r.get('share')}")
            ev = r.get("evidence_cids") or []
            bad = [c for c in ev if str(c) not in cids]
            if bad:
                errs.append(f"evidence_cids に入力に無い cid があります: {bad[:5]}")
    if not obj.get("why_this_song"):
        errs.append("why_this_song を書いてください")
    if not obj.get("top20_vs_rest"):
        errs.append("top20_vs_rest（上位20件だけで同じ結論になったか）を必ず書いてください")
    if not isinstance(obj.get("languages"), dict):
        errs.append("languages は {\"ja\": 0.8, ...} の形にしてください")
    return errs[:30]


# ---------------------------------------------------------------------------
# 道具の中身
# ---------------------------------------------------------------------------
def _hm(sec: float) -> str:
    sec = max(0, int(sec))
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}時間{m}分" if h else f"{max(m, 1)}分"


def _acq_view(a: Analysis):
    """取得の段の様子。取得が無い（試作）か済んでいれば None"""
    acq = a.meta.get("acquisition")
    if not acq or acq.get("status") == "done":
        return None
    if acq.get("status") == "failed":
        return {"state": "failed", "message": f"取得が止まりました（{acq.get('error')}）。運営が確認します。", "eta_seconds": None}
    try:
        from acquire import launch
        p = launch.progress(a.id)
    except Exception as e:  # 見込みが出せなくても状態は返す
        p = {"status": acq.get("status"), "eta_seconds": None, "error": str(e)}
    eta = p.get("eta_seconds")
    if p.get("status") == "queued" and p.get("ahead"):
        msg = f"取得の順番待ち（前に{p['ahead']}件）。終わるまで約{_hm(eta)}。"
    elif p.get("status") == "running":
        msg = f"取得中（{p.get('step_label') or '準備'}）。終わるまで約{_hm(eta)}。"
    else:
        msg = f"取得の開始待ち。終わるまで約{_hm(eta)}。" if eta else "取得の開始待ち。"
    return {"state": "acquiring", "message": msg + "終わったらメールで知らせます。", "eta_seconds": eta}


def status(user_id: str, ref: str | None = None) -> dict:
    with _lock:
        targets = [resolve(user_id, ref)] if ref else list_analyses(user_id)
        items = []
        for a in targets:
            av = _acq_view(a)
            if av:
                items.append({"analysis_id": a.id, "title": a.title, "song": a.song_line, "state": av["state"],
                              "progress": "取得の段", "message": av["message"], "eta_seconds": av["eta_seconds"]})
                continue
            st = _load_state(a)
            cur = _current(st)
            if cur is None or cur["kind"] == "done":
                state_label, msg = "done", "完了しています。"
                for t in st["tasks"]:
                    if t["kind"] == "done" and t["status"] != "done":
                        msg = "AI の仕事は全部済みました。next_task で完了の知らせを受け取れます。"
            elif cur["kind"] == "ask_user":
                state_label, msg = "ask_user", f"利用者の確認待ち（{cur['title']}）。「続けて」で再開できます。"
            else:
                state_label = "ai"
                msg = f"AI の番（次は「{cur['title']}」）。「続けて」で再開できます。"
                if cur["status"] == "issued" and cur["issued_at"]:
                    age = time.time() - datetime.datetime.fromisoformat(cur["issued_at"]).timestamp()
                    if age > STALE_AFTER_S:
                        msg = (f"「{cur['title']}」を渡したまま {int(age // 60)} 分止まっています。"
                               "「続けて」と言えば続きから再開します。")
            items.append({"analysis_id": a.id, "title": a.title, "song": a.song_line, "state": state_label,
                          "progress": _progress(st), "message": msg})
        if not items:
            return {"text": "あなたの分析はまだありません。", "analyses": []}
        text = "\n".join(f"- {i['title']}（{i['analysis_id']}）: {i['message']} 進み具合 {i['progress']}" for i in items)
        return {"text": text, "analyses": items}


MUSIC_URL_RE = re.compile(r"^https://(www\.)?tiktok\.com/music/[^\s/]+-\d+")
MAX_ACTIVE_PER_USER = 2


def start_analysis(user_id: str, song: str, artist: str = "", music_url: str = "") -> dict:
    """分析を作って取得の待ち行列に入れる（取得は Web サービスの外の係が走らせる）"""
    song, artist, music_url = (song or "").strip(), (artist or "").strip(), (music_url or "").strip()
    if not song and not music_url:
        raise RunnerError("曲名（とアーティスト名）か、TikTok の楽曲ページの URL を教えてください")
    if music_url and not MUSIC_URL_RE.match(music_url):
        raise RunnerError("楽曲ページの URL は https://www.tiktok.com/music/曲名-数字 の形です")
    from acquire import launch
    with _lock:
        aid = launch.find_active(user_id, song, music_url)
        created = aid is None
        if created:
            active = [a for a in list_analyses(user_id)
                      if (a.meta.get("acquisition") or {}).get("status") in ("queued", "running")]
            if len(active) >= MAX_ACTIVE_PER_USER:
                raise RunnerError(f"取得中・順番待ちの分析が{len(active)}件あります。終わってから次を頼んでください: "
                                  + ", ".join(a.title for a in active))
            aid = launch.new_analysis(user_id, song, artist, music_url)
    kick = launch.ensure_worker()
    p = launch.progress(aid)
    title = song or music_url
    eta = p.get("eta_seconds")
    lines = [f"「{title}」の{'取得を受け付けました' if created else '取得はもう受け付けています'}（分析 ID: {aid}）。",
             "楽曲ページの動画一覧・属性・サムネを取り、コメントを取る動画を数字で決めてコメントを取ります。",
             (f"前に{p['ahead']}件あります。" if p.get("ahead") else "") + (f"終わるまで約{_hm(eta)}の見込みです。" if eta else ""),
             "終わったらメールでお知らせします。そのあと「" + title + "の分析を続けて」と言ってください。",
             "", "（AI へ: この内容を利用者に短く伝えて、ここで止まる。取得を待たない・見に来ない）"]
    return {"text": "\n".join(l for l in lines if l is not None), "analysis_id": aid, "created": created,
            "worker": kick, "eta_seconds": eta}


def _wait_task(a: Analysis, message: str) -> dict:
    text = (f"# 待ち: {a.title}\n\n- kind: `wait`\n\n{message}\n\n"
            "ここで止まって、利用者にこの内容を短く伝える。next_task を繰り返し呼ばない（待つ間に見に来ない）。\n\n---\n" + REPEAT_RULE)
    a.log(event="issue", task_id=f"{a.id}/wait", kind="wait", chars=len(text))
    return {"text": text, "task_id": None, "kind": "wait", "analysis_id": a.id, "progress": "取得の段"}


def next_task(user_id: str, ref: str | None = None) -> dict:
    with _lock:
        a = resolve(user_id, ref)
        av = _acq_view(a)
        if av:
            return _wait_task(a, av["message"] + "\n取得が終わったら、利用者が「" + a.title + "の分析を続けて」と言えば再開する。")
        st = _load_state(a)
        t = _run_services(a, st) if is_w1(a) else _current(st)
        if t is None:
            t = st["tasks"][-1]
        rendered = _render(a, t, st)
        now = _now()
        if t["kind"] != "done":
            if t["first_issued_at"] is None:
                t["first_issued_at"] = now
            reissue = t["status"] == "issued"
            t["status"] = "issued"
            t["issued_at"] = now
            t["issue_count"] += 1
        else:
            reissue = False
            if t["status"] != "done":
                t["status"] = "done"
                t["done_at"] = now
                t["first_issued_at"] = now
                if not is_w1(a):
                    _finish(a)
        _write_json(a.state_path, st)

        body = rendered["text"]
        header = [
            f"# 仕事 {st['tasks'].index(t) + 1}/{len(st['tasks'])}: {t['title']}",
            "",
            f"- task_id: `{t['task_id']}`",
            f"- kind: `{t['kind']}`",
            f"- 進み具合: {_progress(st)}",
        ]
        if reissue:
            header.append("- （前に渡して、まだ提出されていない仕事です。最初からやり直してください）")
        parts = ["\n".join(header), body]
        if rendered["catalog"]:
            parts.append("## 読む資料（read で取る。analysis_id=`" + a.id + "`）\n" + "\n".join(rendered["catalog"]))
        if t["kind"] == "ai":
            parts.append(f"## 提出\n`submit(task_id=\"{t['task_id']}\", output=<上の「出力の形」のテキスト>)`")
        parts.append("---\n" + REPEAT_RULE)
        text = "\n\n".join(parts)

        a.log(event="issue", task_id=t["task_id"], kind=t["kind"], chars=len(text), reissue=reissue)
        # 渡した指示書はそのまま成果物フォルダに写して残す（どの版で作ったかの記録）
        _write_text(a.outputs("prompts", f"{t['n']:02d}-{t['type']}.md"), text)
        return {"text": text, "task_id": t["task_id"], "kind": t["kind"], "analysis_id": a.id,
                "progress": _progress(st)}


def submit(user_id: str, task_id: str, output) -> dict:
    with _lock:
        aid = (task_id or "").split("/", 1)[0]
        a = resolve(user_id, aid) if aid else None
        if a is None or a.id != aid:
            raise RunnerError(f"task_id {task_id} に当たる分析がありません")
        st = _load_state(a)
        t = next((x for x in st["tasks"] if x["task_id"] == task_id), None)
        if t is None:
            raise RunnerError(f"task_id {task_id} はありません。next_task を呼んでください")
        if t["status"] == "done":
            return {"ok": True, "text": "受領済みの仕事です（何もしませんでした）。next_task を呼んで次へ進んでください。",
                    "progress": _progress(st)}
        cur = _current(st)
        if cur is None or cur["task_id"] != task_id:
            raise RunnerError(f"{task_id} は今の仕事ではありません。next_task を呼んでください")
        if t["kind"] == "done":
            raise RunnerError("完了の知らせには提出は要りません")

        raw = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        n_sub = t["rejects"] + 1
        _write_text(a.dir / "state" / "submissions" / f"{t['n']:02d}-{t['type']}_{n_sub}.txt", raw)

        if is_w1(a):
            import flow_w1
            errs = flow_w1.accept(a, t, raw, st)
        else:
            errs = _accept(a, t, raw)
        now = _now()
        if errs:
            t["rejects"] += 1
            _write_json(a.state_path, st)
            a.log(event="reject", task_id=task_id, chars_in=len(raw), rejects=t["rejects"], reasons=errs[:10])
            return {"ok": False, "reasons": errs,
                    "text": "差し戻しです。次の理由を直して、同じ task_id で submit し直してください。\n"
                            + "\n".join(f"- {e}" for e in errs),
                    "progress": _progress(st)}

        t["status"] = "done"
        t["done_at"] = now
        _write_json(a.state_path, st)
        elapsed = None
        if t["first_issued_at"]:
            elapsed = int(datetime.datetime.fromisoformat(now).timestamp()
                          - datetime.datetime.fromisoformat(t["first_issued_at"]).timestamp())
        a.log(event="done", task_id=task_id, chars_in=len(raw), rejects=t["rejects"], elapsed_s=elapsed,
              issue_count=t["issue_count"])
        return {"ok": True, "progress": _progress(st),
                "text": f"受け取りました（{_progress(st)}）。続けて next_task を呼んでください。"}


def _accept(a: Analysis, t: dict, raw: str) -> list:
    """検査して、良ければ outputs/ に取り込む。だめなら理由の一覧"""
    typ = t["type"]
    if typ == "propose_axes":
        tax, err = _parse_json(raw)
        if err:
            return [err]
        errs = check_taxonomy(tax)
        if not errs:
            _write_json(a.outputs("taxonomy_proposal.json"), tax)
        return errs

    if typ == "confirm_axes":
        obj, err = _parse_json(raw)
        if err:
            return [err + "（{\"user_answer\": \"利用者の言葉\", \"taxonomy\": {...}} の形）"]
        if not isinstance(obj, dict) or not str(obj.get("user_answer", "")).strip():
            return ["user_answer（利用者の答えをそのまま）を入れてください"]
        prop = _taxonomy(a, confirmed=False)
        tax = obj.get("taxonomy") or prop
        errs = check_taxonomy(tax)
        if errs:
            return errs
        modified = json.dumps(tax, sort_keys=True, ensure_ascii=False) != json.dumps(prop, sort_keys=True, ensure_ascii=False)
        _write_json(a.outputs("taxonomy.json"), tax)
        _write_json(a.outputs("confirm_answer.json"),
                    {"user_answer": obj["user_answer"], "modified": modified, "at": _now()})
        return []

    if typ == "label":
        tax = _taxonomy(a)
        errs, rows = check_labels(raw, t["params"]["seqs"], tax)
        if errs:
            return errs
        head = ["seq", "community", "format", "motive", "region", "tier", "conf", "reason"]
        order = {s: i for i, s in enumerate(t["params"]["seqs"])}
        rows.sort(key=lambda r: order[int(r["seq"])])
        text = "\t".join(head) + "\n" + "".join("\t".join(r[h] for h in head) + "\n" for r in rows)
        _write_text(a.outputs("labels", f"batch_{t['params']['batch']:02d}.tsv"), text)
        _merge_labels(a)
        return []

    if typ == "comments":
        tax = _taxonomy(a)
        seq = t["params"]["seq"]
        comments, video_id = a.comment_text(seq)
        obj, err = _parse_json(raw)
        if err:
            return [err]
        errs = check_video_analysis(obj, seq, video_id, comments, tax)
        if errs:
            return errs
        _write_json(a.outputs("video_analysis", f"{seq}.json"), obj)
        _merge_video_analysis(a)
        return []

    return [f"この仕事（{typ}）には提出は要りません"]


def _merge_labels(a: Analysis) -> None:
    labs = _labels(a)
    head = ["seq", "community", "format", "motive", "region", "tier", "conf", "reason"]
    text = "\t".join(head) + "\n" + "".join("\t".join(labs[s][h] for h in head) + "\n" for s in sorted(labs))
    _write_text(a.outputs("labels.tsv"), text)


def _merge_video_analysis(a: Analysis) -> None:
    rows = []
    for f in sorted(a.outputs("video_analysis").glob("*.json"), key=lambda p: int(p.stem)):
        rows.append(json.dumps(_read_json(f), ensure_ascii=False))
    _write_text(a.outputs("video_analysis.jsonl"), "\n".join(rows) + "\n")


def _finish(a: Analysis) -> None:
    """完了のとき: 評価（利用者が軸を直さなかった場合に限り、界隈の一致率を E2E と比べる）"""
    try:
        ans = _read_json(a.outputs("confirm_answer.json"), {}) or {}
        ref_path = a.dir / "eval" / "reference_labels.tsv"
        labs = _labels(a)
        result = {"at": _now(), "axes_modified": ans.get("modified"), "n_labels": len(labs)}
        if ref_path.exists() and labs:
            result.update(community_agreement(labs, ref_path))
        _write_json(a.dir / "eval" / "label_agreement.json", result)
    except Exception as e:  # 評価の失敗で完了を止めない
        _write_json(a.dir / "eval" / "label_agreement.json", {"error": str(e)})


def community_agreement(labs: dict, ref_path: Path) -> dict:
    """界隈の一致率。key の名前も箱の切り方も AI ごとに違うので、2通りに数える。
    - agreement_mapped: 試作の各界隈を E2E で最も重なる界隈に1つずつ対応させる（粗い。E2E の2つを1つにまとめた箱では半分が不一致に見える）
    - agreement_judgment: 両方向の対応を使う。試作→E2E か E2E→試作のどちらかの対応で合えば一致
      （箱をまとめた・分けただけの違いは一致に数え、同じ区分があるのに別の箱に入れたものだけを「判断の食い違い」に数える）"""
    ref = {}
    rows = ref_path.read_text(encoding="utf-8").splitlines()
    head = rows[0].split("\t")
    for r in rows[1:]:
        d = dict(zip(head, r.split("\t")))
        ref[int(d["seq"])] = d["community"]
    common = [s for s in labs if s in ref]
    pairs = {}
    for s in common:
        k = (labs[s]["community"], ref[s])
        pairs[k] = pairs.get(k, 0) + 1
    mine_to_ref, ref_to_mine = {}, {}
    for (mine, theirs), n in sorted(pairs.items(), key=lambda x: -x[1]):
        mine_to_ref.setdefault(mine, theirs)
        ref_to_mine.setdefault(theirs, mine)
    hit = sum(1 for s in common if mine_to_ref.get(labs[s]["community"]) == ref[s])
    judged = [s for s in common
              if mine_to_ref.get(labs[s]["community"]) == ref[s] or ref_to_mine.get(ref[s]) == labs[s]["community"]]
    exact = sum(1 for s in common if labs[s]["community"] == ref[s])
    n = len(common)
    return {"n_common": n,
            "agreement_judgment": round(len(judged) / n, 3) if n else None,
            "agreement_mapped": round(hit / n, 3) if n else None,
            "agreement_same_key": round(exact / n, 3) if n else None,
            "disagree_seqs": sorted(set(common) - set(judged)),
            "mapping": mine_to_ref, "mapping_ref_to_mine": ref_to_mine}


# ---------------------------------------------------------------------------
# read（仕事に要る資料）
# ---------------------------------------------------------------------------
def _paginate(units: list) -> list:
    """単位（動画・見出し）の区切りでページにする。1つの単位がページより大きければ行で割る"""
    out = []
    for u in units:
        if len(u.encode("utf-8")) <= PAGE_BYTES:
            out.append(u)
            continue
        cur = ""
        for line in u.splitlines(keepends=True):
            if cur and len((cur + line).encode("utf-8")) > PAGE_BYTES:
                out.append(cur)
                cur = ""
            cur += line
        if cur:
            out.append(cur)
    pages, cur = [], ""
    for u in out:
        if cur and len((cur + u).encode("utf-8")) > PAGE_BYTES:
            pages.append(cur)
            cur = ""
        cur += u
    if cur:
        pages.append(cur)
    return pages


def _pages_of(a: Analysis, name: str) -> list:
    """長い Markdown を、動画の区切り（### seq）でページに分ける"""
    if is_w1(a):
        import flow_w1
        units = flow_w1.units_of(a, name, None)
        if units is not None:
            return _paginate(units)
    if name in ("taxonomy_sample", "label_set"):
        head, body = a.entries(name)
        units = [head] + [a.thumb_ref(body[s]) for s in sorted(body)]
    elif name == "glossary":
        units = re.split(r"(?m)^(?=### )", a.derived("knowledge", "glossary_FH.md").read_text(encoding="utf-8"))
    elif name == "weekly":
        units = [a.derived("weekly.tsv").read_text(encoding="utf-8")]
    elif name.startswith("comments:"):
        seq = int(name.split(":", 1)[1])
        text, _ = a.comment_text(seq)
        units = re.split(r"(?m)^(?=- \[)", text)
    elif name in ("taxonomy", "taxonomy_proposal"):
        tax = _taxonomy(a, confirmed=(name == "taxonomy"))
        if tax is None:
            raise RunnerError(f"{name} はまだありません")
        units = [json.dumps(tax, ensure_ascii=False, indent=1)]
    else:
        raise RunnerError(f"知らない資料の名前です: {name}")
    return _paginate(units)


def read(user_id: str, ref: str | None, name: str, page: int = 1) -> dict:
    """資料を1ページ返す。画像は {"image": bytes, "format": "jpeg"}"""
    with _lock:
        a = resolve(user_id, ref)
        name = (name or "").strip()
        m = re.match(r"^xsheet[:_]?(\d+)(\.jpg)?$", name)
        if m:   # W1: ラベル対象のうち取得の段のシートに無い動画のサムネ一覧
            p = a.derived("ai", "sheets", f"xsheet_{int(m.group(1)):02d}.jpg")
            if not p.exists():
                raise RunnerError(f"{name} はありません")
            data = p.read_bytes()
            a.log(event="read", name=name, bytes=len(data))
            return {"image": data, "format": "jpeg", "text": f"{name}（サムネイルの一覧。各枠の上に seq）"}
        m = re.match(r"^sheet[:_]?(\d+)(\.jpg)?$", name)
        if m:
            fname = f"sheet_{int(m.group(1)):02d}.jpg"
            if fname not in a.sheet_names():
                raise RunnerError(f"{name} はありません（{', '.join(_sheet_read_name(s) for s in a.sheet_names())}）")
            data = a.derived("llm_input", "sheets", fname).read_bytes()
            a.log(event="read", name=name, bytes=len(data))
            return {"image": data, "format": "jpeg",
                    "text": f"{_sheet_read_name(fname)}（サムネイルの一覧。各枠の上に seq）"}
        pages = _pages_of(a, name)
        page = int(page or 1)
        if not 1 <= page <= len(pages):
            raise RunnerError(f"{name} は {len(pages)} ページです（page=1〜{len(pages)}）")
        text = pages[page - 1]
        a.log(event="read", name=name, page=page, chars=len(text))
        more = f"（続きは page={page + 1}）" if page < len(pages) else "（これが最後のページ）"
        return {"text": f"<!-- {name} page {page}/{len(pages)} {more} -->\n{text}", "page": page, "pages": len(pages)}


# ---------------------------------------------------------------------------
# 完成後の直し（H5）と、指示の設定
# ---------------------------------------------------------------------------
def revise(user_id: str, ref: str | None, instruction: str) -> dict:
    """「このレポートだけ」の直し。該当の章を書き直す仕事を足し、AI はそのまま next_task で片付ける"""
    instruction = (instruction or "").strip()
    if not instruction:
        raise RunnerError("直したい内容を教えてください")
    if len(instruction) > 2000:
        raise RunnerError("直しの指示は2000字までにしてください")
    with _lock:
        a = resolve(user_id, ref)
        if not is_w1(a):
            raise RunnerError("この分析（試作）は直しに対応していません")
        st = _load_state(a)
        if _current(st) is not None:
            raise RunnerError("レポートがまだできていません。完成してから直しを頼んでください")
        import flow_w1
        new = [flow_w1._task(st, "revise", "ai", "レポートを直す（利用者の指示）", {"instruction": instruction}),
               flow_w1._task(st, "finish", "ai", "note 用に仕上げ直す", {"chapter": None, "i": 1, "n": 1}),
               flow_w1._task(st, "assemble", "service", "レポートの組み立て（サービス）"),
               flow_w1._task(st, "verify", "service", "検算（サービス）"),
               flow_w1._task(st, "done", "done", "完了（直し）")]
        st["tasks"] += new
        _write_json(a.state_path, st)
        a.log(event="revise_requested", chars=len(instruction))
        return {"text": f"「{a.title}」の直しを受け付けました。続けて next_task を呼んで、直しの仕事を片付けてください。",
                "analysis_id": a.id}


def settings(user_id: str, action: str = "get", item: str | None = None, value: str | None = None,
             use_style_guide: bool | None = None) -> dict:
    """「今後ずっと」の指示の設定（利用者ごと）。見る・変える・デフォルトに戻す"""
    import user_settings as us
    try:
        if action == "set":
            if not item:
                raise RunnerError("変える項目（style / focus / community_policy / comment_lens）を指定してください")
            us.set_item(user_id, item, value, use_style_guide)
            head = f"{us.ITEMS[item]}を変えました。次の分析（とこれから渡す仕事）から使います。"
        elif action == "reset":
            us.reset(user_id, item)
            head = (f"{us.ITEMS[item]}を" if item else "設定を全部") + "デフォルトに戻しました。"
        else:
            head = "今の設定です。"
    except us.SettingsError as e:
        raise RunnerError(str(e)) from e
    return {"text": head + "\n" + us.describe(user_id), "current": us.get(user_id)}


# ---------------------------------------------------------------------------
# 運営用: python proto_runner.py reset <分析ID> / show <分析ID>
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "reset":
        print(reset(sys.argv[2]))
    elif len(sys.argv) >= 3 and sys.argv[1] == "show":
        a = Analysis(sys.argv[2])
        st = _load_state(a)
        for t in st["tasks"]:
            print(f"{t['n']:>2} {t['status']:<8} rejects={t['rejects']} issues={t['issue_count']} {t['title']}")
    else:
        print("使い方: python proto_runner.py reset <分析ID> | show <分析ID>")
