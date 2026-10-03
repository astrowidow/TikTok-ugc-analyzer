"""
指示書の置き場（利用者が編集できる指示書）。docs/ALL_IN_APP_PLAN.md 段3、2026-10-03 ユーザー「プロンプトそのものをアプリの設定から変えられた方が良い。
AI にたのんで変える道も用意すべき。両方残すのが正解」。

- 初期の指示書: リポジトリの service_prompts/<組>/（アプリでは同梱）
- 利用者の指示書: 環境変数 UGC_PROMPTS_DIR/<組>/（アプリでは ~/Library/Application Support/UGC Collector/prompts/）。
  無ければ初期の指示書だけを使う（Windows 機のサービスは今までどおり）
- 読むとき（load）: 利用者の指示書が「使える」ならそれ、だめなら初期の指示書。
  使える = 初期の指示書の差し込みの印（{{名前}}）が全部残っていること
- 「### 出力の形」の節は、読むときに必ず初期の指示書のものに差し替える（サービスの検査がこの形を前提にしているため。
  利用者がここを直しても使われない。アプリを新しくして形が変わっても、編集した指示書がそのまま使える）
- seed(): 初期の指示書を利用者の置き場に写す。写したときの中身の印（.defaults.json）を残し、
  アプリを新しくしたとき、編集していない指示書だけを新しい初期の指示書に入れ替える（編集したものは残す）
"""
import hashlib
import json
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = BASE_DIR / "service_prompts"
MANIFEST = ".defaults.json"
GROUP = "w1"

# 利用者に見せる名前（アプリのメニューと AI の道具で使う。並びは仕事の順）
TITLES = {
    "axes.md": "分類軸（界隈など）の案を作る",
    "confirm.md": "界隈の確認（利用者に見せる）",
    "label.md": "ラベル付け",
    "phases.md": "拡散の段階を区切る",
    "comments_community.md": "コメント分析（界隈ごと）",
    "ref_select.md": "参考にする過去記事を選ぶ",
    "ref_digest.md": "参考記事の章立てと論理を取り出す",
    "outline.md": "構成案（主張と根拠の割り振り）",
    "write.md": "レポートの執筆（章ごと）",
    "finish.md": "note 用の仕上げ（章ごと）",
    "finish_title.md": "note の題名と見出し",
    "revise.md": "レポートの直し",
    "done.md": "完了の知らせ",
    # 2026-10-03 より前に始めた分析だけが使う（動画1本ずつのコメント分析 → 界隈ごとのまとめ）
    "comments.md": "（前の形）コメント分析（動画1本ずつ）",
    "synthesis.md": "（前の形）界隈ごとのまとめ",
}

PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")
OUTPUT_HEAD_RE = re.compile(r"(?m)^### 出力の形")

README = """この フォルダの指示書（.md）は、AI に渡す作業の指示です。テキストエディットで開いて直し、保存すれば、次の仕事から使われます。

- {{ と }} で囲んだ印（例: {{song}}）は、取得したデータが差し込まれる場所です。消さないでください。
  消えていると、その指示書は使われず、初期の指示書が使われます（取得アプリが通知で知らせます）
- 「### 出力の形」から下の節は、直しても使われません（AI の答えを決まった形で検査するため、いつも初期のものを使います）
- 元に戻したいときは、取得アプリのメニュー「指示書を編集」→「すべて初期の指示書に戻す」か、Claude に「〇〇の指示書を初期に戻して」と言ってください
- アプリを新しくしたとき、編集していない指示書は新しい初期の指示書に入れ替わります。編集した指示書はそのまま残ります
"""


class PromptError(Exception):
    """利用者に返す、日本語の短いエラー"""


def user_root():
    v = os.environ.get("UGC_PROMPTS_DIR")
    return Path(v) if v else None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name.endswith(".md"):
        name += ".md"
    if not re.match(r"^[a-z_]+\.md$", name) or not (DEFAULT_ROOT / GROUP / name).exists():
        raise PromptError(f"{name} という指示書はありません（{', '.join(TITLES)}）")
    return name


def default_text(name: str, group: str = GROUP) -> str:
    return (DEFAULT_ROOT / group / name).read_text(encoding="utf-8")


def placeholders(text: str) -> set:
    return set(PLACEHOLDER_RE.findall(text))


def _output_section(text: str):
    """「### 出力の形」の節（見出しから、次の ### 見出しの手前か終わりまで）の (開始, 終わり)。無ければ None"""
    m = OUTPUT_HEAD_RE.search(text)
    if not m:
        return None
    nxt = re.search(r"(?m)^### ", text[m.end():])
    return m.start(), (m.end() + nxt.start()) if nxt else len(text)


def problems(name: str, text: str, group: str = GROUP) -> list:
    """利用者の指示書が使えない理由（空なら使える）"""
    default = default_text(name, group)
    errs = []
    if not text.strip():
        return ["中身が空です"]
    missing = sorted(placeholders(default) - placeholders(text))
    if missing:
        errs.append("差し込みの印が消えています: " + "、".join("{{" + m + "}}" for m in missing))
    if _output_section(default) and not _output_section(text):
        errs.append("「### 出力の形」の見出しが消えています")
    return errs


def apply_fixed_parts(name: str, text: str, group: str = GROUP) -> str:
    """「### 出力の形」の節を初期の指示書のものに差し替える"""
    default = default_text(name, group)
    d, u = _output_section(default), _output_section(text)
    if not d or not u:
        return text
    return text[:u[0]] + default[d[0]:d[1]] + text[u[1]:]


def load(name: str, group: str = GROUP) -> str:
    """仕事に使う指示書。利用者の指示書が使えるならそれ（出力の形は初期のもの）、だめなら初期の指示書"""
    root = user_root()
    if root is not None:
        p = root / group / name
        try:
            text = p.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError):
            text = None
        if text is not None and not problems(name, text, group):
            return apply_fixed_parts(name, text, group)
    return default_text(name, group)


# ---------------------------------------------------------------------------
# 利用者の置き場の管理（アプリと AI の道具から）
# ---------------------------------------------------------------------------
def _dir(group: str = GROUP) -> Path:
    root = user_root()
    if root is None:
        raise PromptError("指示書の編集は、このサービスでは使えません")
    return root / group


def _manifest(group: str = GROUP) -> dict:
    try:
        return json.loads((_dir(group) / MANIFEST).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_manifest(data: dict, group: str = GROUP) -> None:
    p = _dir(group) / MANIFEST
    tmp = p.with_name(f"{p.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _write(p: Path, text: str) -> None:
    tmp = p.with_name(f"{p.name}.tmp{os.getpid()}")   # Claude は道具のプロセスを同時に2つ起こすことがある
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def seed(group: str = GROUP) -> dict:
    """初期の指示書を写す。編集していない指示書は新しい初期の指示書に入れ替える。何をしたかを返す"""
    d = _dir(group)
    d.mkdir(parents=True, exist_ok=True)
    man = _manifest(group)
    done = {"copied": [], "updated": [], "kept_edited": []}
    for name in TITLES:
        default = default_text(name, group)
        p = d / name
        cur = p.read_text(encoding="utf-8") if p.exists() else None
        if cur is None:
            _write(p, default)
            done["copied"].append(name)
        elif _sha(default) == man.get(name):
            continue
        elif _sha(cur) == man.get(name) or cur == default:
            _write(p, default)   # 編集していない（前の初期のまま）→ 新しい初期に入れ替える
            done["updated"].append(name)
        else:
            done["kept_edited"].append(name)
            continue
        man[name] = _sha(default)
    _save_manifest(man, group)
    (d.parent / "はじめに読んでください.txt").write_text(README, encoding="utf-8")
    return done


def status(group: str = GROUP) -> list:
    """指示書の一覧: 名前・見出し・編集したか・使えるか（使えない理由）"""
    d = _dir(group)
    out = []
    for name, title in TITLES.items():
        p = d / name
        try:
            text = p.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError):
            text = None
        default = default_text(name, group)
        edited = text is not None and text != default
        errs = problems(name, text, group) if text is not None else ["ファイルがありません（初期の指示書を使います）"]
        out.append({"name": name, "title": title, "edited": edited, "ok": not errs, "problems": errs,
                    "path": str(p)})
    return out


def get(name: str, group: str = GROUP) -> str:
    name = _check_name(name)
    p = _dir(group) / name
    return p.read_text(encoding="utf-8") if p.exists() else default_text(name, group)


def put(name: str, text: str, group: str = GROUP) -> list:
    """指示書を書き換える。使えない中身なら書かずに理由を返す（空のリストなら書いた）"""
    name = _check_name(name)
    errs = problems(name, text, group)
    if errs:
        return errs
    d = _dir(group)
    d.mkdir(parents=True, exist_ok=True)
    _write(d / name, text if text.endswith("\n") else text + "\n")
    return []


def reset(name: str | None = None, group: str = GROUP) -> list:
    """初期の指示書に戻す（name を省くと全部）。戻した名前を返す"""
    names = [_check_name(name)] if name else list(TITLES)
    d = _dir(group)
    d.mkdir(parents=True, exist_ok=True)
    man = _manifest(group)
    for n in names:
        default = default_text(n, group)
        _write(d / n, default)
        man[n] = _sha(default)
    _save_manifest(man, group)
    return names
