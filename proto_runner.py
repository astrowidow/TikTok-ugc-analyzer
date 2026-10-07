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

# 取得アプリ（各自の Mac で全部を回す形。docs/ALL_IN_APP_PLAN.md）が起動時に差し込むもの。Windows 機のサービスでは None のまま。
# 持つもの: acquisition_settings() → 取得の設定 / ensure_app() → 取得アプリを起こす / app_state() → {"running", "login_wanted"}
LOCAL = None


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
    # フォルダだけを見る。Finder が置く .DS_Store や、壊れた analysis.json が1つあっても、ほかの分析は使えるようにする
    # （2026-10-06 通し試験: analyses/ に .DS_Store があると NotADirectoryError で道具が全部止まっていた）
    for d in sorted(ANALYSES_DIR.iterdir()):
        if not d.is_dir():
            continue
        try:
            meta = _read_json(d / "analysis.json")
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("owner") == user_id:
            out.append(Analysis(d.name))
    return out


def _is_active(a: Analysis) -> bool:
    """まだ終わっていない分析か（取得中・AI の仕事が残っている）"""
    acq = a.meta.get("acquisition")
    if acq and acq.get("status") == "cancelled":   # 利用者がやめた取得（別の楽曲ページでやり直した）
        return False
    if acq and acq.get("status") != "done":
        return True
    st = _state(a)
    if st is None:
        return bool(acq)          # 取得は済んだが AI の仕事はまだ始まっていない
    return not _all_done(st)


def _acq_status(a: Analysis):
    return (a.meta.get("acquisition") or {}).get("status")


def _song_key(a: Analysis) -> str:
    return _norm((a.meta.get("song") or {}).get("title") or a.title)


def _superseded(a: Analysis, others: list) -> bool:
    """止まった（failed）まま放ってある分析で、同じ曲をあとから頼み直しているか"""
    if _acq_status(a) != "failed":
        return False
    k, at = _song_key(a), a.meta.get("created_at", "")
    return any(b is not a and _song_key(b) == k and b.meta.get("created_at", "") > at for b in others)


def _pick(cands: list) -> Analysis:
    """候補が複数なら、進行中のものを優先し、それでも複数なら新しいもの。
    進行中が無ければ、やめた分析より完成済み・止まった分析を先にする（2026-10-06 通し試験: 完成済みより新しい「やめた分析」があると、
    「〇〇のレポートの3章を直して」がやめた分析に向かっていた）。
    止まった（failed）まま放ってある分析は、同じ曲をあとから頼み直していれば進行中に数えない（頼み直したほうを先にする）"""
    if len(cands) == 1:
        return cands[0]
    act = [a for a in cands if _is_active(a) and not _superseded(a, cands)]
    if not act:
        act = [a for a in cands if _acq_status(a) != "cancelled"]
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
        # 題がぴったり合う分析を先にする（「Lemon」で頼んで、取得中の「Lemonade」を選ばない）
        exact = [a for a in hits if ref_n in (a.title.strip().lower(), str((a.meta.get("song") or {}).get("title") or "").strip().lower())]
        return _pick(exact or hits)
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
    if h:
        return f"{h}時間{m}分" if m else f"{h}時間"   # 「2時間0分」としない
    return f"{max(m, 1)}分"


def come_back_line(eta_seconds, work: str, title: str, then: str) -> str:
    """2段の操作（Mac が取る → 利用者が「続けて」）の最初の返事に、AI がそのまま入れる一文（2026-10-06 ユーザー
    「最初の依頼時の AI の返答で、xxx分後に戻ってきて頼んでもらう必要があることを、事情と共にユーザに言おう。簡潔にな」）。
    使う所: 分析を始める（start_analysis）・界隈の掘り下げ（deepen）・界隈の切り直し（recut。取り足しが要るとき）。
    work は「〇〇するのに」の〇〇、then は「続けて」のあとに AI がすること"""
    if eta_seconds:
        span = f"{work}に約{_span(eta_seconds)}かかります（{_eta_clock(eta_seconds)}に終わる見込み）。"
    else:
        span = f"{work}に時間がかかります。"
    return f"{span}その間 AI は待てないため、Mac に通知が出たら「{title}の分析を続けて」と頼んでください。{then}"


def _span(sec: float) -> str:
    """一文に入れる長さ: 2時間以上は30分単位（「12時間」「12時間半」）、それより短いと5分単位（「25分」「1時間25分」）"""
    sec = max(0, int(sec))
    if sec >= 2 * 3600:
        half = round(sec / 1800)
        return f"{half // 2}時間" + ("半" if half % 2 else "")
    m = max(5, int(round(sec / 300)) * 5)
    return f"{m // 60}時間{m % 60}分" if m >= 60 and m % 60 else f"{m // 60}時間" if m >= 60 else f"{m}分"


def _clock_fine(eta_seconds: float, now: datetime.datetime | None = None) -> str:
    """2時間より短い見込みの時刻を「1時40分ごろ」の形で（5分単位に切り上げ）"""
    now = now or datetime.datetime.now().astimezone()
    t = now + datetime.timedelta(seconds=max(0, int(eta_seconds or 0)))
    t += datetime.timedelta(minutes=(-t.minute) % 5)
    day = "" if t.date() == now.date() else f"明日（{t.month}/{t.day}）の"
    return f"{day}{t.hour}時{t.minute:02d}分ごろ" if t.minute else f"{day}{t.hour}時ごろ"


def _eta_clock(eta_seconds: float) -> str:
    """終わる見込みの時刻。2時間未満は5分単位（_clock_fine）、それ以上は30分単位（_clock）。
    短いときに30分単位で切り下げると、今より前の時刻になる（13:05 に残り10分 →「13時ごろ」。2026-10-06 通し試験）"""
    return _clock_fine(eta_seconds) if eta_seconds < 2 * 3600 else _clock(eta_seconds)


def _clock(eta_seconds: float, now: datetime.datetime | None = None) -> str:
    """終わる見込みの時刻を「今日の23時ごろ」「明日（10/4）の6時半ごろ」の形で"""
    now = now or datetime.datetime.now().astimezone()
    t = now + datetime.timedelta(seconds=max(0, int(eta_seconds or 0)))
    hm = f"{t.hour}時" + ("半" if 20 <= t.minute < 50 else "")
    if t.minute >= 50:
        t2 = t + datetime.timedelta(hours=1)
        hm, t = f"{t2.hour}時", t2
    days = (t.date() - now.date()).days
    if days == 0:
        return f"今日の{hm}ごろ"
    if days == 1:
        return f"明日（{t.month}/{t.day}）の{hm}ごろ"
    return f"{t.month}/{t.day} の{hm}ごろ"


def _acq_view(a: Analysis):
    """取得の段の様子。取得が無い（試作）か済んでいれば None"""
    acq = a.meta.get("acquisition")
    if not acq or acq.get("status") == "done":
        return None
    back = f"終わったら「{a.title}の分析を続けて」と言ってください。"
    if acq.get("status") == "cancelled":
        return {"state": "cancelled", "message": "この取得はやめました（" + (acq.get("cancel_reason") or "利用者がやめた") + "）。",
                "eta_seconds": None}
    if acq.get("status") == "failed":
        if "日本の地域では使えません" in str(acq.get("error") or ""):
            # 日本の地域で使えない楽曲ページ（acquire/pipeline.py の UNAVAILABLE_STOP）。続きから取り直しても同じなので、再開は勧めない。
            # 止まった文に、ほかの楽曲ページでのやり直し方が書いてある
            return {"state": "failed", "message": f"取得が止まりました。{acq.get('error')}。", "eta_seconds": None,
                    "dead_end": True}
        if LOCAL is not None:
            msg = (f"取得が止まりました（{acq.get('error')}）。UGC Analyzer のメニュー「止まった取得を続きから再開」で、"
                   "済んだところの続きから取れます。何度も止まるときは運営に連絡してください。")
        else:
            msg = f"取得が止まりました（{acq.get('error')}）。運営に連絡してください。"
        return {"state": "failed", "message": msg, "eta_seconds": None}
    try:
        from acquire import launch
        p = launch.progress(a.id)
    except Exception as e:  # 見込みが出せなくても状態は返す
        p = {"status": acq.get("status"), "eta_seconds": None, "error": str(e)}
    eta = p.get("eta_seconds")
    when = f"終わるのは{_eta_clock(eta)}の見込み（あと約{_hm(eta)}）。" if eta else ""
    step = p.get("step_label") or "準備"
    if p.get("step") == "comments" and p.get("n_pool"):
        step += f" {p.get('comments_done') or 0}/{p['n_pool']}本"
    if LOCAL is not None:
        app = LOCAL.app_state() or {}
        if not app.get("running"):
            msg = ("あなたの Mac の UGC Analyzer が動いていません（メニューバーに割れた音符のアイコンが無い）。"
                   "アプリケーションフォルダの「UGC Analyzer」を開けば、済んだところの続きから取ります。")
            return {"state": "stopped", "message": msg, "eta_seconds": eta}
        if app.get("login_wanted"):
            msg = ("UGC Analyzer が TikTok のログインを待っています。UGC Analyzer が開いた Chrome で、分析専用のサブアカウントでログインしてください。"
                   "ログインすれば続きから取ります。")
            return {"state": "login", "message": msg, "eta_seconds": eta}
        if p.get("status") == "running":
            msg = f"あなたの Mac で取得中（{step}）。{when}"
        elif p.get("ahead"):
            msg = f"あなたの Mac で取得の順番待ち（前に{p['ahead']}件）。{when}"
        else:
            msg = f"あなたの Mac で取得の開始待ち。{when}"
        msg += back + "そのあいだ Mac を開いたまま・電源につないでおいてください（画面は消えてもかまいません）。"
        return {"state": "acquiring", "message": msg, "eta_seconds": eta}
    if p.get("status") == "queued" and p.get("ahead"):
        msg = f"取得の順番待ち（前に{p['ahead']}件）。{when}"
    elif p.get("status") == "running":
        msg = f"取得中（{step}）。{when}"
    else:
        msg = f"取得の開始待ち。{when}"
    return {"state": "acquiring", "message": msg + back, "eta_seconds": eta}


def status(user_id: str, ref: str | None = None) -> dict:
    with _lock:
        targets = [resolve(user_id, ref)] if ref else list_analyses(user_id)
        items = []
        for a in targets:
            av = _acq_view(a)
            if av:
                item = {"analysis_id": a.id, "title": a.title, "song": a.song_line, "state": av["state"],
                        "progress": "取得の段", "message": av["message"], "eta_seconds": av["eta_seconds"]}
                if av["state"] != "cancelled":
                    item["pages"] = _acq_pages(a)
                items.append(item)
                continue
            dv = _deepen_view(a)
            if dv:
                items.append({"analysis_id": a.id, "title": a.title, "song": a.song_line, "state": dv["state"],
                              "progress": _acq_label(a), "message": dv["message"], "eta_seconds": dv["eta_seconds"]})
                continue
            st = _load_state(a)
            cur = _current(st)
            if cur is None or cur["kind"] == "done":
                state_label, msg = "done", "完了しています。"
                if LOCAL is not None and is_w1(a):   # 取得アプリの形: 成果物のフォルダも出す
                    import flow_w1
                    if flow_w1.REPORTS_DIR and a.outputs("REPORT.md").exists():
                        msg += "成果物: " + "".join(x.strip() for x in flow_w1.folder_lines(a)).replace("- フォルダ: ", "", 1)
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
        text = "\n".join(f"- {i['title']}（{i['analysis_id']}）: {i['message']} 進み具合 {i['progress']}"
                         + "".join(f"\n  - 取っている楽曲ページ: {_page_line(p['url'], p)}" for p in i.get("pages") or [])
                         for i in items)
        return {"text": text, "analyses": items}


def _acq_pages(a: Analysis) -> list:
    """取得の段の分析が取っている楽曲ページ（題・作者・UGC 数・URL）。受け付けのときに読んだ記録（raw/music_pages.json）から。
    start_analysis の返事が時間切れで途切れても、status から利用者に伝えられるように（2026-10-06 友達の試し。mcp_proto.START_CUT_RULE）"""
    used = [u for u in (a.meta.get("music_urls") or [a.meta.get("music_url")]) if u]
    seen = {r.get("url"): r for r in (_read_json(ANALYSES_DIR / a.id / "raw" / "music_pages.json", []) or [])
            if isinstance(r, dict)}
    return [{"url": u, **{k: (seen.get(u) or {}).get(k) for k in ("title", "creator", "video_count_text")},
             **({"kind": "同じ曲のファンの音源（取得のはじめに見つけて足した）"} if (seen.get(u) or {}).get("kind") == "fan" else {})}
            for u in used]


MUSIC_URL_RE = re.compile(r"^https://(www\.)?tiktok\.com/music/[^\s/]+-\d+")
MAX_ACTIVE_PER_USER = 2


def _title_from_music_url(music_url: str) -> str:
    """楽曲ページの URL の曲名の部分（URL だけで頼まれたとき、題名が URL だらけにならないように）"""
    from urllib.parse import unquote
    slug = unquote(music_url.rstrip("/").split("?")[0].rsplit("/", 1)[-1])
    return re.sub(r"-\d+$", "", slug).replace("-", " ").strip() or music_url


VIDEO_URL_RE = re.compile(r"^https://(www\.)?tiktok\.com/@[^/\s]+/(video|photo)/\d+")


def _norm(s) -> str:
    return re.sub(r"[\s・\-_/／（）()「」『』【】!！?？.,、。~〜–—]", "", str(s or "")).lower()


def _music_search_hint(song: str, artist: str, note: str = "") -> dict:
    """取得アプリの形で、楽曲ページの URL が無いとき: AI に探し方を返す（分析は作らない）。
    Mac からの検索は Bot 判定で塞がれるため、AI がウェブ検索で見つけて渡す（2026-10-04）。
    ユーザー「URL を渡すのは最後の手段。アーティスト名・曲名で Claude に頑張らせる」"""
    s_, a_ = song or "", artist or ""
    queries = [f"{s_} – {a_} tiktok", f"{s_} {a_} tiktok 踊ってみた", f"{s_} {a_} tiktok 歌ってみた",
               f"site:tiktok.com {s_}", f"site:tiktok.com/music {s_}"]
    text = ((note + "\n\n") if note else "") + (
        "まだ取得を始めていない。楽曲ページは、**その曲を使った TikTok の動画から Mac が見つける**（利用者に URL を頼むのは最後の手段）。\n"
        "ウェブ検索には TikTok の楽曲ページ（tiktok.com/music/…）はほとんど出ないが、その曲を使った動画（tiktok.com/@…/video/…）はよく出る。"
        "Mac が動画のページを開いて音源を読み、楽曲ページを見つけて UGC 数を比べるので、動画を集めて渡すのが本筋。\n\n"
        "1. 次の検索を順に試す（少なくとも3つ）:\n" +
        "\n".join(f"   - {q.strip()}" for q in queries) +
        f"\n2. 出てきた `https://www.tiktok.com/@…/video/…` の動画を **5〜{MAX_VIDEOS}本**、video_urls に入れる。"
        "**一般の人の動画を中心に**（踊ってみた・歌ってみた・歌詞動画など。一番使われている音源を拾うため）、公式アカウントの動画も1〜2本。"
        "同じ人の動画ばかりにしない\n"
        "3. `https://www.tiktok.com/music/…` の URL が出てきたら、それも全部 candidate_urls に入れる\n"
        "4. video_urls と candidate_urls を付けて start_analysis を呼び直す（Mac が音源を読んで UGC 数を比べ、一番使われているページと、"
        "その3割以上使われている同じ曲の公式のページ（sped up 版など）を合わせて取る。数十秒かかる）\n"
        "5. 1〜4 を全部試しても動画も楽曲ページも見つからないときだけ、利用者に「TikTok アプリでその曲の音源のページを開き、"
        "共有 → リンクをコピー で URL を送ってください」と頼む")
    return {"text": text, "analysis_id": None, "created": False, "needs": "music_url"}


MAX_CANDIDATES = 6
MAX_VIDEOS = 8      # 音源を読む動画の上限（1本数秒。道具の返事が遅くなりすぎないように）
MIN_FIT = 3         # 楽曲ページが1つしか見つからないとき、その音源を使った動画がこの本数あれば「ほかに無い」とみなす
# 同じ曲の公式の楽曲ページ（配信版・先行版・sped up など）は、一番使われているページの UGC のこの割合以上なら合わせて取る
# （2026-10-04 ユーザー「20%でよい」。少ないものは外して、外したと伝える。「それも入れて」で足せる）
# 2026-10-07 3割に上げた（ユーザー「むしろ3割にあげるわ」。12曲の分布で、ほかの音源は2割以上と8%未満に分かれ、
# 2割台の音源（きゃわの なぎ 28%・片栗粉 22%）は50万再生以上が少ない。docs/LIST_CUT.md 第8章）
JOIN_RATIO = 0.3


def _music_fits(song: str, artist: str, info: dict) -> bool:
    """題が曲名に合い、作者がアーティスト名に合う（公式の音源）。「オリジナル楽曲 - 〇〇」のような個人の音源は合わない。
    読めなかった項目は合うとみなす"""
    t, c = _norm(info.get("title")), _norm(info.get("creator"))
    ok_t = not _norm(song) or not t or _norm(song) in t
    ok_c = not _norm(artist) or not c or _norm(artist) in c or c in _norm(artist)
    return ok_t and ok_c


def _pick_music_pages(song: str, artist: str, urls: list, take_all: bool = False) -> tuple:
    """楽曲ページの候補を開いて比べる。戻り値は (取るページ [(url, info)]（1つ目が主）, 外したページ [(url, info, 理由)])。
    - 題・作者が曲名・アーティスト名に合うもの（公式の音源）のうち、UGC 数が一番多いページを主にする
      （2026-10-04: きゃわぽっぴんどぅーは同じ題・作者のページが3つあり、UGC 1,632 / 17.7K / 31.2K。本命は 31.2K）
    - ほかの公式のページは、主の UGC の JOIN_RATIO 以上なら合わせて取る（sped up 版など）
    - take_all（利用者が URL を2つ以上渡した）: 全部取る。主は UGC 数が一番多いもの
    - 日本の地域で使えないページ（unavailable）は、どちらでも外す（理由 UNAVAILABLE_WHY）。全部使えなければ、取るページは空"""
    infos = LOCAL.inspect_many(urls) or []
    rows = [(u, (infos[i] if i < len(infos) else {}) or {}) for i, u in enumerate(urls)]
    n = lambda r: r[1].get("video_count") or 0   # noqa: E731
    gone = [(*r, UNAVAILABLE_WHY) for r in rows if r[1].get("unavailable")]
    rows = [r for r in rows if not r[1].get("unavailable")]
    if not rows:
        return [], gone
    if take_all:
        return sorted(rows, key=n, reverse=True), gone
    good = [r for r in rows if _music_fits(song, artist, r[1])]
    best = max(good or rows, key=n)
    take, dropped = [best], []
    for r in rows:
        if r[0] == best[0]:
            continue
        if r not in good:
            dropped.append((*r, "題か作者が曲名・アーティスト名と合わない（個人の音源など）"))
        elif not n(r) or not n(best):
            dropped.append((*r, "UGC 数が読めなかった"))
        elif n(r) >= JOIN_RATIO * n(best):
            take.append(r)
        else:
            dropped.append((*r, f"UGC が一番多いページの{int(JOIN_RATIO * 100)}%未満"))
    take = [take[0]] + sorted(take[1:], key=n, reverse=True)
    return take, dropped + gone


def _unreadable(info: dict) -> bool:
    """楽曲ページの題・作者・UGC 数が1つも読めなかったか（読み込みがたまたま失敗したなど）。
    日本の地域で使えないという文が出ていたページ（unavailable）は、ここに来る前に外している。試験で開かなかったもの（skipped）は含めない"""
    return not info.get("skipped") and not any(info.get(k) for k in ("title", "creator", "video_count_text", "video_count"))


# 日本の地域で使えない楽曲ページ（ログインなしで開くと「この楽曲はご利用になれません。このサウンドはお住いの国または地域では
# ご利用になれません」と出る。acquire/pipeline.read_music_page が unavailable=True にする）を外した理由
UNAVAILABLE_WHY = "日本の地域では使えない（TikTok の地域の制限）"


def _unavailable_hint(song: str, artist: str, urls: list) -> dict:
    """取るはずの楽曲ページが全部、日本の地域で使えないとき: 分析を作らず、AI にほかのページの探し方を返す
    （2026-10-06 ユーザーの決定。一覧の段が数分で止まり、取り直しても同じになるため。利用者には確認を求めない）"""
    s_, a_ = song or "", artist or ""
    queries = [f"site:tiktok.com/music {s_}", f"{s_} {a_} sped up tiktok", f"{s_} {a_} tiktok 音源",
               f"site:tiktok.com {s_} {a_}"]
    which = "この楽曲ページ" if len(urls) == 1 else "これらの楽曲ページ"
    text = (
        f"まだ取得を始めていない。{which}は日本では使えない（TikTok の地域の制限。ログインなしで開くと「この楽曲はご利用になれません」と出て、"
        "動画の一覧が取れない）:\n" + "\n".join(f"  - {u}" for u in urls) + "\n\n"
        "1. ウェブ検索で、同じ曲のほかの公式の楽曲ページ（`https://www.tiktok.com/music/…`。sped up 版・別のアップロードなど）を探す。"
        "次の検索を順に試す:\n" +
        "\n".join(f"   - {q.strip()}" for q in queries) +
        "\n2. 見つかれば、その URL を music_urls に入れて start_analysis を呼び直す（上の使えないページは入れない）\n"
        "3. 何通りか探しても無ければ、利用者に「この曲は TikTok の日本の地域で使えないため分析できません」と伝えて止まる")
    return {"text": text, "analysis_id": None, "created": False, "needs": "music_url", "unavailable": list(urls)}


def _page_line(url: str, info: dict) -> str:
    shown = "／".join(x for x in (info.get("title"), info.get("creator")) if x)
    ugc = f"UGC {info['video_count_text']}" if info.get("video_count_text") else "UGC 読めず"
    return f"{('『' + shown + '』') if shown else ''}（{ugc}）{url}"


def _clean_urls(urls) -> list:
    out = []
    for u in urls or []:
        u = (u or "").strip().split("?")[0]
        if u and MUSIC_URL_RE.match(u) and u.rsplit("-", 1)[-1] not in {c.rsplit("-", 1)[-1] for c in out}:
            out.append(u)
    return out


# 返信欄を開いて返信を取るときの取得の設定。2026-10-05 から既定は取らない（acquire/pipeline.py の DEFAULTS）。
# 利用者が頼んだ分析だけ、道具の replies で取る（ユーザー「返信とるかどうかはオプションとして選択できるように。Claude から道具で指定できるのが望ましい」）
REPLIES_ON = {"reply_top": 1, "reply_questions": 1, "reply_author": 1}
REPLIES_NOTE = "返信も取ります（返信欄を開くので、コメントの取得が2〜3割長くなります）。"


def wants_replies(meta: dict) -> bool:
    """この分析は返信も取る指定か"""
    s = meta.get("acquisition_settings") or {}
    return any(int(s.get(k) or 0) > 0 for k in REPLIES_ON)


def min_plays_of(meta: dict):
    """この分析で指定した、週ごとに選ぶ動画の再生の下限（指定が無ければ None＝既定）"""
    v = (meta.get("acquisition_settings") or {}).get("min_plays_weekly")
    return int(v) if v is not None else None


CONFIRM_ONCE = "（途中で1回、界隈の分け方を確認します）"
CONFIRM_SKIPPED = "（界隈の分け方の確認は省いて、最後まで書きます）"


def acquisition_settings_for(replies: bool, min_plays: int | None = None, fan_sounds: bool = True) -> dict | None:
    """新しい分析の目録に書く取得の設定（取得アプリの設定に、返信・再生の下限の指定を重ねる）。
    fan_sounds=False（利用者が楽曲ページを渡した）なら、同じ曲のファンの音源を探さない（acquire/pipeline.Run.add_fan_sounds）"""
    s = dict(LOCAL.acquisition_settings() or {}) if LOCAL is not None else {}
    if replies:
        s.update(REPLIES_ON)
    if min_plays is not None:
        s["min_plays_weekly"] = max(0, int(min_plays))
    if not fan_sounds:
        s["fan_sounds"] = 0
    return s or None


def _written_once(a: Analysis) -> bool:
    """レポートが一度でも完成したか（完了の仕事を渡し終えた。そのあとの直し・掘り下げ・切り直しで仕事が足されていても完成済み）"""
    st = _state(a)
    return bool(st) and any(t.get("kind") == "done" and t.get("status") == "done" for t in st.get("tasks") or [])


def _acquired_unwritten(user_id: str, song: str, urls) -> "Analysis | None":
    """同じ利用者・同じ曲（launch.find_active と同じ当て方）の、取得が済んでいて（acquisition done）、
    レポートがまだ完成していない分析（新しいもの）。完成済み・止まった（failed）・やめた分析は見ない"""
    from acquire import launch
    for aid in launch.find_acquired(user_id, song, [u for u in urls or [] if u]):
        try:
            a = Analysis(aid)
            if not _written_once(a):
                return a
        except (RunnerError, OSError, ValueError):   # 壊れた目録・仕事の状態は飛ばす
            continue
    return None


def _resume_reply(a: Analysis, replies: bool = False, min_plays: int | None = None) -> dict:
    """取得が済んでレポートがまだの曲を、もう一度「UGC Analyzer で〇〇を分析して」と頼まれたとき: 新しく作らず、そのまま書き始める
    （2026-10-06 ユーザーの決定 B。前は取得中・順番待ちしか見ず、同じ曲を2〜3時間取り直していた）。
    待ちではないので、接続口（mcp_proto._waiting_text）は kind で見分けて待ちの一文を付けない"""
    lines = [f"『{a.title}』はもう集め終わっています（分析 ID: {a.id}）。このまま書き始めます。"]
    what = "・".join(x for x in ("返信も取る" if replies else "", "再生の下限" if min_plays is not None else "") if x)
    if what:   # 黙って捨てない（すでに受け付けている取得のときと同じく伝える）
        lines.append(f"（{what}の指定は、もう集め終わったこの分析には効きません）")
    lines.append(f"（AI へ: 利用者にはそう短く伝え、すぐ next_task（analysis_id={a.id}）を呼んで、"
                 f"「{a.title}の分析を続けて」と言われたときと同じに進める。取得を待たない）")
    return {"text": "\n".join(lines), "analysis_id": a.id, "created": False, "kind": "resume", "state": "acquired"}


def start_analysis(user_id: str, song: str, artist: str = "", music_url: str = "", video_url: str = "",
                   candidate_urls: list | None = None, only_one: bool = False, music_urls: list | None = None,
                   video_urls: list | None = None, replies: bool = False, min_plays: int | None = None,
                   skip_confirm: bool = False, before_create=None) -> dict:
    """分析を作って取得の待ち行列に入れる。replies=True なら返信も取る（既定は取らない）。
    min_plays は週ごとに選ぶ動画の再生の下限（省けば acquire/pipeline.py の既定 min_plays_weekly）。
    skip_confirm=True なら、取得のあとの界隈の確認を省いて最後まで書く。
    before_create は、楽曲ページを読んで選び終えてから、同じ曲の受け付け済みを探して分析を作る直前に（ロックの中で）呼ぶもの
    （restart_analysis が前の取得をやめる。ページを読む所で失敗しても前の取得が残るように）"""
    res = _start_analysis(user_id, song, artist, music_url, video_url, candidate_urls, only_one, music_urls, video_urls,
                          replies, min_plays, before_create)
    if skip_confirm and res.get("analysis_id"):
        _set_options(ANALYSES_DIR / res["analysis_id"], skip_confirm=True, skip_confirm_at=_now())
        res["text"] = res["text"].replace(CONFIRM_ONCE, CONFIRM_SKIPPED)
    if not res.get("analysis_id"):   # 楽曲ページ探しの案内（分析はまだ作っていない）
        if skip_confirm:
            res["text"] += "\n（AI へ: 利用者は界隈の確認を省くように頼んでいる。start_analysis を呼び直すときも skip_confirm=true を付ける）"
        if replies:
            res["text"] += "\n（AI へ: 利用者は返信も取るように頼んでいる。start_analysis を呼び直すときも replies=true を付ける）"
        if min_plays is not None:
            res["text"] += f"\n（AI へ: 利用者は再生の下限を指定している。start_analysis を呼び直すときも min_plays={int(min_plays)} を付ける）"
    return res


def _start_analysis(user_id: str, song: str, artist: str = "", music_url: str = "", video_url: str = "",
                    candidate_urls: list | None = None, only_one: bool = False, music_urls: list | None = None,
                    video_urls: list | None = None, replies: bool = False, min_plays: int | None = None,
                    before_create=None) -> dict:
    """分析を作って取得の待ち行列に入れる（取得は Web サービスの外の係か、利用者の Mac の取得アプリが走らせる）。
    取得アプリの形では、どの楽曲ページで進めるか（題・作者・UGC 数・URL）を返事に出す（2026-10-04 ユーザー
    「止めるのではなく、このページで進めるからね、ってのがプロンプトに出るくらいがいい」）。
    同じ曲の楽曲ページが複数あれば合わせて取る（candidate_urls は比べて選ぶ、music_urls は利用者が渡したもので全部取る）。
    video_urls（その曲を使った動画）は、Mac が動画のページを開いて音源を読み、楽曲ページの候補にする
    （2026-10-04: AI のウェブ検索には楽曲ページがほぼ出ないが、動画は出る）"""
    song, artist, music_url = (song or "").strip(), (artist or "").strip(), (music_url or "").strip().split("?")[0]
    user_urls = _clean_urls(music_urls)
    if not song and not music_url and not user_urls:
        raise RunnerError("曲名（とアーティスト名）か、TikTok の楽曲ページの URL を教えてください")
    if music_url and not MUSIC_URL_RE.match(music_url):
        raise RunnerError("楽曲ページの URL は https://www.tiktok.com/music/曲名-数字 の形です")
    bad = [u for u in (music_urls or []) if (u or "").strip() and not MUSIC_URL_RE.match((u or "").strip().split("?")[0])]
    if bad:
        raise RunnerError("楽曲ページの URL は https://www.tiktok.com/music/曲名-数字 の形です: " + ", ".join(bad))
    if not song:
        song = _title_from_music_url(music_url or user_urls[0])
    if before_create is None:   # 取得が済んでレポートがまだの同じ曲は、取り直さずにそのまま書き始める（TikTok を開く前に見る）
        done = _acquired_unwritten(user_id, song, [music_url] + user_urls)
        if done is not None:
            return _resume_reply(done, replies, min_plays)
    found_via = ""
    dropped = []
    pages = None     # [(url, info)]。1つ目が主
    if user_urls:    # 利用者が渡した楽曲ページ: 探し直さず、全部から取る
        cands = _clean_urls([music_url] + user_urls) if music_url else user_urls
        only_one = True
    else:
        cands = _clean_urls([music_url] + list(candidate_urls or []))
    vids = []
    for v in [video_url] + list(video_urls or []):
        v = (v or "").strip().split("?")[0]
        if v and v not in vids:
            vids.append(v)
    n_read = n_fit = 0
    survey = {}
    if LOCAL is not None and not user_urls and (vids or not only_one):
        # 楽曲ページを TikTok で探す: 曲名の discover のページの人気の動画と、渡された動画の音源を読む（2026-10-04: AI のウェブ検索には
        # 楽曲ページがほぼ出ず、出てくる動画は古いものが多く、先行版や個人の音源ばかりだった）。only_one なら渡された動画だけ読む
        bad_v = [v for v in vids if not VIDEO_URL_RE.match(v)]
        if bad_v:
            return _music_search_hint(song, artist, "video_urls は https://www.tiktok.com/@投稿者/video/数字 の形にする"
                                                    f"（形の違うもの: {', '.join(bad_v[:3])}）。")
        try:
            survey = LOCAL.find_sounds(song, vids, discover=not only_one, artist=artist) or {}
        except Exception as e:
            survey = {"error": type(e).__name__}
        freq, other = {}, []
        for vm in survey.get("reads") or []:
            if not vm.get("music_url"):
                continue
            n_read += 1
            if _music_fits(song, artist, {"title": vm.get("title"), "creator": vm.get("author")}):
                freq[vm["music_url"]] = freq.get(vm["music_url"], 0) + 1
            else:
                other.append(vm)
        n_fit = sum(freq.values())
        if not freq and not cands:
            if vids and n_read:
                seen = "、".join(f"『{vm.get('title')}』（{vm.get('author')}）" for vm in other[:3])
                return _music_search_hint(song, artist, f"渡された動画などの音源は {seen} で、頼まれた曲「{song}」と違う（個人のオリジナル音源など）。"
                                                        "公式の音源を使った別の動画（一般の人の踊ってみた・歌ってみたなど）を探して渡す。")
            return _music_search_hint(song, artist,
                f"Mac が TikTok で「{song}」の人気の動画を開いて音源を読んだが（{survey.get('found', 0)}本見つけ、{n_read}本読めた）、"
                "曲名・アーティスト名に合う公式の音源が見つからなかった。曲名の表記（カタカナ・英字・記号）を確かめ、下の探し方で動画か楽曲ページを探す。")
        known = {c.rsplit("-", 1)[-1] for c in cands}
        for u in sorted(freq, key=lambda u: -freq[u]):   # 多くの動画で使われている音源から
            if u.rsplit("-", 1)[-1] not in known:
                cands.append(u)
                known.add(u.rsplit("-", 1)[-1])
    if LOCAL is not None and len(cands) == 1 and not only_one and n_fit < MIN_FIT:
        # 楽曲ページが1つしか見つかっておらず、根拠の動画も少ない: ほかの版（配信版・先行版）を探し直させる（2026-10-04 きゃわぽっぴんどぅー。
        # Claude が渡した1つは UGC 1,632 で、本命は 31.2K だった）。AI の中のやりとりで、利用者には聞かない
        if not n_fit:
            return _music_search_hint(song, artist,
                f"楽曲ページが1つだけ渡された（{cands[0]}）。Mac が TikTok で探しても、ほかの版は見つからなかった"
                f"（人気の動画 {survey.get('found', 0)}本のうち、この曲の公式の音源を使った動画が無かった）。"
                "同じ曲の楽曲ページは配信版・先行版・sped up 版などで複数あることが多い。**その曲を使った動画を video_urls に入れて**、"
                "今のページ（candidate_urls）と一緒に呼び直す（Mac が動画の音源を読んで、ほかの版を探す）。"
                "動画が1本も見つからなければ、同じ URL を music_url に入れ、only_one=true を付けて呼び直す。"
                "利用者が URL を指定した場合は、探し直さずに music_urls に入れて呼ぶ。")
        return _music_search_hint(song, artist,
            f"この曲の公式の音源を使った動画が {n_fit} 本しか見つからず、楽曲ページも1つだけ（{cands[0]}）。公式アカウントや初期の動画は、"
            f"先行版などあまり使われていない音源のことがある。**一般の人の動画（踊ってみた・歌ってみた・歌詞動画など）を、あと {MIN_FIT - n_fit} 本以上**"
            "探して video_urls に入れて呼び直す。探しても動画がほかに無ければ、only_one=true を付けて呼び直す。")
    if LOCAL is not None and len(cands) >= 2:   # 候補を比べる（利用者が渡したものは全部取る）
        pages, dropped = _pick_music_pages(song, artist, cands[:MAX_CANDIDATES], take_all=bool(user_urls))
        if not pages:   # どれも日本の地域で使えない: 分析を作らず、ほかのページの探し方を返す
            return _unavailable_hint(song, artist, [u for u, _i, _w in dropped])
        music_url = pages[0][0]
        k = min(len(cands), MAX_CANDIDATES)
        where = (f"TikTok で人気の動画など {n_read} 本の音源から見つけた楽曲ページ" if survey.get("found") else
                 f"動画 {n_read} 本の音源などから見つけた楽曲ページ" if n_read else "同じ曲の楽曲ページ")
        n_gone = sum(1 for _u, _i, w in dropped if w == UNAVAILABLE_WHY)
        if user_urls and n_gone:
            found_via = (f"（渡された楽曲ページ {len(pages) + n_gone} つのうち、日本の地域で使える {len(pages)} つ"
                         f"{'を合わせて取る' if len(pages) > 1 else 'で取る'}）")
        elif user_urls:
            found_via = f"（渡された楽曲ページ {len(pages)} つを全部合わせて取る）"
        elif not any(i.get("video_count") for _, i in pages) and not any(i.get("video_count") for _, i, _w in dropped):
            # どのページも UGC 数が読めず、比べられていない（「一番使われているものを選んだ」と言わない）
            found_via = f"（{where} {k} つは、どれも UGC 数が読めず、比べられなかった）"
        elif len(pages) > 1:
            found_via = (f"（{where} {k} つを比べ、"
                         f"一番使われているページの{int(JOIN_RATIO * 100)}%以上使われている {len(pages)} つを合わせて取る）")
        else:
            found_via = f"（{where} {k} つを比べて、一番使われているものを選んだ）"
    elif cands and not music_url:
        music_url = cands[0]
        if n_read:
            found_via = (f"（TikTok で人気の動画など {n_read} 本の音源を読み、この曲の公式の音源はこのページだけだった）"
                         if survey.get("found") else f"（動画 {n_read} 本の音源から楽曲ページを見つけた）")
    if LOCAL is not None and not music_url:
        return _music_search_hint(song, artist)
    from acquire import launch
    if LOCAL is not None and pages is None and (before_create is not None
                                                or launch.find_active(user_id, song, music_url) is None):
        # 楽曲ページが1つ: 分析を作る前に開いて、題・作者・UGC 数を読む（日本の地域で使えないページなら作らない）。
        # 同じ曲の取得をもう受け付けていれば読まない（返事は受け付け済みのページを出す）
        try:
            info = LOCAL.inspect_music(music_url) or {}
        except Exception as e:
            info = {"error": type(e).__name__}
        if info.get("unavailable"):
            return _unavailable_hint(song, artist, [music_url])
        pages = [(music_url, info)]
    urls = [u for u, _ in pages] if pages else [music_url]
    with _lock:
        if before_create is not None:
            before_create()
        else:   # ページを選んだあとでもう一度（題が違っても、同じ楽曲ページを取り終えた分析があれば、そのまま書き始める）
            done = _acquired_unwritten(user_id, song, urls)
            if done is not None:
                return _resume_reply(done, replies, min_plays)
        aid = launch.find_active(user_id, song, music_url)
        created = aid is None
        if created:
            active = [a for a in list_analyses(user_id)
                      if (a.meta.get("acquisition") or {}).get("status") in ("queued", "running")]
            if len(active) >= MAX_ACTIVE_PER_USER:
                raise RunnerError(f"取得中・順番待ちの分析が{len(active)}件あります。終わってから次を頼んでください: "
                                  + ", ".join(a.title for a in active))
            aid = launch.new_analysis(user_id, song, artist, music_url,
                                      acquisition_settings_for(replies, min_plays, fan_sounds=not user_urls), music_urls=urls)
    if LOCAL is not None and created and pages is None:   # どの楽曲ページで進めるかを見せるため、題・作者・UGC 数を読む（読めなくても進める）
        try:
            info = LOCAL.inspect_music(music_url) or {}
        except Exception as e:
            info = {"error": type(e).__name__}
        pages = [(music_url, info)]
    if LOCAL is not None and created and pages:
        from acquire import pipeline
        how = "受け付けのときに楽曲ページを開いて読んだ"
        rec = [{**info, "at": _now(), "url": u, "page": i, "how": how} for i, (u, info) in enumerate(pages, 1)]
        if any(r.get("video_count") for r in rec):
            pipeline.write_json(ANALYSES_DIR / aid / "raw" / "music_pages.json", rec)
            pipeline.write_json(ANALYSES_DIR / aid / "raw" / "music_page.json", rec[0])
    kick = LOCAL.ensure_app() if LOCAL is not None else launch.ensure_worker()
    p = launch.progress(aid)
    title = song
    eta = p.get("eta_seconds")
    head = f"「{title}」の{'取得を受け付けました' if created else '取得はもう受け付けています'}（分析 ID: {aid}）。"
    meta_now = _read_json(ANALYSES_DIR / aid / "analysis.json", {}) or {}
    mp = min_plays_of(meta_now)
    plays_line = (f"週ごとに選ぶ動画は再生{mp:,}以上にします（起点・大型ヒット・本人・公式などは再生に関わらず取ります）。"
                  if mp is not None else "")
    if min_plays is not None and not created and mp != max(0, int(min_plays)):   # 黙って捨てない（replies と同じく伝える）
        plays_line += ("（再生の下限の指定は、すでに受け付けている取得には効きません。再生の下限を変えるなら「取得をやめて、やり直して」と"
                       "頼んでください）")
    if wants_replies(meta_now):
        reply_line = REPLIES_NOTE
    elif replies and not created:
        reply_line = "（返信も取る指定は、すでに受け付けている取得には効きません。返信も取るなら「取得をやめて、やり直して」と頼んでください）"
    else:
        reply_line = ""
    reply_line += plays_line
    if LOCAL is not None:
        pg = pages or [(music_url, {})]
        warn = other = ""
        if not created:   # 受け付け済みの分析が実際に取っているページを出す（今回渡された・見つけたページではない）
            used = [u for u in (meta_now.get("music_urls") or [meta_now.get("music_url")]) if u]
            seen = {r.get("url"): r for r in (_read_json(ANALYSES_DIR / aid / "raw" / "music_pages.json", []) or [])
                    if isinstance(r, dict)}
            if used:
                if {u.rsplit("-", 1)[-1] for u in urls if u} - {u.rsplit("-", 1)[-1] for u in used}:
                    other = (f"\n（今回頼まれた楽曲ページではなく、受け付け済みの上のページで取っています。ページを替えるなら"
                             f"「{title}の取得をやめて、このページでやり直して」と頼んでください）")
                pg = [(u, seen.get(u) or {}) for u in used]
            found_via, dropped = "", []
        c0 = pg[0][1].get("creator")
        if artist and c0 and _norm(artist) not in _norm(c0) and _norm(c0) not in _norm(artist):
            warn = f"\n（作者が「{c0}」で、アーティスト名と違う。公式でない音源の可能性がある。違っていたら「取得をやめて、このページでやり直して」で直せる）"
        unread = [u for u, i in pg if _unreadable(i)] if created else []
        if unread:   # 地域の制限などで開けないページ。一覧の段も同じ開き方なので、取得が数分で止まりうる（止めずに、知らせて進める）
            which = "この楽曲ページ" if len(pg) == 1 else "楽曲ページ（" + "・".join(unread) + "）"
            warn += (f"\n（{which}は題・作者・UGC 数が1つも読めなかった。TikTok の地域の制限（「この楽曲はご利用になれません」）などで"
                     "開けないページかもしれず、取得が数分で止まることがある。止まったら、ほかの楽曲ページの URL を渡して"
                     f"「{title}の取得をやめて、このページでやり直して」で直せる）")
        if len(pg) > 1:
            total = sum(i.get("video_count") or 0 for _, i in pg)
            body = (f"次の楽曲ページを合わせて進めます{found_via}:\n" +
                    "\n".join(f"  - {_page_line(u, i)}" for u, i in pg) +
                    (f"\n  UGC は合わせて約{total:,}本です。" if total else ""))
        else:
            body = f"次の楽曲ページで進めます{found_via}: {_page_line(*pg[0])}"
        if dropped:   # 日本の地域で使えないページだけなら「それも入れて」とは言わない（入れても取れない）
            joinable = any(w != UNAVAILABLE_WHY for _u, _i, w in dropped)
            body += (("\n外した楽曲ページ（入れたいときは「それも入れて」と言ってください）:\n" if joinable else "\n外した楽曲ページ:\n") +
                     "\n".join(f"  - {_page_line(u, i)} … {why}" for u, i, why in dropped))
        fan = ("同じ曲をファンが上げた音源（別の部分の切り抜きなど）も探し、よく使われていれば合わせて取ります。"
               if created and int((meta_now.get("acquisition_settings") or {}).get("fan_sounds", 1) or 0) else "")
        lines = [head, body + other + warn,
                 "あなたの Mac の UGC Analyzer が、楽曲ページの動画一覧・属性・サムネを取り、コメントを取る動画を数字で決めてコメントを取ります。"
                 + fan + reply_line,
                 (f"前に{p['ahead']}件あります。" if p.get("ahead") else ""),
                 "そのあいだ Mac を開いたまま・電源につないでおいてください（画面は消えてもかまいません）。"]
    else:
        lines = [head,
                 "楽曲ページの動画一覧・属性・サムネを取り、コメントを取る動画を数字で決めてコメントを取ります。" + reply_line,
                 (f"前に{p['ahead']}件あります。" if p.get("ahead") else "")]
    back = come_back_line(eta, "TikTok から動画とコメントを集めるの", title,
                          "そこからレポートを書きます" + CONFIRM_ONCE + "。")
    ai = ("（AI へ: この内容を利用者に短く伝えて、ここで止まる。どの楽曲ページで進めるか（題・作者・UGC 数・URL）は省かずに伝える。"
          "返事の最後に、下の「」の文を言い換えずにそのまま入れる。取得を待たない・見に来ない。")
    if dropped:
        ai += "外した楽曲ページも伝える。"
    if any(w != UNAVAILABLE_WHY for _u, _i, w in dropped):
        ai += ("利用者が「それも入れて」と言ったら、restart_analysis に、上で進める楽曲ページと足すページの URL を"
               "全部 music_urls に入れて呼ぶ。")
    lines += [f"「{back}」", "", ai + "）"]
    return {"text": "\n".join(l for l in lines if l is not None), "analysis_id": aid, "created": created,
            "worker": kick, "eta_seconds": eta}


def _wait_task(a: Analysis, message: str, state: str | None = None) -> dict:
    """state は _acq_view の state（acquiring・cancelled・failed・stopped・login）。待てば進む待ちか、
    利用者が何かするまで進まない待ちかを、接続口（mcp_proto._waiting_text）が見分けるのに使う"""
    text = (f"# 待ち: {a.title}\n\n- kind: `wait`\n\n{message}\n\n"
            "ここで止まって、利用者にこの内容を短く伝える。next_task を繰り返し呼ばない（待つ間に見に来ない）。\n\n---\n" + REPEAT_RULE)
    a.log(event="issue", task_id=f"{a.id}/wait", kind="wait", chars=len(text))
    return {"text": text, "task_id": None, "kind": "wait", "analysis_id": a.id, "progress": "取得の段", "state": state}


# ---------------------------------------------------------------------------
# 知識ベースの取り込み（kb_update.py）。取得アプリの形だけ。分析の仕事の合間にはさむ
# ---------------------------------------------------------------------------
def _kb():
    if LOCAL is None:
        return None
    try:
        import kb_update
        return kb_update if kb_update.ready() else None
    except Exception:
        return None


def _kb_ok_now(a) -> bool:
    """この分析の今の段で、知識ベースの仕事をはさんでよいか。
    取得中（利用者は「まだ」を聞きたい）と、界隈の確認の前（利用者が答えを待っている）にははさまない"""
    if _acq_view(a) or _deepen_view(a):
        return False
    st = _load_state(a)
    return not any(t["kind"] == "ask_user" and t["status"] != "done" for t in st["tasks"])


def _kb_task_text(kb, t: dict) -> dict:
    text = kb.render(t)
    text += (f"\n## 提出\n`submit(task_id=\"{t['task_id']}\", output=<上の「出力の形」のテキスト>)`\n\n---\n" + REPEAT_RULE)
    return {"text": text, "task_id": t["task_id"], "kind": "ai", "analysis_id": None, "progress": "知識ベース"}


SKIP_CONFIRM_ANSWER = "（界隈の確認を省く: 利用者の頼み）"


def _options(d: Path) -> dict:
    """分析ごとの、会話で頼まれた選択（界隈の確認を省く など）。analysis.json は取得の係も書くので別のファイルに持つ"""
    return _read_json(d / "state" / "options.json", {}) or {}


def _set_options(d: Path, **kv) -> None:
    _write_json(d / "state" / "options.json", {**_options(d), **kv})


def _auto_confirm(a: Analysis, st: dict, t: dict):
    """利用者が「界隈の確認はいらない」と頼んだ分析では、界隈の確認（ask_user）で止まらず、案のまま受け取って先へ進める。
    使った界隈は完了の知らせで伝える（2026-10-06 ユーザー「界隈の確認はいらないのでそのまま最後まで書いて、のオプションも欲しい」）"""
    import flow_w1
    errs = flow_w1.accept(a, t, json.dumps({"user_answer": SKIP_CONFIRM_ANSWER}, ensure_ascii=False), st)
    if errs:   # 案のままでは通らない（まれ）。ふだんどおり利用者に見せる
        a.log(event="auto_confirm_failed", task_id=t["task_id"], reasons=errs[:5])
        return t
    c = _read_json(a.outputs("confirm_answer.json"), {}) or {}
    _write_json(a.outputs("confirm_answer.json"), {**c, "auto": True})
    now = _now()
    t["status"] = "done"
    t["done_at"] = now
    t["first_issued_at"] = t["first_issued_at"] or now
    _write_json(a.state_path, st)
    a.log(event="auto_confirm", task_id=t["task_id"])
    return _run_services(a, st)


def next_task(user_id: str, ref: str | None = None, skip_confirm: bool = False) -> dict:
    _await_deepen(user_id, ref)
    with _lock:
        kb = _kb()
        kt = kb.next_task() if kb else None
        if kt:
            try:
                a0 = resolve(user_id, ref)
            except RunnerError:
                a0 = None   # 分析がまだ無くても、知識ベースの仕事はできる
            if a0 is None or _kb_ok_now(a0):
                return _kb_task_text(kb, kt)
        a = resolve(user_id, ref)
        if skip_confirm and not _options(a.dir).get("skip_confirm"):
            _set_options(a.dir, skip_confirm=True, skip_confirm_at=_now())
        av = _acq_view(a)
        if av:
            if av["state"] == "cancelled":   # やめた取得は終わらないので「終わったら続けて」とは言わない。やり直し方を書く
                tail = (f"\nやり直すなら、利用者が「UGC Analyzer で{a.title}を分析して」と頼めば、楽曲ページ探しから最初にやり直す"
                        f"（使いたい楽曲ページの URL があれば、それを渡して「{a.title}の取得を、このページでやり直して」）。")
            elif av.get("dead_end"):   # 続きから取り直しても同じ（日本の地域で使えない楽曲ページ）。「終わったら続けて」とは言わない
                tail = ""
            else:
                tail = "\n取得が終わったら、利用者が「" + a.title + "の分析を続けて」と言えば再開する。"
            return _wait_task(a, av["message"] + tail, av["state"])
        dv = _deepen_view(a)
        if dv:
            return _deepen_wait_task(a, dv)
        st = _load_state(a)
        t = _run_services(a, st) if is_w1(a) else _current(st)
        if is_w1(a):
            fresh = _recut_queued_now(a)
            if fresh:
                return fresh
        if is_w1(a) and t is not None and t["type"] == "confirm" and _options(a.dir).get("skip_confirm"):
            t = _auto_confirm(a, st, t)
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
        if (task_id or "").startswith("kb/"):
            kb = _kb()
            if kb is None:
                raise RunnerError("知識ベースの仕事は、このサービスにはありません")
            raw = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            try:
                errs = kb.accept(task_id, raw)
            except kb.KbError as e:
                raise RunnerError(str(e)) from e
            if errs:
                return {"ok": False, "reasons": errs, "progress": "知識ベース",
                        "text": "差し戻しです。次の理由を直して、同じ task_id で submit し直してください。\n"
                                + "\n".join(f"- {e}" for e in errs)}
            return {"ok": True, "progress": "知識ベース",
                    "text": f"受け取りました（知識ベースの残り {kb.pending_count()} 件）。続けて next_task を呼んでください。"}
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
        cur = _current(st)
        if cur is not None and cur["kind"] != "done":
            raise RunnerError("レポートがまだできていません。完成してから直しを頼んでください")
        import flow_w1
        for t in st["tasks"]:   # 前の完了の知らせを AI が受け取らないまま会話が終わっていたら、済んだことにする（deepen・recut と同じ）
            if t["kind"] == "done" and t["status"] != "done":
                t.update({"status": "done", "done_at": _now()})
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


# ---------------------------------------------------------------------------
# 完成後の界隈の掘り下げ（2026-10-06〜。docs/DEEPEN_COMMUNITY.md）
# ---------------------------------------------------------------------------
# 取り足しの取得を、依頼した会話の中で待つか（Q4）。2026-10-06 ユーザーの判断で False（A: 止まって、取り足しが終わったら利用者が「続けて」）。
# True（B）にすると50秒ずつ next_task を繰り返して待ち、一気に書き直しまで進む（1回の返答で呼べる回数は本番の記録で163回・165回まで続いたので
# 回数は足りる）。ただ待ちの約30回ごとに会話全体を読み直すので、長い会話ほど利用枠を使う（docs/DEEPEN_COMMUNITY.md 3 章）。
# True にするときは、mcp_proto.py の next_task の説明と codex_link.py のスキルに「指示書に『この会話で待つ』とあれば、すぐにもう一度 next_task」を戻す
DEEPEN_WAIT_IN_CHAT = False
DEEPEN_WAIT_S = 50      # B のとき、next_task が1回で待つ長さ（Claude デスクトップは道具1回が約60秒で打ち切り）
DEEPEN_TASKS = ("dcomments", "outline_revise", "review")


def _match_community(tax: dict, text: str) -> str:
    """界隈の key か、レポートでの呼び名（「屋外ダンス界隈」など）から key を当てる。当たらなければ一覧を返す"""
    comm = {k: v for k, v in tax["community"].items() if not k.startswith("_") and k != "unknown"}
    q = (text or "").strip().strip("「」`'\"")
    if q in comm:
        return q
    for k in comm:
        if k.lower() == q.lower():
            return k
    qn = re.sub(r"(の人たち|の人|界隈|勢|層)$", "", q).strip()
    hits = [k for k, v in comm.items() if qn and (qn.lower() in k.lower() or qn in str(v))] if len(qn) >= 2 else []
    if len(hits) == 1:
        return hits[0]
    listing = "\n".join(f"- `{k}`: {_first_sentence(str(v))}" for k, v in comm.items())
    raise RunnerError(f"「{q}」に当たる界隈が{'複数あります' if hits else '見つかりません'}。レポートの本文の呼び名と下の説明を見比べ、"
                      f"key を community に入れて deepen を呼び直してください（利用者には聞かない）:\n{listing}")


def deepen(user_id: str, ref: str | None, community: str, instruction: str) -> dict:
    """完成したレポートの、ある界隈の掘り下げ。取る動画を決めて取得の待ち行列に入れ、AI の仕事を末尾に足す"""
    instruction = (instruction or "").strip() or "この界隈をもっと掘り下げて"
    if len(instruction) > 2000:
        raise RunnerError("頼みの言葉は2000字までにしてください")
    with _lock:
        a = resolve(user_id, ref)
        if not is_w1(a):
            raise RunnerError("この分析（試作）は掘り下げに対応していません")
        st = _load_state(a)
        cur = _current(st)
        if cur is not None and cur["kind"] != "done":
            if _recut_pending(st) or _recut_rest(st, cur):
                raise RunnerError(f"いま界隈の切り直しの途中です。終わってから頼んでください（「{a.title}の分析を続けて」で進みます）")
            if any(t["type"] in DEEPEN_TASKS and t["status"] != "done" for t in st["tasks"]):
                raise RunnerError(f"いま界隈「{(a.meta.get('deepen') or {}).get('community')}」の掘り下げの途中です。"
                                  f"終わってから頼んでください（「{a.title}の分析を続けて」で進みます）")
            raise RunnerError("レポートがまだできていません。完成してから頼んでください")
        tax = _taxonomy(a)
        k = _match_community(tax, community)
        from acquire import deepen as dp
        import flow_w1
        pl = dp.plan(a.dir, k)
        job = dp.request(a.dir, k, instruction, pl)
        for t in st["tasks"]:   # 前の完了の知らせを AI が受け取らないまま会話が終わっていたら、済んだことにする
            if t["kind"] == "done" and t["status"] != "done":
                t.update({"status": "done", "done_at": _now()})
        st["tasks"] += flow_w1.deepen_tasks(st, job)
        _write_json(a.state_path, st)
        a.log(event="deepen_requested", community=k, round=job["round"], n_new=pl["n_new"], n_more=pl["n_more"],
              est_min=pl["est_min"], chars=len(instruction))
    name = _first_sentence(str(tax["community"].get(k, k)), 40)
    head = f"「{a.title}」の界隈 `{k}`（{name}）を掘り下げます。"
    if job["status"] != "queued":
        return {"text": head + "取り足せる動画（コメントの無い動画・続きのある動画）が無いので、手元のコメントを前回より深く読み直して直します。"
                       "利用者にそう短く伝え、続けて next_task を呼んで、仕事を片付けてください。", "analysis_id": a.id}
    if LOCAL is not None:
        LOCAL.ensure_app()
    else:
        from acquire import launch
        launch.ensure_worker()
    eta = pl["est_min"] * 60
    head += f"コメントを取り足します: 新しく{pl['n_new']}本・続きを{pl['n_more']}本。"
    if pl["left_new"] or pl["left_more"]:
        head += f"時間の都合で取らない動画もあります（新しく{pl['left_new']}本・続き{pl['left_more']}本）。"
    if DEEPEN_WAIT_IN_CHAT:
        tail = ("利用者にこの内容を短く伝えてから、続けて next_task を呼ぶ（この会話で取得の終わりを待つ。next_task は最大"
                f"{DEEPEN_WAIT_S}秒待って様子を返す）。会話が途中で止まっても、取得が終わると Mac の通知が出るので、"
                f"利用者が「{a.title}の分析を続けて」と言えば再開できる。")
    else:
        back = come_back_line(eta, "この界隈のコメントを取り足すの", a.title, "取り足した分でレポートを書き直します。")
        tail = ("（AI へ: この内容を利用者に短く伝えて止まる。取得を待たない・見に来ない。返事の最後に、下の「」の文を言い換えずにそのまま入れる。"
                "「続けて」と言われたら next_task で、その界隈の分析のやり直し・構成案と章の書き直し・全章の通し読みを片付ける）\n"
                f"「{back}」")
    return {"text": head + "\n" + tail, "analysis_id": a.id}


def _deepen_view(a: Analysis):
    """掘り下げの取り足しの様子。取得中でなければ None（止まった・済んだ・無い → AI の仕事へ進む）"""
    dp = a.meta.get("deepen") or {}
    if dp.get("status") not in ("queued", "running"):
        return None
    n = dp.get("round")
    done = 0
    p = a.dir / "raw" / "deepen" / f"r{n}.jsonl"
    if p.exists():
        seen = set()
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["video_id"])
                except (ValueError, KeyError, TypeError):   # 取得の途中でアプリが落ち、最後の行が途中で切れている（係が続きから取り直す）
                    pass
        done = len(seen)
    total = len(dp.get("targets") or [])
    est = float(dp.get("est_min") or 25) * 60
    if dp.get("status") == "running" and dp.get("started_at"):
        try:
            el = (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(dp["started_at"])).total_seconds()
            est = max(60.0, est - el)
        except ValueError:
            pass
    back = f"終わったら「{a.title}の分析を続けて」と言ってください。"
    what = "切り直した界隈" if dp.get("kind") == "recut" else f"界隈 `{dp.get('community')}` "
    if LOCAL is not None:
        app = LOCAL.app_state() or {}
        if not app.get("running"):
            return {"state": "stopped", "eta_seconds": est,
                    "message": "あなたの Mac の UGC Analyzer が動いていません。アプリケーションフォルダの「UGC Analyzer」を開けば、取り足しを始めます。"}
        if app.get("login_wanted"):
            return {"state": "login", "eta_seconds": est,
                    "message": "UGC Analyzer が TikTok のログインを待っています。UGC Analyzer が開いた Chrome で、分析専用のサブアカウントでログインしてください。"}
    if dp.get("status") == "queued":
        busy = []
        for d in ANALYSES_DIR.iterdir():
            if not d.is_dir():   # .DS_Store など
                continue
            try:
                m = _read_json(d / "analysis.json")
            except (OSError, ValueError):   # 読めない目録（壊れた写し）は飛ばす（list_analyses と同じ）
                continue
            if isinstance(m, dict) and (m.get("acquisition") or {}).get("status") == "running":
                busy.append(m)
        if busy:   # 半日の取得の後ろ。会話の中では待たない
            return {"state": "behind", "eta_seconds": None,
                    "message": f"{what}のコメントの取り足しは、いま取得中の「{busy[0].get('title')}」が"
                               f"終わってから始まります（取り足しは約{int(round(float(dp.get('est_min') or 25)))}分）。" + back}
        msg = f"{what}のコメントの取り足しの開始待ち（約{_hm(est)}）。"
    else:
        msg = (f"あなたの Mac で{what}のコメントを取り足し中（{done}/{total}本）。"
               f"終わるのは{_eta_clock(est)}の見込み（あと約{_hm(est)}）。")
    return {"state": "deepening", "message": msg + back, "eta_seconds": est}


def _await_deepen(user_id: str, ref: str | None) -> None:
    """B（会話の中で待つ）のとき: 取り足しが終わるまで最大 DEEPEN_WAIT_S 秒待つ（ロックの外で。ほかの道具を止めない）"""
    if not DEEPEN_WAIT_IN_CHAT:
        return
    t0 = time.time()
    while time.time() - t0 < DEEPEN_WAIT_S:
        try:
            a = resolve(user_id, ref)
        except RunnerError:
            return
        dv = _deepen_view(a)
        if dv is None or dv["state"] != "deepening":
            return
        time.sleep(5)


def _deepen_wait_task(a: Analysis, dv: dict) -> dict:
    if DEEPEN_WAIT_IN_CHAT and dv["state"] == "deepening":
        how = ("この会話で待つ: 利用者に何も聞かず、すぐにもう一度 next_task を呼ぶ（取り足しが終わると、そのまま次の仕事が渡される）。"
               "途中経過を毎回書かない。")
    else:
        how = "ここで止まって、利用者にこの内容を短く伝える。next_task を繰り返し呼ばない（待つ間に見に来ない）。"
    what = "界隈の切り直し" if (a.meta.get("deepen") or {}).get("kind") == "recut" else "界隈の掘り下げ"
    text = f"# 待ち: {a.title}（{what}）\n\n- kind: `wait`\n\n{dv['message']}\n\n{how}\n\n---\n" + REPEAT_RULE
    a.log(event="issue", task_id=f"{a.id}/deepen-wait", kind="wait", chars=len(text))
    return {"text": text, "task_id": None, "kind": "wait", "analysis_id": a.id, "progress": _acq_label(a), "state": dv["state"]}


def _acq_label(a: Analysis) -> str:
    """完成後の取り足しの進み具合の名前（掘り下げか、切り直しか）"""
    return "界隈の切り直しの取り足し" if (a.meta.get("deepen") or {}).get("kind") == "recut" else "界隈の掘り下げの取り足し"


# ---------------------------------------------------------------------------
# 完成後の界隈の切り直し（2026-10-06〜。docs/RECUT_COMMUNITY.md）
# ---------------------------------------------------------------------------
# 道具 recut → 仕事 recut（AI: 分類軸を直す）→ ラベルの付け直し → recut_check（サービス: 新しい界隈の必ず読みたい動画にコメントがあるか）
# → 足りていれば止まらずに段階・コメント分析・構成案・執筆・仕上げまで（着席1回）。足りなければ Mac が取り足すので、
# その場で止まり、何分後に戻って「続けて」と頼むかを伝える（着席2回。2026-10-06 ユーザー「追加プロンプトが必要な場合は、最初にユーザに通知」）
RECUT_TASKS = ("recut", "recut_check")


def _recut_pending(st: dict) -> bool:
    """切り直しの仕事（分類軸の直し・ラベルの付け直し・確かめ）が残っているか"""
    return any(t["status"] != "done" and (t["type"] in RECUT_TASKS or (t["type"] == "label" and t["params"].get("recut")))
               for t in st["tasks"])


def _recut_rest(st: dict, cur: dict) -> bool:
    """今の仕事が、切り直しのあとに続く仕事（段階の区切りから新しい版の仕上げまで）か。
    今の仕事より前にある最後の頼み（切り直し recut・掘り下げ dcomments・直し revise）が切り直しなら、その続き
    （2026-10-06 通し試験: 取り足しが済んで「続けて」の前に掘り下げ・切り直しを頼むと「レポートがまだできていません」だけ返っていた）"""
    i = st["tasks"].index(cur)
    asks = [t["type"] for t in st["tasks"][:i] if t["type"] in ("recut", "dcomments", "revise")]
    return bool(asks) and asks[-1] == "recut"


def recut(user_id: str, ref: str | None, instruction: str) -> dict:
    """完成したレポートの界隈を、利用者の指示で切り直す。仕事 recut を末尾に足す（ラベルの付け直しから先は、受け付けたときに足す）"""
    instruction = (instruction or "").strip()
    if not instruction:
        raise RunnerError("界隈をどう切り直したいかを、利用者の言葉で instruction に入れてください")
    if len(instruction) > 2000:
        raise RunnerError("頼みの言葉は2000字までにしてください")
    with _lock:
        a = resolve(user_id, ref)
        if not is_w1(a):
            raise RunnerError("この分析（試作）は界隈の切り直しに対応していません")
        acq = a.meta.get("acquisition")
        if acq and acq.get("status") != "done":
            raise RunnerError("コメントの取得が終わっていません。取得とレポートが済んでから頼んでください")
        if _deepen_view(a):
            raise RunnerError(f"いまコメントの取り足しの途中です。終わってから頼んでください（「{a.title}の分析を続けて」で進みます）")
        st = _load_state(a)
        cur = _current(st)
        if cur is not None and cur["kind"] != "done":
            if _recut_pending(st) or _recut_rest(st, cur):
                raise RunnerError(f"いま界隈の切り直しの途中です。「{a.title}の分析を続けて」で進みます")
            if any(t["type"] in DEEPEN_TASKS and t["status"] != "done" for t in st["tasks"]):
                raise RunnerError(f"いま界隈の掘り下げの途中です。終わってから頼んでください（「{a.title}の分析を続けて」で進みます）")
            raise RunnerError("レポートがまだできていません。完成してから頼んでください")
        import flow_w1
        for t in st["tasks"]:   # 前の完了の知らせを AI が受け取らないまま会話が終わっていたら、済んだことにする
            if t["kind"] == "done" and t["status"] != "done":
                t.update({"status": "done", "done_at": _now()})
        n = flow_w1.recut_last_round(a) + 1
        st["tasks"].append(flow_w1._task(st, "recut", "ai", "界隈の切り直し（利用者の指示）", {"instruction": instruction, "round": n}))
        _write_json(a.state_path, st)
        a.log(event="recut_requested", round=n, chars=len(instruction))
    return {"text": f"「{a.title}」の界隈を、利用者の指示どおりに切り直します。続けて next_task を呼び、界隈の切り直し → 動画のラベルの付け直しを片付ける。\n"
                    "（AI へ: 最初に利用者へ「界隈を切り直して、動画のラベルを付け直します。新しい界隈でコメントが足りないときは、"
                    "付け直したところで一度止まり、何分後に戻ればよいかをお伝えします。足りていれば、そのままレポートの新しい版まで進めます。」"
                    "とだけ伝えてから進める。利用者に確認は取らない）", "analysis_id": a.id}


def _recut_queued_now(a: Analysis):
    """切り直しの確かめ（サービス）がいま取り足しを積んだなら、Mac に取らせて、最初の返事（何分後に戻るか）を返す。積んでいなければ None"""
    m = _read_json(a.dir / "analysis.json") or a.meta
    dp = m.get("deepen") or {}
    if dp.get("kind") != "recut" or dp.get("status") != "queued" or dp.get("announced"):
        return None
    a.meta = m
    if LOCAL is not None:
        LOCAL.ensure_app()
    else:
        from acquire import launch
        launch.ensure_worker()
    dv = _deepen_view(a) or {}
    import flow_w1
    n = dp.get("recut_round")
    rec = flow_w1.recut_record(a, n)
    eta = dv.get("eta_seconds") if dv.get("state") == "deepening" else None
    back = come_back_line(eta, "切り直した界隈のコメントを集めるの", a.title, "新しい界隈の分け方でレポートを書き直します。")
    head = (f"「{a.title}」の界隈を切り直し、動画のラベルを付け直しました（{rec.get('note_to_user', '')}）。"
            f"新しい界隈のうち、読むべき動画にコメントが無いもの（{flow_w1.recut_short_targets(a, n)}）を、Mac が取り足します（{len(dp.get('targets') or [])}本）。")
    if dv.get("state") in ("stopped", "login", "behind"):
        head += dv["message"]
    text = (f"# 待ち: {a.title}（界隈の切り直し）\n\n- kind: `wait`\n\n{head}\n\n"
            "（AI へ: ここで止まる。利用者に、どう切り直したかを1〜2文で伝え、返事の最後に下の「」の文を言い換えずにそのまま入れる。"
            "取得を待たない・next_task を繰り返し呼ばない）\n"
            f"「{back}」\n\n---\n" + REPEAT_RULE)
    dp["announced"] = _now()
    _write_json(a.dir / "analysis.json", m)
    a.log(event="issue", task_id=f"{a.id}/recut-wait", kind="wait", chars=len(text), targets=len(dp.get("targets") or []))
    return {"text": text, "task_id": None, "kind": "wait", "analysis_id": a.id, "progress": "界隈の切り直しの取り足し",
            "state": dv.get("state")}


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


def prompts(user_id: str, action: str = "list", name: str | None = None, text: str | None = None) -> dict:
    """指示書そのものを見る・書き換える・初期に戻す（取得アプリの形だけ。prompt_store.py）"""
    import prompt_store as ps
    if ps.user_root() is None:
        raise RunnerError("指示書の編集は、このサービスでは使えません")
    try:
        if action == "get":
            if not name:
                raise RunnerError("見る指示書の名前（name）を指定してください。一覧は action=list")
            body = ps.get(name)
            return {"text": f"指示書「{ps.TITLES.get(ps._check_name(name), name)}」（{ps._check_name(name)}）の今の全文:\n\n"
                            f"````markdown\n{body}````\n\n直すときは、この全文を直したものを action=set の text で渡す。"
                            "{{…}} の印と「### 出力の形」の見出しは残す（出力の形の節は直しても使われない）"}
        if action == "set":
            if not name or not text:
                raise RunnerError("書き換える指示書の名前（name）と、直した全文（text）を渡してください")
            errs = ps.put(name, text)
            if errs:
                return {"text": "書き換えませんでした。次を直して、もう一度 set してください:\n" + "\n".join(f"- {e}" for e in errs)}
            return {"text": f"指示書「{ps.TITLES[ps._check_name(name)]}」を書き換えました。次に渡す仕事から使います。"
                            "初期に戻すときは action=reset。"}
        if action == "reset":
            done = ps.reset(name or None)
            return {"text": "初期の指示書に戻しました: " + "、".join(ps.TITLES[n] for n in done)}
        rows = ps.status()
        lines = [f"- `{r['name']}` {r['title']}" + ("（編集済み）" if r["edited"] else "")
                 + ("" if r["ok"] else f" ★使えない（{'; '.join(r['problems'])}）→ 初期の指示書を使っている") for r in rows]
        return {"text": "指示書の一覧（name で指定する）:\n" + "\n".join(lines)
                        + "\n\n利用者が「〇〇の指示書をこう変えて」と頼んだら、action=get で全文を読み、直した全文を action=set で渡す。"}
    except ps.PromptError as e:
        raise RunnerError(str(e)) from e


def _stoppable(user_id: str, restart: bool = False) -> list:
    """曲名も分析 ID も無しで「取得をやめて」「やり直して」と頼まれたときの候補 [(分析, 様子)]（古い順）。
    やめる: 取得中・順番待ちと、完成後の取り足し中・その順番待ち。やり直す: 取得中・順番待ちと、止まった分析（同じ曲を頼み直したものは除く）"""
    from acquire import pipeline
    mine = list_analyses(user_id)
    out = []
    for a in mine:
        acq = a.meta.get("acquisition") or {}
        st, dp = acq.get("status"), (a.meta.get("deepen") or {}).get("status")
        if st == "running":
            step = pipeline.STEP_LABELS.get(acq.get("step") or "")
            out.append((a, f"取得中・{step}" if step else "取得中"))
        elif st == "queued":
            out.append((a, "順番待ち"))
        elif restart and st == "failed" and not _superseded(a, mine):
            out.append((a, "止まっている"))
        elif not restart and dp in ("queued", "running"):
            out.append((a, "取り足しの順番待ち" if dp == "queued" else "取り足し中"))
    return sorted(out, key=lambda x: x[0].meta.get("created_at", ""))


def _ask_which(cands: list, restart: bool = False) -> dict:
    """候補が2つ以上: どれもやめずに一覧を返し、どれのことか AI に利用者へ1回聞かせる
    （2026-10-06 ユーザーの決定。前は新しいほうを黙ってやめていた）"""
    kinds = "取得中・順番待ち・止まった" if any(label == "止まっている" for _, label in cands) else "取得中・順番待ちの"
    which = "どちら" if len(cands) == 2 else "どれ"
    listing = "、".join(f"{a.title}（{label}。分析 ID: {a.id}）" for a, label in cands)
    tool, verb = ("restart_analysis", "やり直す") if restart else ("cancel_analysis", "やめる")
    text = (f"{kinds}分析が{len(cands)}つあります: {listing}。まだどれもやめていません。\n"
            f"（AI へ: {which}を{verb}か利用者に1回聞き、答えの曲名か分析 ID で {tool} を呼び直す"
            + ("。music_url・music_urls などの指定は今回と同じものを渡す" if restart else "") + "）")
    return {"text": text, "analysis_id": None, "candidates": [a.id for a, _ in cands]}


def cancel_analysis(user_id: str, ref: str | None) -> dict:
    """取得をやめる（取得アプリの形。2026-10-04 ユーザー「取得を止めて、初めからもう一度やりたい」）。
    分析を言わずに頼まれ、やめられる分析が2つ以上あれば、やめずに一覧を返す"""
    if LOCAL is None:
        raise RunnerError("取得をやめるのは、このサービスでは使えません（運営に連絡してください）")
    with _lock:
        if not (ref or "").strip():
            cands = _stoppable(user_id)
            if len(cands) >= 2:
                return _ask_which(cands)
        a = resolve(user_id, ref)
        acq = a.meta.get("acquisition") or {}
        if acq.get("status") == "done":
            raise RunnerError(f"「{a.title}」の取得はもう終わっています")
        if acq.get("status") == "cancelled":
            return {"text": f"「{a.title}」の取得はもうやめてあります。", "analysis_id": a.id}
        m = _read_json(a.dir / "analysis.json")
        m["acquisition"].update({"status": "cancelled", "cancelled_at": _now(), "cancel_reason": "利用者がやめた"})
        _write_json(a.dir / "analysis.json", m)
        LOCAL.request_stop(a.id)
    return {"text": f"「{a.title}」の取得をやめました（UGC Analyzer が数秒で止めます）。"
                    f"もう一度「UGC Analyzer で{a.title}を分析して」と頼めば、楽曲ページ探しから最初にやり直します。", "analysis_id": a.id}


def restart_analysis(user_id: str, ref: str | None, music_url: str = "", music_urls: list | None = None,
                     replies: bool | None = None, min_plays: int | None = None) -> dict:
    """取得をやめて、やり直す（取得アプリの形）。music_url・music_urls があればそのページ（複数なら全部合わせて）で、
    無ければ楽曲ページ探しから最初に
    （2026-10-04 ユーザー「〇〇の取得をやめて、このページでやり直して」「止めて初めからもう一度やって、正しい楽曲ページを抽出できるか試したい」）。
    前の分析は「やめた」にして、取得アプリが係を止める（印のファイル）。
    分析を言わずに頼まれ、やり直せる分析が2つ以上あれば、やめずに一覧を返す"""
    if LOCAL is None:
        raise RunnerError("取得のやり直しは、このサービスでは使えません（運営に連絡してください）")
    urls = _clean_urls([music_url] + list(music_urls or []))
    bad = [u for u in [music_url] + list(music_urls or [])
           if (u or "").strip() and not MUSIC_URL_RE.match((u or "").strip().split("?")[0])]
    if bad:
        raise RunnerError("楽曲ページの URL は https://www.tiktok.com/music/曲名-数字 の形です: " + ", ".join(bad))
    with _lock:
        if not (ref or "").strip():
            cands = _stoppable(user_id, restart=True)
            if len(cands) >= 2:
                return _ask_which(cands, restart=True)
        a = resolve(user_id, ref)
        acq = a.meta.get("acquisition") or {}
        if acq.get("status") == "done":
            raise RunnerError(f"「{a.title}」の取得はもう終わっています。別の楽曲ページで取り直すなら、新しく分析を頼んでください")
        # すでにやめた分析（「〇〇の取得をやめて」のあとの「このページでやり直して」）は、やめるのを飛ばして新しく始める
        was_cancelled = acq.get("status") == "cancelled"
        old_urls = [u for u in (a.meta.get("music_urls") or [a.meta.get("music_url")]) if u]
        old = "・".join(old_urls) or "楽曲ページ不明"
        m = _read_json(a.dir / "analysis.json")
        song = (m.get("song") or {}).get("title") or a.title
        artist = (m.get("song") or {}).get("artist") or ""
        rep = wants_replies(m) if replies is None else bool(replies)   # 省けば前の分析の指定を引き継ぐ
        mp = min_plays_of(m) if min_plays is None else int(min_plays)
        skip = bool(_options(a.dir).get("skip_confirm"))   # 界隈の確認を省く頼みも引き継ぐ

    def cancel_old():
        """前の取得を「やめた」にして、取得アプリに係を止めさせる。新しいページを読んで選び終えてから呼ぶ
        （先にやめると、ページを読む所で失敗したとき前の取得も新しい取得も無くなる。2026-10-06 通し試験）"""
        with _lock:
            m2 = _read_json(a.dir / "analysis.json")
            st2 = (m2.get("acquisition") or {}).get("status")
            if st2 == "cancelled":
                return
            if st2 == "done":   # ページを読んでいる間に取得が終わった
                raise RunnerError(f"「{a.title}」の取得はもう終わっています。別の楽曲ページで取り直すなら、新しく分析を頼んでください")
            m2["acquisition"].update({"status": "cancelled", "cancelled_at": _now(),
                                      "cancel_reason": f"利用者が別の楽曲ページでやり直した（{'・'.join(urls) or '楽曲ページ探しから'}）"})
            _write_json(a.dir / "analysis.json", m2)
            LOCAL.request_stop(a.id)   # 取得アプリが、この分析を取っている係を止める

    did = "はもうやめてあります" if was_cancelled else "をやめました"
    if not urls:   # 最初から: 楽曲ページを探し直す（AI が検索して start_analysis を呼ぶ）
        cancel_old()
        res = _music_search_hint(song, artist, f"「{a.title}」の前の取得（{old}）{'はもうやめてある' if was_cancelled else 'をやめた'}。"
                                               "楽曲ページ探しから最初にやり直す。")
        if rep:
            res["text"] += "\n（AI へ: この分析は返信も取る指定。start_analysis を呼ぶときは replies=true を付ける）"
        if mp is not None:
            res["text"] += f"\n（AI へ: この分析は再生の下限の指定あり。start_analysis を呼ぶときは min_plays={mp} を付ける）"
        if skip:
            res["text"] += "\n（AI へ: この分析は界隈の確認を省く指定。start_analysis を呼ぶときは skip_confirm=true を付ける）"
        res["cancelled"] = a.id
        return res
    res = start_analysis(user_id, song, artist, music_urls=urls, replies=rep, min_plays=mp, skip_confirm=skip,
                         before_create=cancel_old)   # やり直しは利用者が選んだページで（複数なら全部）
    if not res.get("analysis_id"):
        # 渡されたページが全部、日本の地域で使えなかった（分析は作っていない。前の取得はやめる前なので、そのまま）。
        # 呼び直すのは start_analysis でなく restart_analysis（同じ曲の取得が残っていると、start_analysis は受け付け済みを返すため）
        body = _unavailable_hint(song, artist, res["unavailable"])["text"] if res.get("unavailable") else res["text"]
        res["text"] = (f"「{a.title}」の前の取得（{old}）は{'もうやめてあります' if was_cancelled else 'まだやめていません'}。\n"
                       + body.replace("music_urls に入れて start_analysis を呼び直す",
                                      f"music_urls に入れて restart_analysis（analysis_id={a.id}）を呼び直す")
                       + "\n（AI へ: start_analysis は呼ばない。" + ("" if was_cancelled else
                                                                    "前の取得は、ほかの楽曲ページで restart_analysis を呼んだときにやめる。")
                       + "返信・再生の下限・界隈の確認の指定は引き継ぐので付けなくてよい）")
        return res
    res["text"] = (f"「{a.title}」の前の取得（{old}）{did}。\n" + res["text"])
    res["cancelled"] = a.id
    return res


def update_knowledge(user_id: str) -> dict:
    """著者の note の新着を見て、知識ベースの取り込みの仕事を用意する（取得アプリの形だけ）"""
    kb = _kb()
    if kb is None:
        raise RunnerError("知識ベースの更新は、このサービスでは使えません")
    try:
        res = kb.check_new(log=lambda m: None)
    except Exception as e:
        raise RunnerError(f"著者の note を確かめられませんでした（{type(e).__name__}）。ネットにつながっているか確かめて、少し待ってからもう一度") from e
    n = kb.pending_count()
    head = f"著者の note を確かめました。新しい記事 {len(res['new'])} 本。"
    if n:
        head += (f"取り込みの仕事が {n} 件あります。続けて next_task を呼んで片付ける（1件1〜2分。分析の仕事の前にはさまる）。"
                 "利用者には「知識ベースを新しくしています」と短く伝える。")
    else:
        head += "取り込む記事はありません。"
    return {"text": head + "\n" + kb.summary()}


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
