"""メニューバーのアプリ（Rectangle と同じく、メニューバーのアイコンだけ。Dock にも画面にも出ない）。

画面の操作はメインのスレッド、取得の見張り（Chrome・ログイン・係・再開・通知）・知識ベースの新着・指示書の見張りは裏のスレッドで回す。
分析を頼むのは Claude か ChatGPT との会話（AI が mcp_local.py の道具で分析を作り、ここが5秒おきに見て拾う）。
"""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

from . import VERSION, chrome, claude_link, code_link, codex_link, config, jobs, notify, system, worker_entry

MUSIC_URL_RE = re.compile(r"^https://(www\.)?tiktok\.com/music/[^\s/]+-\d+")


def ai_where() -> str:
    """利用者が話しかける先（通知・案内の文。つないだ AI で出し分ける）。どちらもつないでいなければ空"""
    c = claude_link.status() == "connected"
    g = codex_link.status() == "connected"
    if c and g:
        return "Claude か ChatGPT の Work"
    return "Claude" if c else "ChatGPT の Work" if g else ""


CONNECT_HINT = "「Claude につなぐ」か「ChatGPT につなぐ」（使っている方）"
# 新しい分析を頼む言い方（名指しのときだけ始まる。2026-10-05 ユーザー）
START_PHRASE = "UGC Analyzer で〇〇／△△を分析して"

# 自動でつなぐ AI（キー・名前・つなぐ部品・入っているか・開いているか）
AI_LINKS = (("claude", "Claude", claude_link, "claude_installed", "claude_running"),
            ("chatgpt", "ChatGPT", codex_link, "chatgpt_installed", "chatgpt_running"))


def ai_installed() -> bool:
    return any(getattr(link, inst)() for _, _, link, inst, _ in AI_LINKS)


def autolink(log) -> tuple:
    """入っている AI に、まだつないでいなければつなぐ（2026-10-05 ユーザー「作る」。友達の手順から「〇〇につなぐ」を無くす。
    名指しのときだけ始まる決まりになったので、つないでもふだんの会話に道具が入り込まない）。
    AI ごとに1回だけ（state.json の autolinked）。あとで利用者が外したものは足し直さない。
    返すのは (つないだ AI の名前, そのうち今開いている AI の名前)"""
    st = config.load_state()
    done = dict(st.get("autolinked") or {})
    linked, running = [], []
    for key, name, link, inst, is_running in AI_LINKS:
        if done.get(key) or not getattr(link, inst)():
            continue
        if link.status() != "none":   # すでにつながっている（前の場所・ChatGPT で切られている、も含めて触らない）
            done[key] = "already"
            continue
        try:
            link.connect()
        except link.LinkError as e:
            log.warning("%s に自動でつなげませんでした（メニューから手でつなげます）: %s", name, e)
            done[key] = "error"
            continue
        log.info("%s に自動でつなぎました", name)
        done[key] = "linked"
        linked.append(name)
        if getattr(link, is_running)():
            running.append(name)
    if done != (st.get("autolinked") or {}):
        config.save_state(autolinked=done)
    return linked, running
# 試験用: Chrome とログインを飛ばす（TikTok に触らずにメニュー・係・通知だけを確かめる）
TEST_NO_CHROME = bool(os.environ.get("UGC_COLLECTOR_TEST_NO_CHROME"))
TICK = 5            # 見張りの間隔（秒）
RENDER_EVERY = 600  # 取得中に描画を確かめる間隔（秒）
RETRY_WAIT = 300    # 止まった取得を続きから取り直すまで（秒）
CHROME_IDLE_QUIT = 90   # 仕事が無くなってから取得用の Chrome を閉じるまで（秒）
KB_EVERY = 3600     # 知識ベースの新着を確かめる時期かを見る間隔（秒。実際に note を見るのは週1回）
PROMPTS_EVERY = 15  # 編集された指示書を確かめる間隔（秒）
POWER_EVERY = 60    # 取得の仕事があるあいだ、電源（電池で動いていないか）を確かめる間隔（秒）
LOW_BATTERY = 20    # 電池の残りがこれを下回ったら、もう一度知らせる（%）


def power_state() -> dict:
    """電源の状態: {"ac": 電源につながっているか, "percent": 電池の残り（電池の無い Mac は None）}"""
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {"ac": True, "percent": None}
    import re as _re
    m = _re.search(r"(\d+)%", out)
    return {"ac": "AC Power" in out, "percent": int(m.group(1)) if m else None}


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
        self.known_deepen = {}         # (分析 ID, 回) → 前に見た掘り下げの取り足しの状態
        self.next_kb = time.time() + 60
        self.kb_busy = False
        self.kb_text = ""
        self.next_prompts = 0.0
        self.prompt_rows = []          # 指示書の一覧（メニューに出す）
        self.prompt_seen = {}          # 指示書の名前 → 前に見た (mtime, 使えるか)
        self.awake = None              # 取得の仕事があるあいだ Mac を眠らせない（caffeinate。このアプリが生きている間）
        self.next_power = 0.0
        self.power_warned = None       # 電池で動いていると知らせた段階（None / "battery" / "low"）
        self.on_battery = False
        self.lock = threading.RLock()
        self.stopped = False

    # --- 起動・停止 ---
    def start(self):
        for m in jobs.analyses():
            self.known[m["analysis_id"]] = (m.get("acquisition") or {}).get("status")
            self.known_deepen[(m["analysis_id"], (m.get("deepen") or {}).get("round"))] = (m.get("deepen") or {}).get("status")
            # 自動の取り直しを待つ間にアプリが開き直された（取り直しの予定はメモリにしか無い）。開いたら取り直しを続ける
            if (m.get("acquisition") or {}).get("status") == "failed" and jobs.retriable(m):
                self._schedule_retry(m, restarted=True)
        self._watch_prompts(first=True)
        self._kb_refresh_text()
        threading.Thread(target=self._loop, name="collector", daemon=True).start()

    def shutdown(self):
        self.stopped = True
        self.worker.stop("アプリの終了")
        self.chrome.quit()
        self._hold_awake(False)

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
                        self._side_jobs()
                        work = self._work_pending()
                        self._hold_awake(work)
                        self._watch_power(work)
            except Exception as e:   # 見張りは止めない
                self.log.exception("見張りで例外: %s", e)
                self.status_text = f"エラー: {e}"
            time.sleep(TICK)

    # --- スリープさせない・電源（2026-10-03 ユーザー「Mac のスリープオフを導線に含められないか」） ---
    def _work_pending(self) -> bool:
        """取得の仕事が残っているか（取得中・順番待ち・止まって取り直し待ち・ログイン待ちのあいだ）。
        係のプロセスが生きている間だけ眠らせない作りだと、取り直しを待つ5分のあいだに Mac が眠り、朝まで続きが取られない"""
        if self.worker.running() or self.pending_retry:
            return True
        try:
            return any((m.get("acquisition") or {}).get("status") in ("queued", "running") or jobs.deepening(m)
                       for m in jobs.analyses())
        except Exception:
            return False

    def _hold_awake(self, on: bool):
        """仕事があるあいだだけ、Mac を自動で眠らせない（-i。画面は消えてよい。設定で -d も）。ふたを閉じたときは眠る"""
        alive = self.awake is not None and self.awake.poll() is None
        if on and not alive:
            flags = "-di" if self.worker.keep_display_on else "-i"
            self.awake = subprocess.Popen(["/usr/bin/caffeinate", flags, "-w", str(os.getpid())],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.log.info("取得の仕事があるので、Mac を自動で眠らせないようにしました（%s）", flags)
        elif not on and alive:
            self.awake.terminate()
            self.awake = None
            self.log.info("取得の仕事が無くなったので、Mac の眠りを元に戻しました")

    def _watch_power(self, work: bool):
        """取得の仕事があるのに電池で動いていたら知らせる（電池が切れたところで止まるため）"""
        if not work:
            self.power_warned, self.on_battery = None, False
            return
        if time.time() < self.next_power:
            return
        self.next_power = time.time() + POWER_EVERY
        ps = power_state()
        self.on_battery = not ps["ac"]
        if ps["ac"]:
            self.power_warned = None
            return
        pct = ps["percent"]
        if self.power_warned is None:
            self.power_warned = "battery"
            notify.send("電源につないでください",
                        f"取得の途中です。電池で動いていると、電池が切れたところで止まります（残り {pct}%）。ふたも閉じないでください")
            self.log.info("取得中に電池で動いている（残り %s%%）", pct)
        elif self.power_warned == "battery" and pct is not None and pct <= LOW_BATTERY:
            self.power_warned = "low"
            notify.send("電池が少なくなっています", f"残り {pct}%。電源につながないと、このあと取得が止まります")

    def _side_jobs(self):
        """取得とは別の見張り: 編集された指示書・知識ベースの新着"""
        if time.time() >= self.next_prompts:
            self.next_prompts = time.time() + PROMPTS_EVERY
            self._watch_prompts()
        if time.time() >= self.next_kb and not self.kb_busy:
            self.next_kb = time.time() + KB_EVERY
            try:
                import kb_update
                if kb_update.ready() and kb_update.due():
                    self.check_knowledge(quiet=True)
            except Exception as e:
                self.log.info("知識ベースの確かめができませんでした: %s", e)

    def _handle_cancels(self):
        """Claude の道具が「この取得をやめる」の印を置いたら、その分析を取っている係を止める（2026-10-04、取得のやり直し）。
        係を止めたあと、分析の状態を「やめた」に書き直す（係が最後に書いた状態で上書きされていることがあるため）"""
        for flag in config.LOCK_DIR.glob("cancel-*"):
            aid = flag.name[len("cancel-"):]
            p = config.ANALYSES_DIR / aid / "analysis.json"
            try:
                import json as _json
                m = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                flag.unlink(missing_ok=True)
                continue
            acq = m.get("acquisition") or {}
            wpid = self.worker.pid()
            # 係は1つで分析を順に取る。やめた分析が止まったあと、同じ係がもう別の分析を取っていれば、その係は止めない
            if wpid and acq.get("pid") == wpid and self.worker.running() and not self._busy_with_other(aid, wpid):
                self.worker.stop(f"利用者がやめた（{aid}）")
            if acq.get("status") != "cancelled":
                from acquire import pipeline
                acq.update({"status": "cancelled", "cancelled_at": acq.get("cancelled_at") or pipeline.now()})
                m["acquisition"] = acq
                pipeline.write_json(p, m)
            self.known[aid] = "cancelled"
            self.pending_retry.pop(aid, None)
            flag.unlink(missing_ok=True)
            self.log.info("取得をやめました（利用者の頼み）: %s", aid)
            notify.send(f"「{m.get('title') or aid}」の取得をやめました", "別の楽曲ページでやり直します")

    @staticmethod
    def _busy_with_other(aid: str, pid) -> bool:
        """同じ係（PID）が、ほかの分析を取っている最中か（取得の段か、掘り下げ・切り直しの取り足し）"""
        for o in jobs.analyses():
            if o["analysis_id"] == aid:
                continue
            for part in (o.get("acquisition") or {}, o.get("deepen") or {}):
                if part.get("status") == "running" and part.get("pid") == pid:
                    return True
        return False

    def _tick(self):
        self._handle_cancels()
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
                self.status_text = self._idle_text("（試験: Chrome なし）")
            return
        # 取得の係がコメントの段でログイン切れに気づいた（worker_entry.py が印を置いて待っている）。
        # 印を置いた係がもういない（止められた・落ちた）なら、印は古いものとして消える（login_waiting）
        if not self.login_wanted and worker_entry.login_waiting():
            config.save_state(logged_in_at=None)
            self.login_wanted, self.login_presented = True, False
        if self.login_wanted or not st.get("logged_in_at"):
            self._login_flow()
            return
        if self.worker.running():
            self.idle_since = None
            cur = next((m for m in jobs.analyses() if (m.get("acquisition") or {}).get("status") == "running"), None)
            dcur = None if cur else next((m for m in jobs.analyses() if (m.get("deepen") or {}).get("status") == "running"), None)
            self.status_text = jobs.describe(cur) if cur else jobs.describe_deepen(dcur) if dcur else "取得の準備中…"
            if (cur and (cur.get("acquisition") or {}).get("step") == "comments") or dcur:
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
        # 仕事が無い。自動では取り直さない止まった取得があれば、1行目に出す（友達向けの紙「『止まっています』と出ていたら」）
        failed = [m for m in jobs.analyses() if (m.get("acquisition") or {}).get("status") == "failed"]
        if failed:
            line = jobs.describe(failed[-1])
            line = line if len(line) <= 70 else line[:69] + "…"   # 止まった理由が長いとメニューの幅が広がりすぎる
            self.status_text = line + ("" if "続きから再開" in line else "（メニューの「止まった取得を続きから再開」）")
        else:
            self.status_text = self._idle_text()
        if self.chrome.listening():
            self.idle_since = self.idle_since or time.time()
            if time.time() - self.idle_since > CHROME_IDLE_QUIT and self.chrome.window_state() in ("minimized", "closed"):
                self.chrome.quit()   # 使わない間は取得用の Chrome を閉じておく（Dock のアイコンも消える。窓だけ閉じられたものも）
        else:
            self.idle_since = None

    def _idle_text(self, extra: str = "") -> str:
        where = ai_where()
        if not where and not ai_installed():
            return "準備OK・次は Claude か ChatGPT のアプリを入れてください（入れると自動でつなぎます）" + extra
        if not where:
            return f"準備OK・次はメニューの{CONNECT_HINT}を押してください" + extra
        return f"準備OK・待機中（{where} で「{START_PHRASE}」と言ってください）" + extra

    def _login_flow(self):
        """捨て垢のログインを待つ。ログインは利用者が自分で、取得用の Chrome の画面でする"""
        # Chrome が閉じている・窓だけ閉じられている（タブが0）なら開く。開いたら、ログインの画面を出し直す
        if self.chrome.ensure(url=chrome.LOGIN_URL, minimized=False):
            self.login_presented = False
        if not self.login_presented:
            if self.chrome.logged_in():   # すでにログイン済み（前にログインしたプロファイル）
                self.login_presented = True
            else:
                self.chrome.navigate(chrome.LOGIN_URL)
                self.chrome.show()
                notify.send("TikTok にログインしてください",
                            "開いた Chrome で、分析専用のサブアカウント（ふだんとは別のアカウント）でログインしてください")
                self.login_presented = True
            self.login_wanted = True
        if self.chrome.logged_in():
            config.save_state(logged_in_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            self.login_wanted = False
            self.login_presented = False
            # 係のログイン待ちの印もここで消す（係が気づいて消すのは最大10秒後。その間に見張りが印を見て、
            # ログインの流れをもう一度回し「準備OK」を2回出していた）。係は10秒おきにログインを確かめて続ける
            worker_entry.need_login_flag().unlink(missing_ok=True)
            self.chrome.minimize()
            self.log.info("TikTok のログインを確かめました")
            if ai_where():
                notify.send("準備OK", "ログインできました。取得用の Chrome は Dock にしまいます。このまま使えます")
            elif not ai_installed():
                notify.send("ログインできました", "最後に、Claude か ChatGPT のアプリを入れて、ログインしてください（入れると自動でつなぎます）")
            # AI のアプリが入っていれば、メニューのアプリがすぐ自動でつないで、開き直すかを聞く（CollectorApp._autolink）
            self.status_text = self._idle_text()
            return
        self.status_text = "TikTok のログイン待ち（取得用の Chrome で、サブアカウントでログイン）"

    def _check_transitions(self):
        """分析の状態が変わったら知らせる。止まったものは続きから取り直す"""
        for m in jobs.analyses():
            self._check_deepen(m)
            aid = m["analysis_id"]
            st = (m.get("acquisition") or {}).get("status")
            before = self.known.get(aid)
            self.known[aid] = st
            if st == before:
                continue
            title = m.get("title") or aid
            if st == "queued" and before is None:
                self.log.info("新しい取得を受け付けました: %s %s", aid, title)
                notify.send(f"「{title}」の取得を始めます",
                            "終わるまで Mac を開いたまま・電源につないでおいてください（画面は消えてもかまいません）")
            elif st == "done" and before is not None:
                c = (((m.get("acquisition") or {}).get("steps") or {}).get("comments") or {}).get("detail") or {}
                notify.send(f"「{title}」の取得が終わりました",
                            f"{ai_where() or 'Claude'} で「{title}の分析を続けて」と言ってください"
                            f"（コメント {c.get('videos_ok', '?')}本・{c.get('comments', '?')}件）")
                self.log.info("取得が終わりました: %s %s", aid, c)
            elif st == "failed" and before is not None:
                err = (m.get("acquisition") or {}).get("error") or ""
                self.log.warning("取得が止まりました: %s %s", aid, err)
                if "ログイン" in err:
                    config.save_state(logged_in_at=None)
                if jobs.retriable(m):
                    wait = self._schedule_retry(m)
                    if wait > 30:
                        notify.send(f"「{title}」の取得が止まりました",
                                    f"{wait // 60}分後に続きから取り直します（{err[:60]}）")
                else:
                    notify.send(f"「{title}」の取得が止まりました",
                                f"{err[:80]}。メニューの「止まった取得を続きから再開」で続きから取れます")

    def _schedule_retry(self, m: dict, restarted: bool = False) -> int:
        """止まった取得を、少し待ってから続きから取り直す予定に入れる。待つ秒数を返す。
        Chrome が閉じられた・つながらない、はすぐ取り直す。それ以外（TikTok 側など）は少し空ける。
        restarted（アプリを開き直したとき）は、止まった時刻から数えて待ち、少なくとも30秒はネットがつながるのを待つ"""
        err = (m.get("acquisition") or {}).get("error") or ""
        quick = any(k in err for k in ("Chrome", "WebDriver", "接続できません", "session", "Session"))
        wait = 30 if quick else RETRY_WAIT
        when = time.time() + wait
        if restarted:
            try:
                import datetime
                failed_at = datetime.datetime.fromisoformat((m.get("acquisition") or {}).get("failed_at")).timestamp()
                when = max(time.time() + 30, failed_at + wait)
            except (TypeError, ValueError):
                pass
            self.log.info("止まっていた取得を、続きから取り直す予定に入れました（アプリを開き直した）: %s", m["analysis_id"])
        self.pending_retry[m["analysis_id"]] = when
        return wait

    def _check_deepen(self, m: dict):
        """完成後の界隈の掘り下げの取り足し（acquire/deepen.py）が終わったら知らせる"""
        dp = m.get("deepen") or {}
        key = (m["analysis_id"], dp.get("round"))
        st = dp.get("status")
        before = self.known_deepen.get(key)
        self.known_deepen[key] = st
        if st == before or before is None and st in ("done", "failed"):   # 起動したときにもう済んでいたものは知らせない
            return
        title = m.get("title") or m["analysis_id"]
        recut = dp.get("kind") == "recut"   # 界隈の切り直しの取り足し（acquire/recut.py）。取り方は掘り下げと同じ
        what = "界隈の切り直し" if recut else "界隈の掘り下げ"
        if st == "queued" and before is None:
            notify.send(f"「{title}」のコメントを取り足します",
                        f"{'切り直した界隈' if recut else '界隈 ' + str(dp.get('community'))}・約{int(round(float(dp.get('est_min') or 0)))}分。"
                        "Mac を開いたままにしておいてください")
        elif st == "done" and ((dp.get("result") or {}).get("error") or (dp.get("result") or {}).get("blocked")):
            notify.send(f"「{title}」の取り足しが途中で止まりました",
                        f"{ai_where() or 'Claude'} で「{title}の分析を続けて」と言うと、取れた分をレポートに活かします")
            self.log.warning("%sの取り足しが途中で止まりました: %s %s", what, m["analysis_id"], dp.get("result"))
        elif st == "done":
            r = dp.get("result") or {}
            notify.send(f"「{title}」の取り足しが終わりました",
                        f"{ai_where() or 'Claude'} で「{title}の分析を続けて」と言うと、"
                        + (f"新しい界隈の分け方でレポートを書き直します（{r.get('videos_new', 0)}本、コメント{r.get('comments_added', 0)}件）"
                           if recut else
                           f"レポートに活かします（新しく{r.get('videos_new', 0)}本・続き{r.get('videos_more', 0)}本、コメント{r.get('comments_added', 0)}件）"))
            self.log.info("%sの取り足しが終わりました: %s %s", what, m["analysis_id"], r)
        elif st == "failed":
            notify.send(f"「{title}」の取り足しが止まりました",
                        f"{ai_where() or 'Claude'} で「{title}の分析を続けて」と言えば、手元のコメントで"
                        + ("レポートを書き直します" if recut else "掘り下げを進めます"))
            self.log.warning("%sの取り足しが止まりました: %s %s", what, m["analysis_id"], dp.get("error"))

    def _keep_chrome(self):
        """コメントの段で取得用の Chrome が閉じられたら、すぐ最小化で開き直す（2026-10-02 ユーザー）。
        取りかけの1本は失敗になるが、係が続きから取り直す（止まった場合はアプリが30秒後に続きから）。
        窓を赤い×で閉じただけ（Chrome は動いたままタブが0）も、閉じられたとみなして窓を開き直す"""
        if not self.chrome.ensure(minimized=True):
            return   # 開いていて、窓もある
        self.log.info("取得中に取得用の Chrome（の窓）が閉じられたので、最小化で開き直しました")
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

    # --- 指示書（段3） ---
    def _watch_prompts(self, first: bool = False):
        """編集された指示書が使えるかを見て、使えないものを知らせる（使えない指示書は初期の指示書で動く）"""
        try:
            import prompt_store
            rows = prompt_store.status()
        except Exception as e:
            self.log.info("指示書の一覧を読めませんでした: %s", e)
            return
        for r in rows:
            try:
                mt = Path(r["path"]).stat().st_mtime
            except FileNotFoundError:
                mt = 0
            before = self.prompt_seen.get(r["name"])
            self.prompt_seen[r["name"]] = (mt, r["ok"])
            if first or before is None or before[0] == mt:
                continue
            if not r["ok"]:
                notify.send(f"指示書「{r['title']}」は使えません",
                            f"{r['problems'][0][:70]}。直すまでは初期の指示書を使います")
                self.log.warning("編集された指示書が使えません: %s %s", r["name"], r["problems"])
            else:
                self.log.info("指示書が編集されました: %s", r["name"])
        self.prompt_rows = rows

    # --- 知識ベース（段4） ---
    def _kb_refresh_text(self):
        try:
            import kb_update
            self.kb_text = kb_update.summary()
        except Exception as e:
            self.kb_text = f"知識ベース: 読めません（{e}）"

    def check_knowledge(self, quiet: bool = False):
        """著者の note の新着を見る（裏で）。新しい記事は、次に AI に「〇〇の分析を続けて」と言ったときに取り込む"""
        if self.kb_busy:
            return

        def go():
            self.kb_busy = True
            try:
                import kb_update
                res = kb_update.check_new(log=self.log.info)
                n = len(res["new"])
                if n:
                    notify.send("知識ベースに新しい記事があります",
                                f"著者の note の新しい記事 {n} 本。次に {ai_where() or 'Claude'} で「〇〇の分析を続けて」と言ったときに取り込みます"
                                "（分析が無ければ「UGC Analyzer の知識ベースを更新して」）")
                elif not quiet:
                    notify.send("知識ベースは最新です", "著者の note に新しい記事はありませんでした")
            except Exception as e:
                self.log.warning("知識ベースの新着を確かめられませんでした: %s", e)
                if not quiet:
                    notify.send("知識ベースを確かめられませんでした", f"ネットにつながっているか確かめてください（{type(e).__name__}）")
            finally:
                self.kb_busy = False
                self._kb_refresh_text()

        threading.Thread(target=go, daemon=True).start()

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
        self.worker.keep_display_on = on
        if self.awake is not None and self.awake.poll() is None:
            self._hold_awake(False)
            self._hold_awake(True)


class CollectorApp(rumps.App):
    def __init__(self, ctl: Controller):
        icon = _asset("menubar.png")
        super().__init__(config.APP_NAME, icon=icon if Path(icon).exists() else None, template=True,
                         quit_button=None)
        if not Path(icon).exists():
            self.title = "UGC"
        self.ctl = ctl
        self._autolink_at = 0.0
        self._autolinking = False
        self.status_item = rumps.MenuItem("起動中…")
        self.claude_item = rumps.MenuItem("Claude につなぐ", callback=self.connect_claude)
        self.chatgpt_item = rumps.MenuItem("ChatGPT につなぐ", callback=self.connect_chatgpt)
        self.kb_item = rumps.MenuItem("知識ベース: …")
        self.keep_display = rumps.MenuItem("取得中は画面を消さない", callback=self.toggle_display)
        self.keep_display.state = int(ctl.worker.keep_display_on)
        self.login_item = rumps.MenuItem("ログイン時に起動", callback=self.toggle_login_item)
        self.login_item.state = int(system.login_item_enabled())

        self.prompt_menu = rumps.MenuItem("指示書を編集")
        self.prompt_items = {}
        try:
            import prompt_store
            titles = prompt_store.TITLES
        except Exception:
            titles = {}
        for name, title in titles.items():
            item = rumps.MenuItem(title, callback=self._open_prompt_cb(name))
            self.prompt_items[name] = item
            self.prompt_menu.add(item)
        self.prompt_menu.add(None)
        self.prompt_menu.add(rumps.MenuItem("指示書のフォルダを開く", callback=self.open_prompts_dir))
        self.prompt_menu.add(rumps.MenuItem("すべて初期の指示書に戻す…", callback=self.reset_prompts))

        kb_menu = rumps.MenuItem("知識ベース")
        kb_menu.add(self.kb_item)
        kb_menu.add(rumps.MenuItem("今すぐ新しい記事を確かめる", callback=self.check_kb))
        kb_menu.add(rumps.MenuItem("知識ベースのフォルダを開く", callback=lambda _: subprocess.run(["open", str(config.KB_DIR)])))

        other = rumps.MenuItem("そのほか")
        other.add(self.keep_display)
        other.add(self.login_item)
        other.add(None)
        other.add(rumps.MenuItem("データのフォルダを開く", callback=self.open_data))
        other.add(rumps.MenuItem("取得の記録を開く", callback=self.open_logs))

        items = [
            self.status_item,
            None,
            rumps.MenuItem("止まった取得を続きから再開", callback=self.resume_failed),
            rumps.MenuItem("取得の画面を見る", callback=self.show_chrome),
            rumps.MenuItem("取得の画面をしまう", callback=self.hide_chrome),
            rumps.MenuItem("TikTok にログイン", callback=self.login),
            None,
            self.claude_item,
            self.chatgpt_item,
            rumps.MenuItem("レポートのフォルダを開く", callback=self.open_reports),
            self.prompt_menu,
            kb_menu,
            None,
            other,
        ]
        if config.OPERATOR_FLAG.exists():   # 運営の Mac だけ（友達には出さない）
            op = rumps.MenuItem("運営向け")
            op.add(rumps.MenuItem("曲を取得する（本番の規模）…", callback=self.add_full))
            op.add(rumps.MenuItem("ちょいとり（試し・約5分）…", callback=self.add_trial))
            self.ai_trial = rumps.MenuItem("AI（Claude・ChatGPT）から頼んだ取得を、ちょいとりの規模にする",
                                           callback=self.toggle_ai_trial)
            self.ai_trial.state = int(bool(config.load_state().get("ai_trial")))
            op.add(self.ai_trial)
            items.append(op)
        items += [None, rumps.MenuItem(f"{config.APP_NAME} {VERSION}"), rumps.MenuItem("終了", callback=self.quit_app)]
        self.menu = items
        rumps.Timer(self.refresh, 2).start()

    def refresh(self, _):
        self._autolink_tick()
        t = self.ctl.status_text + ("（電池で動作中・電源につないでください）" if self.ctl.on_battery else "")
        if self.status_item.title != t:
            self.status_item.title = t
        st = claude_link.status()
        title = "Claude につながっています" if st == "connected" else "Claude につなぐ"
        if self.claude_item.title != title:
            self.claude_item.title = title
        self.claude_item.state = int(st == "connected")
        gst = codex_link.status()
        title = "ChatGPT につながっています" if gst == "connected" else "ChatGPT につなぐ"
        if self.chatgpt_item.title != title:
            self.chatgpt_item.title = title
        self.chatgpt_item.state = int(gst == "connected")
        kb ="知識ベース: " + (self.ctl.kb_text or "…")
        if self.kb_item.title != kb:
            self.kb_item.title = kb
        for r in self.ctl.prompt_rows:
            item = self.prompt_items.get(r["name"])
            if not item:
                continue
            want = ("★ " if not r["ok"] else "") + r["title"] + ("（編集済み）" if r["edited"] else "")
            if item.title != want:
                item.title = want

    # --- 自動でつなぐ（2026-10-05。友達は「〇〇につなぐ」を押さなくてよい。メニューの項目は手でつなぎ直すときのため） ---
    def _autolink_tick(self):
        """TikTok のログインが済んだら、入っている AI に自動でつなぎ、開いている AI を開き直すかを聞く（10秒おきに見る）"""
        if self._autolinking or time.time() - self._autolink_at < 10:
            return
        self._autolink_at = time.time()
        if not config.FROZEN or system.is_translocated() or not system.in_applications():
            return   # 置き場所が決まっていないと、AI から呼べる場所を書けない
        if self.ctl.login_wanted or not config.load_state().get("logged_in_at"):
            return   # ログインの最中に別の画面を出さない
        self._autolinking = True
        try:
            linked, running = autolink(self.ctl.log)
            if not linked:
                return
            names = "と".join(linked)
            say = f"使うときは、{ai_where()} で「{START_PHRASE}」と言ってください。"
            if not running:
                notify.send(f"{names} につなぎました", say)
                return
            r = rumps.alert(f"{names} につなぎました",
                            f"{'と'.join(running)} を開き直すと、UGC Analyzer が使えるようになります。今すぐ開き直しますか？\n"
                            "（話している途中の会話は保存されています）\n\n" + say,
                            ok="開き直す", cancel="あとで自分で")
            if r == 1:
                for n in running:
                    threading.Thread(target=self._restart_claude if n == "Claude" else self._restart_chatgpt,
                                     daemon=True).start()
        finally:
            self._autolinking = False

    # --- Claude につなぐ（段2） ---
    def connect_claude(self, _):
        if config.FROZEN and (system.is_translocated() or not system.in_applications()):
            rumps.alert("先にアプリを「アプリケーション」フォルダに移してください",
                        "ダウンロードした場所のままだと、Claude からこのアプリを呼べません。"
                        f"移したら、アプリケーションフォルダの「{config.APP_NAME}」を開き直してください。")
            return
        if not claude_link.claude_installed():
            rumps.alert("Claude のアプリが見つかりません",
                        "Claude デスクトップアプリ（https://claude.ai/download）を入れて、ログインしてから、もう一度押してください。")
            return
        try:
            backup = claude_link.connect()
        except claude_link.LinkError as e:
            rumps.alert("Claude につなげませんでした", str(e))
            return
        self.ctl.log.info("Claude の設定に道具を足しました（控え: %s）", backup or "元の設定ファイル無し")
        r = rumps.alert("Claude につなぎました",
                        "Claude を開き直すと使えるようになります。今すぐ開き直しますか？\n"
                        "（話している途中の会話は保存されています）",
                        ok="開き直す", cancel="あとで自分で")
        if r == 1:
            threading.Thread(target=self._restart_claude, daemon=True).start()

    def _restart_claude(self):
        if claude_link.restart_claude():
            notify.send("Claude を開き直しました", f"Claude で「{START_PHRASE}」と言ってみてください")
        else:
            notify.send("Claude を閉じられませんでした", "Claude を自分で終了（⌘Q）して、開き直してください")

    # --- ChatGPT につなぐ（2026-10-04。ChatGPT の Mac アプリの Work の画面で使う） ---
    def connect_chatgpt(self, _):
        if config.FROZEN and (system.is_translocated() or not system.in_applications()):
            rumps.alert("先にアプリを「アプリケーション」フォルダに移してください",
                        "ダウンロードした場所のままだと、ChatGPT からこのアプリを呼べません。"
                        f"移したら、アプリケーションフォルダの「{config.APP_NAME}」を開き直してください。")
            return
        if not codex_link.chatgpt_installed():
            rumps.alert("ChatGPT のアプリが見つかりません",
                        "ChatGPT の Mac アプリ（https://openai.com/chatgpt/download/）を入れて、ログインしてから、もう一度押してください。")
            return
        try:
            backup = codex_link.connect(enable=True)   # 押したのは利用者。ChatGPT の設定で切ってあっても入れ直す
        except codex_link.LinkError as e:
            rumps.alert("ChatGPT につなげませんでした", str(e))
            return
        self.ctl.log.info("ChatGPT（Codex）の設定に道具を足し、スキルを置きました（控え: %s）", backup or "元の設定ファイル無し")
        r = rumps.alert("ChatGPT につなぎました",
                        "ChatGPT を開き直すと使えるようになります。今すぐ開き直しますか？\n"
                        "（話している途中の会話は保存されています）\n\n"
                        f"使うときは、ChatGPT の「Work」で「{START_PHRASE}」と言ってください。",
                        ok="開き直す", cancel="あとで自分で")
        if r == 1:
            threading.Thread(target=self._restart_chatgpt, daemon=True).start()

    def _restart_chatgpt(self):
        if codex_link.restart_chatgpt():
            notify.send("ChatGPT を開き直しました", f"ChatGPT の Work で「{START_PHRASE}」と言ってみてください")
        else:
            notify.send("ChatGPT を閉じられませんでした", "ChatGPT を自分で終了（⌘Q）して、開き直してください")

    # --- 指示書（段3） ---
    def _open_prompt_cb(self, name):
        def cb(_):
            p = config.PROMPTS_DIR / "w1" / name
            if not p.exists():
                try:
                    import prompt_store
                    prompt_store.seed()
                except Exception:
                    pass
            subprocess.run(["open", "-e", str(p)])   # テキストエディットで開く。保存すれば次の仕事から使われる
        return cb

    def open_prompts_dir(self, _):
        subprocess.run(["open", str(config.PROMPTS_DIR)])

    def reset_prompts(self, _):
        r = rumps.alert("すべて初期の指示書に戻しますか？", "編集した指示書は全部、初期の中身に戻ります。",
                        ok="戻す", cancel="やめる")
        if r != 1:
            return
        import prompt_store
        prompt_store.reset()
        notify.send("指示書を初期に戻しました", "次の仕事から初期の指示書を使います")

    def check_kb(self, _):
        self.ctl.check_knowledge(quiet=False)

    # --- 曲を頼む（運営向け。ふだんは Claude との会話で頼む） ---
    def _ask_song(self, trial: bool):
        w = rumps.Window(
            message=("曲名とアーティスト名を「曲名 / アーティスト名」の形で入れてください。\n"
                     "TikTok の楽曲ページの URL（https://www.tiktok.com/music/…）でもかまいません。"
                     + ("\n\nちょいとり: 動画20本の一覧と、2本のコメント（各30件）だけ取ります。約5分です。" if trial else
                        "\n\n本番の規模: 終わるまで Mac を開いたまま、電源につないでおいてください。")),
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

    def toggle_ai_trial(self, item):
        item.state = int(not item.state)
        config.save_state(ai_trial=bool(item.state))

    # --- 取得用の Chrome ---
    def show_chrome(self, _):
        def go():
            if not self.ctl.chrome.has_tab():   # 窓だけ閉じられている（タブが0）ときも、出せる画面は無い
                notify.send("取得用の Chrome は閉じています", "コメントを取る段と、ログインのときだけ開きます")
                return
            self.ctl.chrome.show()
        threading.Thread(target=go, daemon=True).start()

    def hide_chrome(self, _):
        threading.Thread(target=lambda: self.ctl.chrome.has_tab() and self.ctl.chrome.minimize(), daemon=True).start()

    def login(self, _):
        threading.Thread(target=self.ctl.request_login, daemon=True).start()

    # --- そのほか ---
    def open_reports(self, _):
        config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(config.REPORTS_DIR)])

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


def version_notice(st: dict, log) -> None:
    """アプリを新しくしたあと初めて開いたとき（版が変わった）、開いている Claude（ChatGPT）を開き直すよう1回だけ知らせる。
    AI の道具（--mcp）は Claude（ChatGPT）が起こしたまま動き続けるので、開き直すまで前の版の中身のまま
    （.app を置き換えたあとは、前の版の道具が部品を読めずに止まることもある）。st は起動したときの state.json"""
    if st.get("app_version") == VERSION:
        return
    config.save_state(app_version=VERSION)
    if not st.get("first_run_done"):
        return   # 初めて入れた（前の版は無い）
    names = [name for _, name, link, _, is_running in AI_LINKS
             if link.status() == "connected" and getattr(link, is_running)()]
    log.info("版が変わりました: %s → %s（開いている AI: %s）", st.get("app_version") or "前の版", VERSION, "、".join(names) or "なし")
    if names:
        notify.send(f"{config.APP_NAME} を {VERSION} にしました",
                    f"{'と'.join(names)} を開き直してください。開き直すと、新しい版の UGC Analyzer が使えます（話している途中の会話は保存されています）")


def run():
    log = config.logger()
    code = config.setup_env()
    log.info("=== %s %s を起動（%s、データ %s）", config.APP_NAME, VERSION, sys.executable, config.DATA_DIR)
    notify.setup(log)
    if not system.claim_single_instance():
        notify.send(config.APP_NAME, "すでに起動しています（メニューバーのアイコンから使えます）")
        return
    config.seed_data(log)
    st = config.load_state()
    if config.FROZEN and not TEST_NO_CHROME:
        if system.is_translocated() or not system.in_applications():
            notify.send("アプリを「アプリケーション」に移してください",
                        "ダウンロードした場所のままだと、Mac を起動したときに自動で立ち上がらず、Claude や ChatGPT からも呼べません")
        else:
            if not st.get("first_run_done"):
                # 初回は「ログイン時に起動」を入れておく（メニューで外せる）。初回の印はここで立てる
                # （.dmg の窓・ダウンロードのまま開いた1回目で立てると、アプリケーションから開いたときに入らない）
                system.set_login_item(True)
                config.save_state(first_run_done=True)
            system.refresh_login_item()
            if claude_link.status() == "outdated":   # アプリの場所が変わった（入れ直した）ら、Claude の設定も直す
                try:
                    claude_link.connect()
                    log.info("Claude の設定を、今のアプリの場所に直しました")
                except claude_link.LinkError as e:
                    log.warning("Claude の設定を直せませんでした: %s", e)
            if codex_link.status() == "outdated":   # アプリの場所が変わった・スキルが古い（アプリを新しくした）
                try:
                    codex_link.connect()
                    log.info("ChatGPT（Codex）の設定とスキルを、今のアプリに合わせて直しました")
                except codex_link.LinkError as e:
                    log.warning("ChatGPT の設定を直せませんでした: %s", e)
            if claude_link.claude_installed():   # Claude の Code タブ用の作業フォルダ（~/UGC Analyzer。docs/CODE_TAB_ONE_SITTING.md）
                try:
                    ch = code_link.ensure()
                    if ch:
                        log.info("Code タブ用の作業フォルダ（%s）を、今のアプリに合わせて整えました: %s", code_link.FOLDER, "、".join(ch))
                except Exception as e:   # 作業フォルダの中身が想定と違っても、アプリの起動は止めない（記録だけ）
                    log.warning("Code タブ用の作業フォルダを整えられませんでした: %s", e)
            version_notice(st, log)
    ctl = Controller(code, log)
    system.watch_sleep(ctl.on_sleep, ctl.on_wake)
    app = CollectorApp(ctl)
    ctl.start()
    app.run()
