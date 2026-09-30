"""
利用者ごとの指示の設定（docs/IMPLEMENTATION_HANDOVER.md 6-2）。

変えてよいのは4項目だけ。空欄ならデフォルト（こちらで設定した指示書のまま）:
  style            文体（デフォルトは著者の文体ガイド。利用者の指定は追記として足す。「ガイドを使わない」も選べる）
  focus            レポートの重点
  community_policy 界隈の分け方の方針
  comment_lens     コメント分析の観点
出力の形・検算・仕事の刻み方・繰り返し方は変えられない（指示書に「出力の形の決まりは設定より優先」と書く）。

保存: output/users/<利用者ID>/settings.json（変更の履歴つき）。
仕事を渡すたびに、その時点の設定を分析フォルダの outputs/settings_used.jsonl に写す（どの版で作ったかの記録）。
"""
import copy
import datetime
import json
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
USERS_DIR = Path(os.environ.get("UGC_USERS_DIR", BASE_DIR / "output" / "users"))
MAX_CHARS = 400
ITEMS = {"style": "文体", "focus": "レポートの重点", "community_policy": "界隈の分け方の方針", "comment_lens": "コメント分析の観点"}
DEFAULT = {"style": {"use_guide": True, "extra": ""}, "focus": "", "community_policy": "", "comment_lens": ""}


class SettingsError(Exception):
    pass


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _path(user_id: str) -> Path:
    if not re.match(r"^[A-Za-z0-9_-]+$", user_id or ""):
        raise SettingsError("利用者 ID の形が正しくありません")
    return USERS_DIR / user_id / "settings.json"


def _load(user_id: str) -> dict:
    try:
        data = json.loads(_path(user_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    cur = copy.deepcopy(DEFAULT)
    for k in ITEMS:
        if k in data.get("current", {}):
            cur[k] = data["current"][k]
    return {"current": cur, "history": data.get("history", [])}


def _save(user_id: str, data: dict) -> None:
    p = _path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def get(user_id: str) -> dict:
    return _load(user_id)["current"]


def describe(user_id: str) -> str:
    cur = get(user_id)
    st = cur["style"]
    lines = [f"- 文体: {'著者の文体ガイドを使う' if st.get('use_guide', True) else '著者の文体ガイドは使わない'}"
             + (f"。追記: {st['extra']}" if st.get("extra") else "（追記なし）")]
    for k in ("focus", "community_policy", "comment_lens"):
        lines.append(f"- {ITEMS[k]}: {cur[k] or '（デフォルト）'}")
    return "\n".join(lines)


def set_item(user_id: str, item: str, value: str | None = None, use_style_guide: bool | None = None) -> dict:
    if item not in ITEMS:
        raise SettingsError(f"変えられるのは {', '.join(f'{k}（{v}）' for k, v in ITEMS.items())} だけです")
    value = (value or "").strip()
    if len(value) > MAX_CHARS:
        raise SettingsError(f"{ITEMS[item]} は {MAX_CHARS} 字までにしてください（今 {len(value)} 字）")
    data = _load(user_id)
    old = copy.deepcopy(data["current"][item])
    if item == "style":
        new = dict(old)
        if value or use_style_guide is None:
            new["extra"] = value
        if use_style_guide is not None:
            new["use_guide"] = bool(use_style_guide)
    else:
        new = value
    data["current"][item] = new
    data["history"].append({"at": _now(), "item": item, "old": old, "new": new})
    _save(user_id, data)
    return data["current"]


def reset(user_id: str, item: str | None = None) -> dict:
    data = _load(user_id)
    items = [item] if item else list(ITEMS)
    for k in items:
        if k not in ITEMS:
            raise SettingsError(f"知らない項目です: {k}")
        data["history"].append({"at": _now(), "item": k, "old": data["current"][k], "new": copy.deepcopy(DEFAULT[k])})
        data["current"][k] = copy.deepcopy(DEFAULT[k])
    _save(user_id, data)
    return data["current"]


def prompt_block(user_id: str, items: list) -> str:
    """指示書に差し込む「利用者の設定」の欄。デフォルトのままなら空"""
    cur = get(user_id)
    lines = []
    for k in items:
        if k == "style":
            st = cur["style"]
            if not st.get("use_guide", True):
                lines.append("- 文体: 著者の文体ガイドには合わせない（読みやすい標準的な日本語で）" +
                             (f"。{st['extra']}" if st.get("extra") else ""))
            elif st.get("extra"):
                lines.append(f"- 文体（著者の文体ガイドに加えて）: {st['extra']}")
        elif cur.get(k):
            lines.append(f"- {ITEMS[k]}: {cur[k]}")
    if not lines:
        return ""
    return ("\n### 利用者の設定\n\n" + "\n".join(lines) +
            "\n\n**出力の形の決まり（JSON・TSV の形、見出しの行、検査の条件）は、この設定より優先する。**\n")


def snapshot(a, task: dict, items: list) -> None:
    """仕事を渡したときの設定を分析フォルダに写す（前と同じなら書かない）"""
    cur = get(a.owner) if a.owner else DEFAULT
    vals = {k: cur.get(k) for k in items}
    p = a.outputs("settings_used.jsonl")
    last = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            last.update(row.get("values", {}))
    if all(last.get(k) == v for k, v in vals.items()):
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), "task_id": task.get("task_id"), "values": vals}, ensure_ascii=False) + "\n")
