"""
コメント取得戦略の実験（docs/COMMENT_STRATEGY_HANDOVER.md 第5章）の共通部分: 実験の置き場と環境変数。

本物の置き場（~/Library/Application Support/UGC Analyzer/）では回さない。写しは output/comment_exp/home/ に置き、
proto_runner・flow_w1 が読む環境変数をそこへ向ける（付けないとリポジトリの output/analyses を見に行く。第8章の10）。
proto_runner は import のときに置き場を決めるので、setup_env() は import より前に呼ぶ。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_DATA = Path.home() / "Library" / "Application Support" / "UGC Analyzer"
EXP = ROOT / "output" / "comment_exp"
HOME = Path(os.environ.get("UGC_EXP_HOME", EXP / "home"))
USER = "local"          # 写しの owner。AI 役の道具もこの利用者として呼ぶ

DIRS = {"UGC_ANALYSES_DIR": "analyses", "UGC_KB_DIR": "knowledge", "UGC_PROMPTS_DIR": "prompts",
        "UGC_USERS_DIR": "users", "UGC_REPORTS_DIR": "reports"}


def setup_env() -> None:
    for k, sub in DIRS.items():
        os.environ[k] = str(HOME / sub)
    os.environ["PYTHONUTF8"] = "1"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def analysis_dir(aid: str) -> Path:
    return HOME / "analyses" / aid
