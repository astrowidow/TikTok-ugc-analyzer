"""固めたアプリの入口（PyInstaller はこのファイルから始める）。ソースからは `python -m collector_app`。

取得の部品（acquire/・analysis/ など）は .py のまま同梱して実行時に読み込むので、
部品が使うライブラリは UGCCollector.spec の hiddenimports で PyInstaller に拾わせる。
"""
from collector_app.__main__ import main

if __name__ == "__main__":
    main()
