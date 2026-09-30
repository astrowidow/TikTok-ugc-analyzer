#!/usr/bin/env python3
"""試作の分析フォルダ（silhouette-proto）を、Mac の手持ちデータ（E2E 試験の写し）から作る。

引き継ぎ書 4-7。フォルダ名は DB_DESIGN の ①〜⑤ に対応させる:
  raw/        ①原本        シルエット.csv（一覧）・enriched.jsonl（属性）・e2e.jsonl（コメント）
  fetch_log/  ②取得の記録  enrich.log・e2e.log・e2e_run2.out・e2e_summary.json
  derived/    ③計算物      videos.jsonl・weekly.tsv・llm_input/・comments/・knowledge/
  outputs/    ④成果物      （AI と人が作ったもの。空で作る）
  eval/       ⑤評価        reference_labels.tsv（E2E のラベル。一致率を出すためだけ。AI には渡さない）
  state/      （サービス）  仕事の状態（空で作る）
  analysis.json             目録

使い方:
  python analysis/build_proto_analysis.py [出力先の親フォルダ]   # 既定 output/analyses
  → できたフォルダを Windows の output\\analyses\\ へ scp する
"""
import datetime
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "output" / "trial_silhouette"
ANALYSIS_ID = "silhouette-proto"


def copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def mtime(p: Path) -> str:
    return datetime.datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def glossary_fh() -> str:
    """著者の用語集から F 章（採用文脈・動機）と H 章（指標の読み方）だけを抜く"""
    text = (ROOT / "output" / "notes_corpus" / "distilled" / "GLOSSARY.md").read_text(encoding="utf-8")
    out, keep = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            keep = line.startswith("## F.") or line.startswith("## H.")
        if keep:
            out.append(line)
    return "# 反応の読み方の語彙（著者の用語集から F 章・H 章）\n\n" + "".join(out)


def main():
    parent = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "analyses"
    dest = parent / ANALYSIS_ID
    if dest.exists():
        sys.exit(f"{dest} はもうあります（消さないので、別の場所を指定するか手で退けてください）")

    copy(ROOT / "シルエット.csv", dest / "raw" / "list.csv")
    copy(SRC / "enriched.jsonl", dest / "raw" / "enriched.jsonl")
    copy(SRC / "e2e" / "e2e.jsonl", dest / "raw" / "comments.jsonl")

    for name in ("enrich.log",):
        copy(SRC / name, dest / "fetch_log" / name)
    for name in ("e2e.log", "e2e_run2.out", "e2e_summary.json"):
        copy(SRC / "e2e" / name, dest / "fetch_log" / name)

    copy(SRC / "videos.jsonl", dest / "derived" / "videos.jsonl")
    copy(SRC / "weekly.tsv", dest / "derived" / "weekly.tsv")
    for name in ("records.jsonl", "taxonomy_sample.md", "label_set.md", "video_urls.json", "sheets"):
        copy(SRC / "llm_input" / name, dest / "derived" / "llm_input" / name)
    copy(SRC / "e2e" / "comments", dest / "derived" / "comments")
    (dest / "derived" / "knowledge").mkdir(parents=True)
    (dest / "derived" / "knowledge" / "glossary_FH.md").write_text(glossary_fh(), encoding="utf-8")

    copy(SRC / "clean" / "labels.tsv", dest / "eval" / "reference_labels.tsv")
    for d in ("outputs", "state"):
        (dest / d).mkdir(parents=True, exist_ok=True)

    n_videos = sum(1 for _ in open(SRC / "videos.jsonl", encoding="utf-8"))
    meta = {
        "analysis_id": ANALYSIS_ID,
        "title": "シルエット（試作）",
        "song": {"artist": "KANA-BOON", "title": "シルエット",
                 "note": "NARUTO 疾風伝 OP。2025年7〜12月に TikTok で再バズした"},
        "owner": "operator",
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "n_videos": n_videos,
        "period": "2025-07〜2025-12",
        "origin": "E2E 試験（2026-09、シルエット）の手持ちデータの写しから作った試作。このフォルダのための取得はしていない。"
                  "ログに日付が無いので、原本の取得日時は Mac の元ファイルの更新時刻（raw_fetched_at）で代える",
        "raw_fetched_at": {name: mtime(p) for name, p in (("list.csv", ROOT / "シルエット.csv"),
                                                          ("enriched.jsonl", SRC / "enriched.jsonl"),
                                                          ("comments.jsonl", SRC / "e2e" / "e2e.jsonl"))},
        "proto": {"comment_seqs": [22, 276, 106],
                  "comment_seqs_why": {"22": "起点に近い（2025-07-30、ID）", "276": "公式（@kanaboon_official）",
                                       "106": "一般（jp_student）"}},
        "layout": {"raw": "①原本", "fetch_log": "②取得の記録", "derived": "③計算物", "outputs": "④成果物",
                   "eval": "⑤評価", "state": "サービスの台帳（仕事の状態）"},
    }
    (dest / "analysis.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(dest)


if __name__ == "__main__":
    main()
