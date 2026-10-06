"""入口。固めたアプリの実行ファイルは、取得の部品から見て「python」の代わりもする。

  （引数なし）                 メニューバーのアプリ
  -m <モジュール> [引数…]       python -m の代わり（取得の係 acquire.worker をこれで起こす）
  <スクリプト.py> [引数…]       python script.py の代わり（acquire/pipeline.py の _script が sys.executable で呼ぶ）
  --mcp                        Claude に出す道具（Claude デスクトップが起こす。標準入出力で話す）
  --connect-claude             Claude デスクトップの設定に道具を足す（メニュー「Claude につなぐ」と同じ。運営用）
  --wait <分析> [上限の時間]   取得・取り足しが終わるまで待つ（Claude の Code タブで、Claude が作業フォルダの ugc-wait から裏で走らせる）
  --selftest                   同梱物が読めるかだけ確かめて終わる（TikTok には触らない）
"""
import runpy
import sys
from pathlib import Path


def main() -> None:
    from . import config
    argv = sys.argv[1:]
    if argv[:1] == ["--mcp"]:
        from . import mcp_local
        sys.exit(mcp_local.main())
    if argv[:1] == ["--wait"]:   # Claude の Code タブ用（docs/CODE_TAB_ONE_SITTING.md、collector_app/waiter.py）
        from . import waiter
        sys.exit(waiter.main(argv[1:]))
    if argv[:1] == ["--connect-claude"]:   # 運営用: メニューの「Claude につなぐ」と同じことを、画面を出さずに
        from . import claude_link
        backup = claude_link.connect()
        print("控え:", backup or "（元の設定ファイル無し）", "／ 状態:", claude_link.status())
        return
    code = config.setup_env()
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
