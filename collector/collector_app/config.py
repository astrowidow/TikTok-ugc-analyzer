"""置き場所と定数。取得の部品（acquire/）は環境変数で置き場所を受け取るので、読み込む前に setup_env() を呼ぶ。"""
import hashlib
import json
import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import VERSION

APP_NAME = "UGC Collector"
APP_ID = "jp.ugc-analyzer.collector"
OWNER = "local"   # 試作は利用者を区別しない（本線に合流するときはサービスの利用者 ID）

DATA_DIR = Path(os.environ.get("UGC_COLLECTOR_HOME") or Path.home() / "Library" / "Application Support" / APP_NAME)
ANALYSES_DIR = DATA_DIR / "analyses"
LOCK_DIR = DATA_DIR / "locks"
PROFILE_DIR = DATA_DIR / "chrome-profile"   # 取得専用の Chrome のプロファイル（捨て垢のログインはここだけに残る）
CODE_ROOT = DATA_DIR / "code"
LOG_DIR = DATA_DIR / "logs"
STATE_FILE = DATA_DIR / "state.json"
PID_FILE = DATA_DIR / "app.pid"
CHROME_PORT = int(os.environ.get("UGC_COLLECTOR_PORT") or 9250)   # 本線の Windows 機の 9222 とは別

FROZEN = getattr(sys, "frozen", False)
# 本線のリポジトリの直下（ソースから動かすとき）。固めたアプリでは同梱の code/ を DATA_DIR に写して使う
REPO_ROOT = Path(__file__).resolve().parents[2]
CODE_FILES = ["acquire", "analysis/pool.py", "analysis/enrich.py", "analysis/build_llm_input.py",
              "analysis/prep_sample.py", "analysis/prep_comments.py", "scraper.py", "log_setup.py", "tiktok_lock.py"]


def bundled_code_dir() -> Path:
    if FROZEN:
        return Path(sys._MEIPASS) / "code"  # noqa: SLF001
    return REPO_ROOT


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def code_dir() -> Path:
    """取得の部品を動かす場所。ソースならリポジトリそのもの。

    固めたアプリでは、同梱の部品を DATA_DIR/code/<版> に写して使う。アプリ本体の中は書き込めない
    （ダウンロードしたまま開くと読み取り専用の場所で動く＝App Translocation）のに、部品はログを横に書くため。
    本線に合流したあと、部品だけをサービスから入れ替えるときもこの置き場を使う。
    """
    if not FROZEN:
        return REPO_ROOT
    src = bundled_code_dir()
    dst = CODE_ROOT / f"{VERSION}-{_tree_digest(src)}"
    if not (dst / ".complete").exists():
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        (tmp / ".complete").write_text(VERSION, encoding="utf-8")
        shutil.rmtree(dst, ignore_errors=True)
        tmp.rename(dst)
    return dst


def setup_env() -> Path:
    """置き場所を作り、取得の部品が読む環境変数を入れ、部品を import できるようにする"""
    for d in (DATA_DIR, ANALYSES_DIR, LOCK_DIR, PROFILE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    os.environ["UGC_ANALYSES_DIR"] = str(ANALYSES_DIR)
    os.environ["UGC_LOCK_DIR"] = str(LOCK_DIR)
    os.environ["PYTHONUTF8"] = "1"
    code = code_dir()
    for p in (code / "analysis", code):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    return code


def logger() -> logging.Logger:
    log = logging.getLogger("collector")
    if not log.handlers:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LOG_DIR / "collector.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    return log


# --- 小さな状態（ログインを確かめた時刻・画面を消さない設定など） ---
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(**kv) -> dict:
    s = load_state()
    s.update(kv)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)
    return s
