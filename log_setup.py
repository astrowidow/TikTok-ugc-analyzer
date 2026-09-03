"""
ログ出力の共通設定。

「動かない」と言われたときに、どのジョブがどこで止まったのかを
後から追えるように、ファイルとコンソールの両方に出力する。

ログファイル: logs/ugc-analyzer.log （5MBごとに世代交代、5世代まで保存）
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "ugc-analyzer.log")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-8s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging():
    """ルートロガーを設定する。何度呼んでも二重に登録されない"""
    global _configured
    if _configured:
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    # 環境変数 UGC_LOG_LEVEL=DEBUG でより細かいログを出せる
    root.setLevel(os.environ.get("UGC_LOG_LEVEL", "INFO").upper())
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # ライブラリの通信ログでログが埋まらないようにする
    for noisy in ("selenium", "urllib3", "httpx", "httpcore", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def brief(error: Exception, limit: int = 200) -> str:
    """Seleniumの例外は長大なスタックトレースを含むので、1行目だけに切り詰める"""
    return str(error).strip().split("\n")[0][:limit]
