"""取得の係の入口（アプリ用）。本線の acquire.worker をそのまま回し、次の2点だけ外から差し込む。

1. 取得用 Chrome の用意（pipeline.ensure_chrome）: 本線は Windows のタスクで起こすが、Mac にはそれが無いので、
   このアプリの Chrome を最小化で起動する。**コメントの段の頭で初めて起動する**（一覧・属性はヘッドレスで取るので、
   それまでは画面に何も出さない。2026-10-02 ユーザー「使ってないのにウィンドウが出るのは紛らわしい」）。
   ログインが切れていたら、アプリに知らせて（need_login.json）ログインされるまで待つ
2. ちょいとり（acquisition_settings の trial_links / trial_pool / trial_cap）: 一覧とプールを小さく切る
   （2026-10-02 ユーザー「ちょいとりは5分で終わる規模に」）。要求の速さは変えない

本線のファイルは変えない（合流するときに、本線に入れるかを決める）。

  <アプリ> -m collector_app.worker_entry
"""
import json
import sys
import time

from . import chrome as chrome_mod
from . import config

LOGIN_WAIT_HOURS = 6


def need_login_flag():
    return config.LOCK_DIR / "need_login.json"


def main() -> int:
    config.setup_env()
    from acquire import pipeline, worker
    log = config.logger()

    def ensure_chrome(port, plog):
        c = chrome_mod.Chrome(int(port), config.PROFILE_DIR, log)
        if not c.listening():
            plog(f"    取得用の Chrome を最小化で起動します（ポート {port}）")
            c.ensure(minimized=True)
        if c.logged_in():
            return
        flag = need_login_flag()
        flag.write_text(json.dumps({"since": pipeline.now()}), encoding="utf-8")
        plog("    TikTok のログインが切れています。アプリがログインの画面を出すので、ログインされるまで待ちます")
        t0 = time.time()
        try:
            while time.time() - t0 < LOGIN_WAIT_HOURS * 3600:
                time.sleep(10)
                try:
                    if c.listening() and c.logged_in():
                        plog("    ログインを確かめました。続けます")
                        return
                except Exception:
                    pass
            raise pipeline.StepError(f"TikTok にログインされないまま{LOGIN_WAIT_HOURS}時間たったので止めました")
        finally:
            flag.unlink(missing_ok=True)

    pipeline.ensure_chrome = ensure_chrome

    orig_list = pipeline.Run.step_list

    def step_list(self):
        res = orig_list(self) or {}
        n = int(self.settings().get("trial_links") or 0)
        if not n:
            return res
        p = self.p("raw", "grid_links.jsonl")
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) > n:
            p.write_text("\n".join(lines[:n]) + "\n", encoding="utf-8")
            self._write_links_csv()   # noqa: SLF001
            self.log(f"    ちょいとり: 一覧を先頭{n}本に切りました（{len(lines)}本 → {n}本）")
        return {**res, "links": min(len(lines), n), "trial_links_from": len(lines)}

    pipeline.Run.step_list = step_list

    orig_pool = pipeline.Run.step_pool

    def step_pool(self):
        res = orig_pool(self) or {}
        s = self.settings()
        n = int(s.get("trial_pool") or 0)
        if not n:
            return res
        p = self.p("derived", "pool.tsv")
        rows = [r for r in p.read_text(encoding="utf-8").splitlines() if r.strip()]
        head, body = rows[0], rows[1:]
        ci = head.split("\t").index("cap")
        cap = str(int(s.get("trial_cap") or 30))
        cut = []
        for r in body[:n]:
            f = r.split("\t")
            f[ci] = cap
            cut.append("\t".join(f))
        p.write_text("\n".join([head, *cut]) + "\n", encoding="utf-8")
        self.log(f"    ちょいとり: コメントを取る動画を{len(cut)}本・各{cap}件までにしました（プール{len(body)}本から）")
        return {**res, "n_pool": len(cut), "trial_pool_from": len(body)}

    pipeline.Run.step_pool = step_pool
    return worker.main()


if __name__ == "__main__":
    sys.exit(main())
