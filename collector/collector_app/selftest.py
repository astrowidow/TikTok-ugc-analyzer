"""同梱物の自己点検（TikTok にも Claude にも触らない）: 部品が使うライブラリ・部品のファイル・指示書・知識ベースが読めるか。"""
import importlib
import json
from pathlib import Path

from . import chrome, config

MODULES = ["selenium.webdriver", "selenium.webdriver.common.by", "selenium.webdriver.common.keys", "pandas", "requests",
           "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "websocket", "rumps", "AppKit", "UserNotifications",
           "smtplib", "email.message", "ctypes", "unicodedata", "logging.handlers", "urllib.parse", "secrets",
           "sqlite3", "fcntl",
           "acquire.pipeline", "acquire.worker", "acquire.launch", "acquire.spatest", "acquire.notify",
           "tiktok_lock", "scraper", "enrich", "pool",
           "mcp.server.mcpserver", "mcp.server.stdio", "mcp.types", "proto_runner", "flow_w1", "user_settings",
           "mcp_proto", "kb_update", "prompt_store", "collector_app.mcp_local", "collector_app.claude_link"]


def run(code: Path) -> int:
    res = {"code_dir": str(code), "data_dir": str(config.DATA_DIR), "frozen": config.FROZEN, "missing": [],
           "chrome": str(chrome.find_binary() or "")}
    for m in MODULES:
        try:
            importlib.import_module(m)
        except Exception as e:
            res["missing"].append(f"{m}: {type(e).__name__}: {e}")
    for f in ("acquire/pipeline.py", "analysis/prep_sample.py", "analysis/build_llm_input.py", "analysis/pool.py",
              "analysis/prep_comments.py", "store.py", "service_prompts/w1/axes.md", "service_prompts/w1/done.md"):
        if not (code / f).exists():
            res["missing"].append(f"file {f}")
    kb = config.bundled_knowledge_dir()
    if not kb:
        res["missing"].append("knowledge（同梱の知識ベース）")
    else:
        notes = kb / "notes" if (kb / "notes").exists() else kb
        res["knowledge_notes"] = len(list(notes.glob("*.md")))
    try:
        done = config.seed_data()
        import kb_update
        import prompt_store
        res["seed"] = done
        res["kb_ready"] = kb_update.ready()
        res["prompts_ok"] = all(r["ok"] for r in prompt_store.status())
        if not res["kb_ready"] or not res["prompts_ok"]:
            res["missing"].append("知識ベースか指示書を置けませんでした")
    except Exception as e:
        res["missing"].append(f"seed: {type(e).__name__}: {e}")
    from acquire import pipeline
    res["analyses_dir"] = str(pipeline.ANALYSES_DIR)
    res["ok"] = not res["missing"] and res["analyses_dir"] == str(config.ANALYSES_DIR)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["ok"] else 1
