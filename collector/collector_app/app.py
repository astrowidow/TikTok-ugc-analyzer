"""メニューバーのアプリ（Rectangle と同じく、メニューバーのアイコンだけ。Dock にも画面にも出ない）。

画面の操作はメインのスレッド、取得の見張り（Chrome・ログイン・係・再開・通知）は裏のスレッドで回す。
"""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

from . import VERSION, chrome, config, jobs, notify, system, worker_entry

MUSIC_URL_RE = re.compile(r"^https://(www\.)?tiktok\.com/music/[^\s/]+-\d+")
# 試験用: Chrome とログインを飛ばす（TikTok に触らずにメニュー・係・通知だけを確かめる）
TEST_NO_CHROME = bool(os.environ.get("UGC_COLLECTOR_TEST_NO_CHROME"))
TICK = 5            # 見張りの間隔（秒）
RENDER_EVERY = 600  # 取得中に描画を確かめる間隔（秒）
RETRY_WAIT = 300    # 止まった取得を続きから取り直すまで（秒）
CHROME_IDLE_QUIT = 90   # 仕事が無くなってから取得用の Chrome を閉じるまで（秒）


def _asset(name: str) -> str:
    base = Path(sys._MEIPASS) / "assets" if config.FROZEN else Path(__file__).resolve().parent.parent / "assets"  # noqa: SLF001
    return str(base / name)


class Controller:
    """裏で回す見張り役。状態は status_text に出し、画面のタイマーが拾う"""

    def __init__(self, code_dir: Path, log):
        self.log = log
        self.chrome = chrome.Chrome(config.CHROME_PORT, config.PROFILE_DIR, log)
        self.worker = jobs.Worker(code_dir, log)
        self.worker.keep_display_on = bool(config.load_state().get("keep_display_on"))
        self.source = jobs.LocalJobs()
        self.status_text = "起動中…"
        self.sleeping = False
        self.login_wanted = False      # ログインの画面を出して、ログインを待っている
        self.login_presented = False   # ログインの画面を前に出した（何度も前に出さない）
        self.retry_after = 0.0
        self.pending_retry = {}        # 分析 ID → 取り直す時刻（係が終わってから待ち行列に戻す）
        self.next_render = 0.0
        self.render_warned = False
        self.render_suspect = False
        self.chrome_relaunch_noted = False
        self.idle_since = None
        self.known = {}                # 分析 ID → 前に見た状態（変わったら通知）
        self.lock = threading.RLock()
        self.stopped = False

    # --- 起動・停止 ---
    def start(self):
        for m in jobs.analyses():
            self.known[m["analysis_id"]] = (m.get("acquisition") or {}).get("status")
        threading.Thread(target=self._loop, name="collector", daemon=True).start()

    def shutdown(self):
        self.stopped = True
        self.worker.stop("アプリの終了")
        self.chrome.quit()

    # スリープの知らせは画面のスレッドに来る。見張りが Chrome の起動などで lock を持っていても待たない
    def on_sleep(self):
        self.sleeping = True
        self.worker.stop("Mac のスリープ")   # 止めた分析は、起きたあと続きから

    def on_wake(self):
        self.retry_after = time.time() + 30   # ネットがつながるのを少し待つ
        self.sleeping = False

    # --- 見張り ---
    def _loop(self):
        while not self.stopped:
            try:
                with self.lock:
                    if not self.sleeping:
                        self._tick()
            except Exception as e:   # 見張りは止めない
                self.log.exception("見張りで例外: %s", e)
                self.status_text = f"エラー: {e}"
            time.sleep(TICK)

    def _tick(self):
        if not chrome.find_binary():
            self.status_text = "Google Chrome が必要です（入れてから開き直してください）"
            return
        self._check_transitions()
        st = config.load_state()
        if TEST_NO_CHROME:
            if self.worker.running():
                cur = next((m for m in jobs.analyses() if (m.get("acquisition") or {}).get("status") == "running"), None)
                self.status_text = jobs.describe(cur) if cur else "取得の準備中…"
            elif jobs.queued_ids():
                self.worker.start()
            else:
                self.status_text = "準備OK・待機中（試験: Chrome なし）"
            return
        # 取得の係がコメントの段でログイン切れに気づいた（worker_entry.py が印を置いて待っている）
        if worker_entry.need_login_flag().exists() and not self.login_wanted:
            config.save_state(logged_in_at=None)
            self.login_wanted, self.login_presented = True, False
        if self.login_wanted or not st.get("logged_in_at"):
            self._login_flow()
            return
        if self.worker.running():
            self.idle_since = None
            cur = next((m for m in jobs.analyses() if (m.get("acquisition") or {}).get("status") == "running"), None)
            self.status_text = jobs.describe(cur) if cur else "取得の準備中…"
            if cur and (cur.get("acquisition") or {}).get("step") == "comments":
                self._keep_chrome()
                self._render_watch()
            return
        # 止まった取得の取り直し: 係が終わってから、待ち時間をおいて待ち行列に戻す
        # （係が生きているうちに戻すと、係がすぐ拾い直して失敗を繰り返す。2026-10-02 の試しで3回連続失敗した）
        for aid, when in list(self.pending_retry.items()):
            if time.time() >= when:
                jobs.requeue(aid)
                self.known[aid] = "queued"
                del self.pending_retry[aid]
        q = jobs.queued_ids()
        waits = [w for w in [self.retry_after, *self.pending_retry.values()] if w > time.time()]
        if waits and (q or self.pending_retry):
            left = int(min(waits) - time.time())
            self.status_text = (f"少し待ってから続きを取ります（あと{max(left // 60, 1)}分）" if left >= 60
                                else f"少し待ってから続きを取ります（あと{max(left, 1)}秒）")
            return
        if q:
            # 取得用の Chrome はここでは開かない（コメントの段の頭で worker_entry が開く）
            self.worker.start()
            self.next_render = 0.0
            self.render_warned = False
            self.chrome_relaunch_noted = False
            self.status_text = "取得を始めます…"
            return
        # 仕事が無い
        self.status_text = "準備OK・待機中"
        if self.chrome.listening():
            self.idle_since = self.idle_since or time.time()
            if time.time() - self.idle_since > CHROME_IDLE_QUIT and self.chrome.window_state() == "minimized":
                self.chrome.quit()   # 使わない間は取得用の Chrome を閉じておく（Dock のアイコンも消える）
        else:
            self.idle_since = None

    def _login_flow(self):
        """捨て垢のログインを待つ。ログインは利用者が自分で、取得用の Chrome の画面でする"""
        if not self.chrome.listening():
            self.chrome.ensure(url=chrome.LOGIN_URL, minimized=False)
            self.login_presented = False
        if not self.login_presented:
            if self.chrome.logged_in():   # すでにログイン済み（前にログインしたプロファイル）
                self.login_presented = True
            else:
                self.chrome.navigate(chrome.LOGIN_URL)
                self.chrome.show()
                notify.send("TikTok にログインしてください",
                            "開いた Chrome で、捨て垢（ふだん使っていないアカウント）でログインしてください")
                self.login_presented = True
            self.login_wanted = True
        if self.chrome.logged_in():
            config.save_state(logged_in_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            self.login_wanted = False
            self.login_presented = False
            self.chrome.minimize()
            self.log.info("TikTok のログインを確かめました")
            notify.send("準備OK", "ログインできました。取得用の Chrome は Dock にしまいます。このまま使えます")
            self.status_text = "準備OK・待機中"
            return
        self.status_text = "TikTok のログイン待ち（取得用の Chrome で、捨て垢でログイン）"

    def _check_transitions(self):
        """分析の状態が変わったら知らせる。止まったものは続きから取り直す"""
        for m in jobs.analyses():
            aid = m["analysis_id"]
            st = (m.get("acquisition") or {}).get("status")
            before = self.known.get(aid)
            self.known[aid] = st
            if st == before:
                continue
            title = m.get("title") or aid
            if st == "done" and before is not None:
                c = (((m.get("acquisition") or {}).get("steps") or {}).get("comments") or {}).get("detail") or {}
                notify.send(f"「{title}」の取得が終わりました",
                            f"コメント {c.get('videos_ok', '?')}本・{c.get('comments', '?')}件。"
                            "メニューの「データのフォルダを開く」から見られます")
                self.log.info("取得が終わりました: %s %s", aid, c)
            elif st == "failed" and before is not None:
                err = (m.get("acquisition") or {}).get("error") or ""
                self.log.warning("取得が止まりました: %s %s", aid, err)
                if "ログイン" in err:
                    config.save_state(logged_in_at=None)
                if jobs.retriable(m):
                    # Chrome が閉じられた・つながらない、はすぐ取り直す。それ以外（TikTok 側など）は少し空ける
                    quick = any(k in err for k in ("Chrome", "WebDriver", "接続できません", "session", "Session"))
                    wait = 30 if quick else RETRY_WAIT
                    self.pending_retry[aid] = time.time() + wait
                    if not quick:
                        notify.send(f"「{title}」の取得が止まりました",
                                    f"{wait // 60}分後に続きから取り直します（{err[:60]}）")
                else:
                    notify.send(f"「{title}」の取得が止まりました", err[:120])

    def _keep_chrome(self):
        """コメントの段で取得用の Chrome が閉じられたら、すぐ最小化で開き直す（2026-10-02 ユーザー）。
        取りかけの1本は失敗になるが、係が続きから取り直す（止まった場合はアプリが30秒後に続きから）"""
        if self.chrome.listening():
            return
        self.log.info("取得中に取得用の Chrome が閉じられたので、最小化で開き直します")
        self.chrome.ensure(minimized=True)
        if not self.chrome_relaunch_noted:
            self.chrome_relaunch_noted = True
            notify.send("取得用の Chrome を開き直しました",
                        "取得の途中は、取得用の Chrome を閉じないでください（Dock にしまってあります）")

    def _render_watch(self):
        """取得中のタブで描画が回っているか（止まると20件で頭打ちになる。2-9）。止まっていたら知らせる"""
        if time.time() < self.next_render or not self.chrome.listening():
            return
        if self.next_render == 0.0:   # コメントの段に入ったばかり。開いて落ち着くまで少し待つ
            self.next_render = time.time() + 60
            return
        self.next_render = time.time() + RENDER_EVERY
        try:
            r = self.chrome.render_check()
        except Exception as e:
            self.log.info("描画の確かめができませんでした: %s", e)
            return
        ok = r.get("vis") == "visible" and int(r.get("frames") or 0) > 0
        self.log.info("描画: %s フレーム=%s 窓=%s %s", r.get("vis"), r.get("frames"), self.chrome.window_state(),
                      "OK" if ok else "★止まっている（確かめ直す）" if not self.render_suspect else "★止まっている")
        if ok:
            self.render_suspect = False
            return
        # フォーカスの模擬は取得の部品がつないでいる間だけ効く。動画の切り替わりやコメントの段の終わりに当たると
        # hidden に見えることがある（2026-10-02 の誤報）。1分後にもう一度確かめ、まだコメントの段で止まっていれば知らせる
        if not self.render_suspect:
            self.render_suspect = True
            self.next_render = time.time() + 60
            return
        if not self.render_warned:
            self.render_warned = True
            notify.send("取得の画面の描画が止まっています",
                        "このままだとコメントが20件で止まります。メニューの「取得の画面を見る」で開いてください")

    # --- メニューから ---
    def create(self, song, artist, url, trial):
        with self.lock:
            aid = self.source.create(song, artist, url, trial=trial)
            self.known[aid] = "queued"
            self.retry_after = 0
        return aid

    def request_login(self):
        with self.lock:
            self.chrome.ensure(url=chrome.LOGIN_URL, minimized=False)
            self.chrome.navigate(chrome.LOGIN_URL)
            self.chrome.show()
            self.login_wanted, self.login_presented = True, True

    def set_keep_display(self, on: bool):
        config.save_state(keep_display_on=on)
        self.worker.restart_display_setting(on)


class CollectorApp(rumps.App):
    def __init__(self, ctl: Controller):
        icon = _asset("menubar.png")
        super().__init__(config.APP_NAME, icon=icon if Path(icon).exists() else None, template=True,
                         quit_button=None)
        if not Path(icon).exists():
            self.title = "UGC"
        self.ctl = ctl
        self.status_item = rumps.MenuItem("起動中…")
        self.keep_display = rumps.MenuItem("取得中は画面を消さない", callback=self.toggle_display)
        self.keep_display.state = int(ctl.worker.keep_display_on)
        self.login_item = rumps.MenuItem("ログイン時に起動", callback=self.toggle_login_item)
        self.login_item.state = int(system.login_item_enabled())
        self.menu = [
            self.status_item,
            None,
            rumps.MenuItem("曲を取得する（本番の規模・約13時間）…", callback=self.add_full),
            rumps.MenuItem("ちょいとり（試し・約5分）…", callback=self.add_trial),
            rumps.MenuItem("止まった取得を続きから再開", callback=self.resume_failed),
            None,
            rumps.MenuItem("取得の画面を見る", callback=self.show_chrome),
            rumps.MenuItem("取得の画面をしまう", callback=self.hide_chrome),
            rumps.MenuItem("TikTok にログイン", callback=self.login),
            None,
            rumps.MenuItem("データのフォルダを開く", callback=self.open_data),
            rumps.MenuItem("取得の記録を開く", callback=self.open_logs),
            self.keep_display,
            self.login_item,
            None,
            rumps.MenuItem(f"試作 {VERSION}（サービスにはつながっていません）"),
            rumps.MenuItem("終了", callback=self.quit_app),
        ]
        rumps.Timer(self.refresh, 2).start()

    def refresh(self, _):
        t = self.ctl.status_text
        if self.status_item.title != t:
            self.status_item.title = t

    # --- 曲を頼む（試作だけ。本線ではここは AI との会話になる） ---
    def _ask_song(self, trial: bool):
        w = rumps.Window(
            message=("曲名とアーティスト名を「曲名 / アーティスト名」の形で入れてください。\n"
                     "TikTok の楽曲ページの URL（https://www.tiktok.com/music/…）でもかまいません。"
                     + ("\n\nちょいとり: 動画20本の一覧と、2本のコメント（各20件）だけ取ります。約5分です。" if trial else
                        "\n\n本番の規模: 終わるまで13時間前後。そのあいだ Mac を開いたまま、電源につないでおいてください。")),
            title="ちょいとり（試し・約5分）" if trial else "曲を取得する",
            default_text="", ok="取得を始める", cancel="やめる", dimensions=(360, 24))
        r = w.run()
        text = (r.text or "").strip()
        if r.clicked != 1 or not text:
            return
        song, artist, url = "", "", ""
        if text.startswith("http"):
            if not MUSIC_URL_RE.match(text):
                rumps.alert("楽曲ページの URL は https://www.tiktok.com/music/曲名-数字 の形です")
                return
            url = text.split("?")[0]
        else:
            parts = re.split(r"\s*[/／]\s*", text, maxsplit=1)
            song, artist = parts[0], (parts[1] if len(parts) > 1 else "")
        aid = self.ctl.create(song, artist, url, trial)
        notify.send("取得を受け付けました",
                    f"「{song or url}」（{aid}）。Mac を開いたまま、電源につないでおいてください")

    def resume_failed(self, _):
        ids = jobs.failed_ids()
        if not ids:
            rumps.alert("止まっている取得はありません")
            return
        with self.ctl.lock:
            for aid in ids:
                jobs.requeue(aid, manual=True)
                self.ctl.known[aid] = "queued"
                self.ctl.pending_retry.pop(aid, None)
            self.ctl.retry_after = 0
        notify.send("続きから再開します", f"{len(ids)}件の取得を、済んだところの続きから取り直します")

    def add_full(self, _):
        self._ask_song(trial=False)

    def add_trial(self, _):
        self._ask_song(trial=True)

    # --- 取得用の Chrome ---
    def show_chrome(self, _):
        def go():
            if not self.ctl.chrome.listening():
                notify.send("取得用の Chrome は閉じています", "コメントを取る段と、ログインのときだけ開きます")
                return
            self.ctl.chrome.show()
        threading.Thread(target=go, daemon=True).start()

    def hide_chrome(self, _):
        threading.Thread(target=lambda: self.ctl.chrome.listening() and self.ctl.chrome.minimize(), daemon=True).start()

    def login(self, _):
        threading.Thread(target=self.ctl.request_login, daemon=True).start()

    # --- そのほか ---
    def open_data(self, _):
        subprocess.run(["open", str(config.ANALYSES_DIR)])

    def open_logs(self, _):
        """いちばん新しい分析の取得の記録（無ければアプリの記録のフォルダ）"""
        cur = next(iter(reversed(jobs.analyses())), None)
        p = config.ANALYSES_DIR / cur["analysis_id"] / "fetch_log" / "pipeline.log" if cur else None
        if p and p.exists():
            subprocess.run(["open", "-t", str(p)])
        else:
            subprocess.run(["open", str(config.LOG_DIR)])

    def toggle_display(self, item):
        item.state = int(not item.state)
        self.ctl.set_keep_display(bool(item.state))

    def toggle_login_item(self, item):
        if not config.FROZEN:
            rumps.alert("ソースから動かしているときは使えません（アプリにしたときだけ）")
            return
        item.state = int(not item.state)
        system.set_login_item(bool(item.state))

    def quit_app(self, _):
        running = self.ctl.worker.running()
        if running:
            r = rumps.alert("取得の途中です", "終了すると取得は止まります。次に開いたとき続きから取ります。終了しますか？",
                            ok="終了する", cancel="やめる")
            if r != 1:
                return
        self.ctl.shutdown()
        system.release_single_instance()
        rumps.quit_application()


def run():
    log = config.logger()
    code = config.setup_env()
    log.info("=== %s %s を起動（%s、データ %s）", config.APP_NAME, VERSION, sys.executable, config.DATA_DIR)
    notify.setup(log)
    if not system.claim_single_instance():
        notify.send(config.APP_NAME, "すでに起動しています（メニューバーのアイコンから使えます）")
        return
    st = config.load_state()
    if config.FROZEN and not TEST_NO_CHROME:
        if system.is_translocated() or not system.in_applications():
            notify.send("アプリを「アプリケーション」に移してください",
                        "ダウンロードした場所のままだと、Mac を起動したときに自動で立ち上がりません")
        elif not st.get("first_run_done"):
            system.set_login_item(True)   # 初回は「ログイン時に起動」を入れておく（メニューで外せる）
        system.refresh_login_item()
    config.save_state(first_run_done=True)
    ctl = Controller(code, log)
    system.watch_sleep(ctl.on_sleep, ctl.on_wake)
    app = CollectorApp(ctl)
    ctl.start()
    app.run()
