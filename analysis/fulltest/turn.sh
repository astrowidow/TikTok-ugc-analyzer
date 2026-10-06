#!/bin/bash
# 会話の1往復を記録に足す: AI 役の前の返事（ファイル）と、利用者の次の発言（文字列）。
#   analysis/fulltest/turn.sh <走行のフォルダ> <AI の返事のファイル|-> '<利用者の次の発言>'
set -e
cd "$1"
PY=../../collector/.venv/bin/python; U=../../analysis/fulltest/ugc.py
[ "$2" != "-" ] && $PY $U say ai "$2" >/dev/null
if [ -n "${3:-}" ]; then printf '%s' "$3" > work/_u.txt; $PY $U say user work/_u.txt >/dev/null; fi
echo "記録: $(wc -l < log.jsonl) 行"
