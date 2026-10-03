# PyInstaller の設定（collector/build.sh から使う）。Apple シリコンの Mac 用の .app を作る。
# -*- mode: python ; coding: utf-8 -*-
import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)            # collector/
REPO = HERE.parent               # 本線のリポジトリの直下
VERSION = re.search(r'VERSION = "([^"]+)"', (HERE / "collector_app" / "__init__.py").read_text()).group(1)

# 取得の部品は .py のまま code/ に入れる（実行時に DATA_DIR に写して使う。collector_app/config.py）
code_files = ["acquire", "analysis/pool.py", "analysis/enrich.py", "analysis/build_llm_input.py",
              "analysis/prep_sample.py", "analysis/prep_comments.py", "scraper.py", "log_setup.py", "tiktok_lock.py"]
datas = []
for f in code_files:
    p = REPO / f
    if p.is_dir():
        for q in p.rglob("*"):
            if q.is_file() and "__pycache__" not in q.parts and q.suffix in (".py", ".bat", ".xml", ".md"):
                datas.append((str(q), str(Path("code") / q.parent.relative_to(REPO))))
    else:
        datas.append((str(p), str(Path("code") / p.parent.relative_to(REPO))))
datas += [(str(HERE / "assets" / n), "assets") for n in ("menubar.png", "menubar@2x.png") if (HERE / "assets" / n).exists()]
datas += collect_data_files("selenium")   # Selenium Manager（chromedriver を自動で用意する）を含む

# 部品が使うライブラリ（部品は解析されないので、ここで名前を挙げる）
hidden = ["csv", "argparse", "collections", "contextlib", "math", "random", "smtplib", "email.message", "ctypes",
          "unicodedata", "logging.handlers", "urllib.parse", "secrets", "socket", "traceback", "hashlib", "statistics",
          "pandas", "requests", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "websocket",
          "UserNotifications", "AppKit", "Foundation", "objc"]
hidden += collect_submodules("selenium.webdriver.chrome") + collect_submodules("selenium.webdriver.common") \
    + collect_submodules("selenium.webdriver.remote") + collect_submodules("selenium.webdriver.support")

a = Analysis([str(HERE / "run_collector.py")], pathex=[str(HERE)], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "matplotlib", "IPython", "pytest"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="UGC Collector", console=False, argv_emulation=False,
          target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name="UGC Collector")
app = BUNDLE(coll, name="UGC Collector.app", icon=str(HERE / "assets" / "AppIcon.icns"),
             bundle_identifier="jp.ugc-analyzer.collector", version=VERSION,
             info_plist={"LSUIElement": True, "CFBundleDisplayName": "UGC Collector",
                         "CFBundleShortVersionString": VERSION, "LSMinimumSystemVersion": "13.0",
                         "NSHumanReadableCopyright": "UGC Analyzer（試作）"})
