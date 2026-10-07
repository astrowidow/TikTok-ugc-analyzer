"""
取得が済んだばかり（AI の仕事がまだ）の分析を、実験の置き場にそのまま写す（2026-10-07 一覧の段の台帳 0.7.0 の確かめ。docs/LIST_CUT.md 第7章）。
AI も TikTok も使わない。

  python3 analysis/comment_exp/fork_fresh.py a20261007-1556-761f x-sil-v070

- 元の分析は本物の置き場から読むだけ。写しは実験の置き場（common.HOME）の analyses/<新しい ID>/
- 写すもの: raw（covers を除く。サムネの一覧は derived/llm_input/sheets に作ってある）・fetch_log・derived・analysis.json
- AI の仕事の状態（state）は写さない（AI 役が最初の仕事＝界隈の軸から回す）。界隈の確認は省く（skip_confirm）
- 題を変える（同じ題だとレポートのフォルダを上書きする）
- --rederive: 集計と AI の入力（derive）を今のコードで作り直す（取得のあとに集計を直したとき。動画の一覧と通し番号は変わらない）
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

common.setup_env()
from acquire import pipeline  # noqa: E402


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    src = common.APP_DATA / "analyses" / sys.argv[1]
    new = sys.argv[2]
    dst = common.analysis_dir(new)
    if dst.exists():
        sys.exit(f"{dst} はもうある")
    m = json.loads((src / "analysis.json").read_text(encoding="utf-8"))
    if (m.get("acquisition") or {}).get("status") != "done":
        sys.exit(f"{sys.argv[1]} の取得がまだ済んでいない")
    shutil.copytree(src / "raw", dst / "raw", ignore=shutil.ignore_patterns("covers"))
    for sub in ("fetch_log", "derived"):
        shutil.copytree(src / sub, dst / sub)
    for sub in ("outputs", "eval", "state"):
        (dst / sub).mkdir(parents=True, exist_ok=True)
    m.update({"analysis_id": new, "title": f"{m['title']}（{new}）", "owner": common.USER, "forked_from": sys.argv[1],
              "forked_at": pipeline.now()})
    (dst / "analysis.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    import proto_runner as pr
    pr._set_options(dst, skip_confirm=True, skip_confirm_at=pipeline.now())
    if "--rederive" in sys.argv:
        print(json.dumps(pipeline.Run(new).step_derive(), ensure_ascii=False))
    led = sum(1 for _ in open(dst / "raw" / "ledger.jsonl", encoding="utf-8")) if (dst / "raw" / "ledger.jsonl").exists() else None
    links = sum(1 for _ in open(dst / "raw" / "grid_links.jsonl", encoding="utf-8"))
    print(json.dumps({"id": new, "title": m["title"], "ledger": led, "links": links}, ensure_ascii=False))


if __name__ == "__main__":
    main()
