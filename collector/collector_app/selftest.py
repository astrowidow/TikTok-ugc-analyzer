"""同梱物の自己点検（TikTok には触らない）: 取得の部品が使うライブラリと部品のファイルが読めるか。"""
import importlib
import json
from pathlib import Path

from . import chrome, config

MODULES = ["selenium.webdriver", "selenium.webdriver.common.by", "selenium.webdriver.common.keys", "pandas", "requests",
           "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "websocket", "rumps", "AppKit", "UserNotifications",
           "smtplib", "email.message", "ctypes", "unicodedata", "logging.handlers", "urllib.parse", "secrets",
           "acquire.pipeline", "acquire.worker", "acquire.launch", "acquire.spatest", "acquire.notify",
           "tiktok_lock", "scraper", "enrich", "pool"]


def run(code: Path) -> int:
    res = {"code_dir": str(code), "data_dir": str(config.DATA_DIR), "frozen": config.FROZEN, "missing": [],
           "chrome": str(chrome.find_binary() or "")}
    for m in MODULES:
        try:
            importlib.import_module(m)
        except Exception as e:
            res["missing"].append(f"{m}: {type(e).__name__}: {e}")
    for f in ("acquire/pipeline.py", "analysis/prep_sample.py", "analysis/build_llm_input.py", "analysis/pool.py",
              "analysis/prep_comments.py"):
        if not (code / f).exists():
            res["missing"].append(f"file {f}")
    from acquire import pipeline
    res["analyses_dir"] = str(pipeline.ANALYSES_DIR)
    res["ok"] = not res["missing"] and res["analyses_dir"] == str(config.ANALYSES_DIR)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["ok"] else 1
