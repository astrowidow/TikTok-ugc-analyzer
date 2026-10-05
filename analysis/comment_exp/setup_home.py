"""
実験の置き場（output/comment_exp/home/）を作る。docs/COMMENT_STRATEGY_HANDOVER.md 第5章 段0 の 1。

  python3 analysis/comment_exp/setup_home.py

知識ベース（knowledge/）と指示書（prompts/）は、今のアプリの置き場から写して**固定する**
（知識ベースは週1回の自動更新で中身が変わるので、回し直しの間に変わると比べられない）。本物の置き場は読むだけ。
もうあれば何もしない（写し直すのは --refresh。それまでの回し直しと比べられなくなるので、ふつうは使わない）。
"""
import argparse
import datetime
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402


def tree_md5(d: Path) -> str:
    h = hashlib.md5()
    for p in sorted(d.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(d)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    home = common.HOME
    snap = home / "SNAPSHOT.json"
    if snap.exists() and not args.refresh:
        print(f"もうあります: {home}\n" + snap.read_text(encoding="utf-8"))
        return
    for sub in common.DIRS.values():
        (home / sub).mkdir(parents=True, exist_ok=True)
    info = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "from": str(common.APP_DATA)}
    for sub in ("knowledge", "prompts"):
        src, dst = common.APP_DATA / sub, home / sub
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        info[sub + "_md5"] = tree_md5(dst)
    snap.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
