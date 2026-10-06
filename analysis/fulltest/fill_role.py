#!/usr/bin/env python3
"""AI 役の指示（AI_ROLE.md）に、走行のフォルダ・呼び出し口・道具の説明を差し込んで、走行のフォルダに AI_ROLE_full.md を書く。
  python analysis/fulltest/fill_role.py <走行のフォルダ>   （先に、そのフォルダで ugc.py tools > tools.txt を作っておく）"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
run = Path(sys.argv[1]).resolve()
tools = (run / "tools.txt").read_text(encoding="utf-8")
text = (HERE / "AI_ROLE.md").read_text(encoding="utf-8")
for k, v in {"{{RUN}}": str(run), "{{PY}}": str(ROOT / "collector/.venv/bin/python"),
             "{{UGC}}": str(HERE / "ugc.py"), "{{TOOLS}}": tools}.items():
    text = text.replace(k, v)
(run / "AI_ROLE_full.md").write_text(text, encoding="utf-8")
print(run / "AI_ROLE_full.md", len(text), "字")
