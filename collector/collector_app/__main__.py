"""入口。固めたアプリの実行ファイルは、取得の部品から見て「python」の代わりもする。

  （引数なし）                 メニューバーのアプリ
  -m <モジュール> [引数…]       python -m の代わり（取得の係 acquire.worker をこれで起こす）
  <スクリプト.py> [引数…]       python script.py の代わり（acquire/pipeline.py の _script が sys.executable で呼ぶ）
  --selftest                   同梱物が読めるかだけ確かめて終わる（TikTok には触らない）
"""
import runpy
import sys
from pathlib import Path


def main() -> None:
    from . import config
    code = config.setup_env()
    argv = sys.argv[1:]
    if argv[:1] == ["-m"] and len(argv) >= 2:
        mod = argv[1]
        sys.argv = [mod, *argv[2:]]
        runpy.run_module(mod, run_name="__main__", alter_sys=True)
        return
    if argv and argv[0].endswith(".py"):
        path = Path(argv[0])
        if not path.is_absolute():
            path = Path.cwd() / path
        sys.argv = [str(path), *argv[1:]]
        sys.path.insert(0, str(path.parent))
        runpy.run_path(str(path), run_name="__main__")
        return
    if argv[:1] == ["--selftest"]:
        from . import selftest
        sys.exit(selftest.run(code))
    from . import app
    app.run()


if __name__ == "__main__":
    main()
