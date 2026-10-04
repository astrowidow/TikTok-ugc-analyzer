# PyInstaller の設定（collector/build.sh から使う）。Apple シリコンの Mac 用の .app を作る。
# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)            # collector/
REPO = HERE.parent               # 本線のリポジトリの直下
sys.path.insert(0, str(HERE))
from collector_app import VERSION                 # noqa: E402
from collector_app.config import CODE_FILES       # noqa: E402

# 取得の部品と AI の相手は .py・指示書（.md）のまま code/ に入れる（実行時に DATA_DIR に写して使う。collector_app/config.py）
datas = []
for f in CODE_FILES:
    p = REPO / f
    if p.is_dir():
        for q in p.rglob("*"):
            if q.is_file() and "__pycache__" not in q.parts and q.suffix in (".py", ".bat", ".xml", ".md"):
                datas.append((str(q), str(Path("code") / q.parent.relative_to(REPO))))
    else:
        datas.append((str(p), str(Path("code") / p.parent.relative_to(REPO))))

# 最初の知識ベース（著者の note 記事と蒸留物）。リポジトリには入れていない（output/ は git 管理外）ので、作る Mac の手元から
KB = REPO / "output" / "notes_corpus"
if not (KB / "distilled" / "GLOSSARY.md").exists():
    raise SystemExit(f"知識ベースが見つかりません: {KB}（output/notes_corpus/ を置いてから作ってください）")
for q in sorted(KB.glob("*.md")):
    datas.append((str(q), "knowledge/notes"))
datas.append((str(KB / "INDEX.json"), "knowledge"))
for n in ("README.md", "GLOSSARY.md", "STYLE_GUIDE.md", "cards.jsonl", "COMMUNITY_GUIDE.md", "community_defs.jsonl"):
    datas.append((str(KB / "distilled" / n), "knowledge/distilled"))

datas += [(str(HERE / "assets" / n), "assets") for n in ("menubar.png", "menubar@2x.png") if (HERE / "assets" / n).exists()]
datas += collect_data_files("selenium")   # Selenium Manager（chromedriver を自動で用意する）を含む

# 部品が使うライブラリ（部品は解析されないので、ここで名前を挙げる）
hidden = ["csv", "argparse", "collections", "contextlib", "math", "random", "smtplib", "email.message", "ctypes",
          "unicodedata", "logging.handlers", "urllib.parse", "secrets", "socket", "traceback", "hashlib", "statistics",
          "sqlite3", "zipfile", "io", "typing", "fcntl", "html", "difflib",
          "pandas", "requests", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "websocket",
          "UserNotifications", "AppKit", "Foundation", "objc"]
hidden += collect_submodules("selenium.webdriver.chrome") + collect_submodules("selenium.webdriver.common") \
    + collect_submodules("selenium.webdriver.remote") + collect_submodules("selenium.webdriver.support")
# Claude に出す道具（mcp_proto.py が使う MCP の SDK）
hidden += collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli")) \
    + collect_submodules("mcp_types") + ["pydantic", "pydantic_core", "anyio"]   # mcp.cli は読むと終わる（CLI 用）

a = Analysis([str(HERE / "run_collector.py")], pathex=[str(HERE)], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "matplotlib", "IPython", "pytest"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="UGC Analyzer", console=False, argv_emulation=False,
          target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name="UGC Analyzer")
app = BUNDLE(coll, name="UGC Analyzer.app", icon=str(HERE / "assets" / "AppIcon.icns"),
             bundle_identifier="jp.ugc-analyzer.collector", version=VERSION,
             info_plist={"LSUIElement": True, "CFBundleDisplayName": "UGC Analyzer",
                         "CFBundleShortVersionString": VERSION, "LSMinimumSystemVersion": "13.0",
                         "NSHumanReadableCopyright": "UGC Analyzer"})
