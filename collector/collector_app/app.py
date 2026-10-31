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
from PyObjCTools import AppHelper

from . import VERSION, chrome, claude_link, code_link, codex_link, config, jobs, notify, system, updater, worker_entry

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
        # 新しい版（0.9.0。updater.py・docs/AUTO_UPDATE.md）
        self.next_update = time.time() + updater.FIRST_CHECK
        self.update_thread = None
        self.update_phase = None       # 見に行っている（"checking"）・落として確かめている（"downloading"）
        self.update_found = None       # 落として確かめている版
        self.update_staged = updater.staged_from_state()   # 入れる準備ができた版（{"version", "app", "note"}）
        self.update_busy = None        # 入れるのを待っている理由（"取得"・"AI の作業"）
        self.update_manual = False     # メニューの「アップデートを確認…」から頼まれた（結果を窓で知らせる）
        self.update_asking = False     # 窓で聞いている最中
        self.update_requests = []      # 画面のスレッドに出してほしい窓（CollectorApp._update_ui_tick が出す）
        self.update_go = None          # 黙って入れ替えてほしい（{"mode", "restart"}。画面のスレッドがアプリを終える）
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
        self.next_update = min(self.next_update, time.time() + updater.FIRST_CHECK)
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
        if time.time() >= self.next_update:
            self.next_update = time.time() + updater.CHECK_EVERY
            self.check_update(manual=False)
        self._update_tick()

    # --- 新しい版（0.9.0。updater.py・docs/AUTO_UPDATE.md） ---
    def check_update(self, manual: bool) -> bool:
        """新しい版を見に行く（裏のスレッドで。見つけたら落として確かめる）。もう見に行っている途中なら False。
        manual はメニューの「アップデートを確認…」（無かった・確かめられなかったも窓で知らせる。用意できたら窓で聞く）"""
        if not config.FROZEN and not os.environ.get("UGC_UPDATE_API"):
            return False   # ソースから動かしているときは見ない（試験は UGC_UPDATE_API で手元の偽物に向ける）
        if manual:
            self.update_manual = True
        if self.update_thread is not None and self.update_thread.is_alive():
            return False
        self.update_phase = "checking"
        self.update_thread = threading.Thread(target=self._check_update, name="update", daemon=True)
        self.update_thread.start()
        return True

    def _check_update(self):
        try:
            rel = updater.latest_release()
            updater.save(checked_at=updater.now_iso(), check_error=None)
            if not rel or not updater.is_newer(rel["version"]):
                self.log.info("新しい版はありません（いまの版 %s・最新 %s）", VERSION, rel["version"] if rel else "なし")
                if self.update_manual and not self.update_staged:
                    self.update_manual = False
                    self.update_requests.append({"kind": "latest"})
                return
            if self.update_staged and self.update_staged["version"] == rel["version"]:
                return   # もう用意してある（入れるかは _update_tick）
            self.update_found = rel["version"]
            self.update_phase = "downloading"
            self.log.info("新しい版 %s を見つけました（%s）。落として確かめます", rel["version"],
                          "試し版" if rel["prerelease"] else "友達向け")
            staged = updater.stage(rel, self.log)
            updater.save(staged=staged)
            self.update_staged = staged
            self.log.info("新しい版 %s を入れる準備ができました", staged["version"])
        except Exception as e:
            self.log.warning("新しい版を用意できませんでした: %s: %s", type(e).__name__, e)
            try:
                updater.save(check_error=f"{type(e).__name__}: {e}"[:300])
            except Exception:
                pass
            if self.update_manual and not self.update_staged:
                self.update_manual = False
                self.update_requests.append({"kind": "error"})
        finally:
            self.update_found = None
            self.update_phase = None

    def ai_open(self) -> list:
        """開いていて、UGC Analyzer につないである AI のアプリの名前（入れ替えるときに開き直すもの）"""
        out = []
        for _, name, link, _, is_running in AI_LINKS:
            try:
                if link.status() == "connected" and getattr(link, is_running)():
                    out.append(name)
            except Exception:
                pass
        return out

    def update_wait_reason(self) -> str | None:
        """いま入れ替えられない理由（取得・取り足し・順番待ち・取り直しの予定・TikTok のロック → "取得"、AI の作業 → "AI の作業"）"""
        if self._work_pending() or (config.LOCK_DIR / "tiktok.lock").exists():
            return "取得"
        if updater.ai_active():
            return "AI の作業"
        return None

    def _update_tick(self):
        """用意した版を入れ替えるか（見張りの5秒おき。決めるのは updater.decide）"""
        s = self.update_staged
        if not s:
            self.update_busy = None
            return
        if self.update_phase == "downloading" or self.update_go or self.update_asking:
            return
        if not updater.is_newer(s["version"]):   # 手で同じ版以上を入れた
            self.update_staged = None
            updater.save(staged=None)
            return
        self.update_busy = self.update_wait_reason()
        manual = self.update_manual
        ai = [] if self.update_busy else self.ai_open()
        act = updater.decide(s, self.update_busy, ai, config.load_state().get("update") or {}, manual=manual)
        if act == "wait" and manual:
            self.update_manual = False
            self.update_requests.append({"kind": "wait", "version": s["version"], "why": self.update_busy})
        elif act == "silent":
            self.log.info("Claude・ChatGPT が閉じていて作業中でもないので、黙って %s に入れ替えます", s["version"])
            self.update_go = {"mode": "silent", "restart": []}
        elif act == "ask":
            self.update_manual = False
            if not manual:
                updater.save(asked_on=updater.today())   # 自分から聞くのは1日1回まで
            self.update_asking = True
            self.update_requests.append({"kind": "ask", "version": s["version"], "note": s.get("note"), "ai": ai})

    def update_answer(self, now: bool, ai: list | None = None) -> tuple:
        """窓の答え（画面のスレッドから）。("go", {"mode", "restart"})・("wait", 理由)・("later", None)。ai は窓を出したときに開いていた AI"""
        self.update_asking = False
        if not now:
            # 「あとで」: 今日はもう聞かない。AI を開いていたなら、閉じたときに黙ってアップデートする（窓でそう伝えた）。
            # AI を開いていないとき（メニューから押した）に断ったなら、今日はもう自分からはアップデートしない
            updater.save(asked_on=updater.today(), **({} if ai else {"declined_on": updater.today()}))
            self.log.info("アップデートは「あとで」になりました（%s）", "AI を閉じたときに" if ai else "今日はしない")
            return "later", None
        why = self.update_wait_reason()
        if why:
            return "wait", why
        return "go", {"mode": "now", "restart": self.ai_open()}

    def _handle_cancels(self):
        """Claude の道具が「この取得をやめる」の印を置いたら、その分析を取っている係を止める（2026-10-04、取得のやり直し）。
        係を止めたあと、分析の状態を「やめた」に書き直す（係が最後に書いた状態で上書きされていることがあるため）。

        掘り下げ・切り直しの取り足し（analysis.json の deepen）もやめられる（2026-10-12。前は取得が済んだ分析でも取得の状態を
        「やめた」に書き直していた）。道具は deepen に status=cancelled・cancelled_at を書いてから同じ印を置く。見分け方:
          - 取得（acquisition）が順番待ち・取得中でなく、deepen が順番待ち・取得中か、道具が書いた cancelled_at がある → 取り足しをやめる
            （係の deepen.Run は自分の項目だけを足して書くので、上書きされても cancelled_at は残る）
          - それ以外 → 取得をやめる（今までどおり）。ただし取得が済んでいる（done）なら、取得の状態は書き換えない。
            例外は、道具がやめると書いた（cancelled_at）直後に係が取り終えて done で上書きしたとき（係は項目を足して書くので
            cancelled_at が残る。そのあと取り直していない）。これは「やめた」に戻す（取り足しと同じ考え方）
        係を止めるのは、係がこの分析を取っているとき（取得なら acquisition.pid、取り足しなら deepen.pid が係の PID）だけ"""
        import json as _json
        from acquire import pipeline
        for flag in config.LOCK_DIR.glob("cancel-*"):
            aid = flag.name[len("cancel-"):]
            p = config.ANALYSES_DIR / aid / "analysis.json"
            try:
                m = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                flag.unlink(missing_ok=True)
                continue
            acq = m.get("acquisition") or {}
            dp = m.get("deepen") or {}
            for_deepen = acq.get("status") not in ("queued", "running") and bool(dp) and \
                (dp.get("status") in ("queued", "running") or bool(dp.get("cancelled_at")))
            part = dp if for_deepen else acq
            overwritten = not for_deepen and acq.get("status") == "done" and bool(acq.get("cancelled_at")) and \
                str(acq.get("cancelled_at")) > str(acq.get("requeued_at") or "")
            wpid = self.worker.pid()
            # 係は1つで分析を順に取る。やめた分析が止まったあと、同じ係がもう別の分析を取っていれば、その係は止めない
            # （取得はやめた印より前に係が書いた running のまま・取り足しは道具が cancelled に書いたあとのこともあるので、PID で見る）
            if wpid and part.get("pid") == wpid and (part.get("status") != "done" or overwritten) and self.worker.running() \
                    and not self._busy_with_other(aid, wpid):
                self.worker.stop(f"利用者がやめた（{aid}{'・取り足し' if for_deepen else ''}）")
            # 係が止まる前に書いた状態を読み直してから直す（止める前に読んだものを書き戻すと、係が書いた進みを消す）
            m = pipeline.read_json(p, m) or m
            title = m.get("title") or aid
            if for_deepen:
                dp = m.get("deepen") or {}
                if dp.get("status") != "cancelled":
                    dp.update({"status": "cancelled", "cancelled_at": dp.get("cancelled_at") or pipeline.now(),
                               "cancel_reason": dp.get("cancel_reason") or "利用者がやめた"})
                    m["deepen"] = dp
                    pipeline.write_json(p, m)
                self.known_deepen[(aid, dp.get("round"))] = "cancelled"
                flag.unlink(missing_ok=True)
                self.log.info("取り足しをやめました（利用者の頼み）: %s %s回目", aid, dp.get("round"))
                notify.send(f"「{title}」のコメントの取り足しをやめました", "レポートは今のまま読めます")
                continue
            acq = m.get("acquisition") or {}
            if acq.get("status") != "cancelled" and (acq.get("status") != "done" or overwritten):   # 済んだ取得は「やめた」にしない
                acq.update({"status": "cancelled", "cancelled_at": acq.get("cancelled_at") or pipeline.now()})
                m["acquisition"] = acq
                pipeline.write_json(p, m)
            self.known[aid] = acq.get("status")
            self.pending_retry.pop(aid, None)
            flag.unlink(missing_ok=True)
            if acq.get("status") == "done":
                self.log.info("やめる印がありましたが、取得はもう済んでいます（そのまま）: %s", aid)
                continue
            self.log.info("取得をやめました（利用者の頼み）: %s", aid)
            # 取り直しでやめたのか、ただ「やめて」と言われたのかで文を分ける（2026-10-12）
            again = m.get("superseded_by")
            notify.send(f"「{title}」の取得をやめました",
                        "新しい分析で取り直します" if again else "また集めるときは、Claude（ChatGPT）に頼んでください")

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
            region = "日本の地域では使えません" in str((failed[-1].get("acquisition") or {}).get("error") or "")
            self.status_text = line + ("" if "続きから再開" in line or region else "（メニューの「止まった取得を続きから再開」）")
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
                elif "日本の地域では使えません" in err:   # 再開しても同じ所で止まる。止まった文（ほかのページで頼み直す案内）を切らずに出す
                    notify.send(f"「{title}」の取得が止まりました", err)
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
        """完成後の取り足し（acquire/deepen.py。界隈の掘り下げ・切り直し・動画のコメント・動画の追加）が終わったら知らせる。
        動画を外す・戻す（exclude・include）は取得が無い（積んだらすぐ済む）ので知らせない"""
        dp = m.get("deepen") or {}
        key = (m["analysis_id"], dp.get("round"))
        st = dp.get("status")
        before = self.known_deepen.get(key)
        self.known_deepen[key] = st
        if st == before or before is None and st in ("done", "failed"):   # 起動したときにもう済んでいたものは知らせない
            return
        kind = dp.get("kind") or "deepen"
        if kind in ("exclude", "include"):
            return
        title = m.get("title") or m["analysis_id"]
        recut = kind == "recut"   # 界隈の切り直しの取り足し（acquire/recut.py）。取り方は掘り下げと同じ
        what = jobs.deepen_what(dp)
        ai = ai_where() or "Claude"
        r = dp.get("result") or {}
        est = f"約{int(round(float(dp.get('est_min') or 0)))}分"
        n = len(dp.get("targets") or [])
        if st == "queued" and before is None:
            if kind == "add":
                notify.send(f"「{title}」に動画を足します（属性とコメント）", f"{n}本・{est}。Mac を開いたままにしておいてください")
            elif kind == "videos":
                notify.send(f"「{title}」の動画のコメントを取り足します",
                            f"{'全部の界隈・' if dp.get('community') == 'all' else ''}{n}本・{est}。Mac を開いたままにしておいてください")
            else:
                notify.send(f"「{title}」のコメントを取り足します",
                            f"{'切り直した界隈' if recut else '界隈 ' + str(dp.get('community'))}・{est}。"
                            "Mac を開いたままにしておいてください")
        elif st == "done" and (r.get("error") or r.get("blocked")):
            notify.send(f"「{title}」の{'動画の追加' if kind == 'add' else '取り足し'}が途中で止まりました",
                        f"{ai} で「{title}の分析を続けて」と言うと、取れた分をレポートに活かします")
            self.log.warning("%sが途中で止まりました: %s %s", what, m["analysis_id"], r)
        elif st == "done":
            if kind == "add":
                lost = len(r.get("not_found") or [])
                notify.send(f"「{title}」に動画を足しました",
                            f"{ai} で「{title}の分析を続けて」と言うと、足した動画をレポートに活かします"
                            f"（{len(r.get('added') or [])}本、コメント{r.get('comments_added', 0)}件"
                            + (f"。{lost}本は動画のページを読めませんでした" if lost else "") + "）")
            else:
                notify.send(f"「{title}」の取り足しが終わりました",
                            f"{ai} で「{title}の分析を続けて」と言うと、"
                            + (f"新しい界隈の分け方でレポートを書き直します（{r.get('videos_new', 0)}本、コメント{r.get('comments_added', 0)}件）"
                               if recut else
                               f"レポートに活かします（新しく{r.get('videos_new', 0)}本・続き{r.get('videos_more', 0)}本、コメント{r.get('comments_added', 0)}件）"))
            self.log.info("%sが終わりました: %s %s", what, m["analysis_id"], r)
        elif st == "failed":
            notify.send(f"「{title}」の{'動画の追加' if kind == 'add' else '取り足し'}が止まりました",
                        f"{ai} で「{title}の分析を続けて」と言えば、"
                        + ("今ある動画で続きを進めます" if kind == "add" else
                           "手元のコメントでレポートを書き直します" if recut else
                           "手元のコメントでレポートを直します" if kind == "videos" else "手元のコメントで掘り下げを進めます"))
            self.log.warning("%sが止まりました: %s %s", what, m["analysis_id"], dp.get("error"))

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
        self._alerting = False
        self._restarting = set()       # 開き直している最中の AI のアプリ
        self._restart_lock = threading.Lock()
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
        # アップデート（0.9.0）: いつも出す。用意できたら「バージョン X にアップデート…」、作業中は灰色で「…が終わったらアップデートします」
        self.update_item = rumps.MenuItem(UPDATE_CHECK, callback=self.update_clicked)
        items += [None, self.update_item, rumps.MenuItem(f"{config.APP_NAME} {VERSION}"),
                  rumps.MenuItem("終了", callback=self.quit_app)]
        self.menu = items
        rumps.Timer(self.refresh, 2).start()

    def refresh(self, _):
        self._startup_alert_tick()
        self._update_ui_tick()
        self._autolink_tick()
        while startup_restarts:   # 窓で「今すぐアップデート」を押して入れ替えたあと: 聞かずに開き直す
            name = startup_restarts.pop(0)
            threading.Thread(target=self._restart_after_update, args=(name,), daemon=True).start()
        self._update_menu()
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

    def _startup_alert_tick(self):
        """起動のときに積んだ窓（版を上げた・.app が2つある）を1つずつ出す。窓は答えるまで止まるので、出している間はほかを出さない"""
        if self._alerting or not startup_alerts:
            return
        self._alerting = True
        try:
            a = startup_alerts.pop(0)
            r = rumps.alert(a["title"], a["message"], ok=a["ok"], cancel=a["cancel"])
            if r == 1 and a.get("open"):
                subprocess.run(["open", a["open"]])
            if r == 1:
                for n in a.get("restart") or []:
                    threading.Thread(target=self._restart_claude if n == "Claude" else self._restart_chatgpt,
                                     daemon=True).start()
        finally:
            self._alerting = False

    # --- 新しい版（0.9.0。updater.py・docs/AUTO_UPDATE.md） ---
    def _update_menu(self):
        """メニューの「アップデートを確認…」の行を、いまの様子に合わせる（押せないときは灰色）"""
        c = self.ctl
        s = c.update_staged
        if s and c.update_busy:
            title, on = update_wait_title(c.update_busy), False
        elif s:
            title, on = f"バージョン {s['version']} にアップデート…", True
        elif c.update_phase == "downloading" and c.update_found:
            title, on = "アップデートをダウンロード中…", False
        elif c.update_phase == "checking" and c.update_manual:
            title, on = "アップデートを確認中…", False
        else:
            title, on = UPDATE_CHECK, True
        if self.update_item.title != title:
            self.update_item.title = title
        want = self.update_clicked if on else None
        if self.update_item.callback != want:
            self.update_item.set_callback(want)

    def update_clicked(self, _):
        c = self.ctl
        if not config.FROZEN and not os.environ.get("UGC_UPDATE_API"):
            rumps.alert("ソースから動かしているときは使えません（アプリにしたときだけ）")
            return
        s = c.update_staged
        if not s:   # 見に行く（結果は窓で。_update_ui_tick）
            c.check_update(manual=True)
            self._update_menu()
            return
        if c.update_asking or c.update_go or self._alerting:
            return   # 窓をもう出している・出すところ・入れ替えるところ
        why = c.update_wait_reason()
        if why:
            rumps.alert(update_wait_title(why), UPDATE_WAIT_TEXT)
            return
        c.update_asking = True
        self._alerting = True
        try:
            self._ask_update(s, c.ai_open())
        finally:
            self._alerting = False

    def _update_ui_tick(self):
        """見張りが決めたこと（黙って入れ替える・窓で聞く・メニューから頼まれた結果）を、画面のスレッドでする"""
        if self._alerting:
            return
        c = self.ctl
        if c.update_go:
            go, c.update_go = c.update_go, None
            self._do_update(go)
            return
        if not c.update_requests:
            return
        req = c.update_requests.pop(0)
        self._alerting = True
        try:
            if req["kind"] == "ask":
                self._ask_update(c.update_staged or req, req.get("ai") or [])
            elif req["kind"] == "latest":
                rumps.alert("最新の状態です", f"{config.APP_NAME} {VERSION} は最新のバージョンです。")
            elif req["kind"] == "error":
                rumps.alert("アップデートを確認できませんでした",
                            "インターネットに接続されているか確かめて、しばらくしてからもう一度お試しください。")
            elif req["kind"] == "wait":
                rumps.alert(update_wait_title(req["why"]), UPDATE_WAIT_TEXT)
        finally:
            self._alerting = False

    def _ask_update(self, s: dict, ai: list):
        """「UGC Analyzer X が利用できます」の窓（Claude・ChatGPT を開いているとき・メニューから押したとき）。答えまで画面のスレッドで"""
        c = self.ctl
        title, msg = update_ask_text(s, ai)
        try:
            r = rumps.alert(title, msg, ok="今すぐアップデート", cancel="あとで")
        except Exception:
            c.update_asking = False
            raise
        kind, val = c.update_answer(r == 1, ai)
        if kind == "wait":
            rumps.alert(f"{val}が始まったため、終わってからアップデートします", UPDATE_WAIT_TEXT)
        elif kind == "go":
            self._do_update(val)

    def _do_update(self, go: dict):
        """入れ替えの係を起こして、アプリを終える（係がアプリの終わりを待って入れ替え、新しい版を開く）"""
        c = self.ctl
        s = c.update_staged
        try:
            if not s:
                raise updater.UpdateError("入れる準備ができた版がありません")
            updater.apply(s, go["mode"], go.get("restart") or [], c.log)
        except Exception as e:
            c.log.warning("入れ替えの係を起こせませんでした: %s: %s", type(e).__name__, e)
            if s:
                updater.save(failed={"version": s["version"], "at": updater.now_iso(), "why": str(e)[:300]})
            r = rumps.alert("アップデートできませんでした", UPDATE_FAIL_TEXT, ok="手順を開く", cancel="閉じる")
            if r == 1:
                subprocess.run(["open", updater.GUIDE_URL])
            return
        c.status_text = "アップデートしています…"
        c.shutdown()
        system.release_single_instance()
        rumps.quit_application()

    def _restart_after_update(self, name: str):
        restart = claude_link.restart_claude if name == "Claude" else codex_link.restart_chatgpt
        self._restart_ai(name, restart, "「このまま終了」" if name == "Claude" else "終了するほう", None)   # 通知はアップデートの1つだけ

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
        self._restart_ai("Claude", claude_link.restart_claude, "「このまま終了」", f"Claude で「{START_PHRASE}」と言ってみてください")

    def _restart_ai(self, name: str, restart, button: str, after: str | None):
        """AI のアプリを開き直す（裏のスレッドで）。作業中で終了の確認が出たら案内し、答えてもらうまで最長10分待つ（system.relaunch_app）"""
        with self._restart_lock:
            if name in self._restarting:   # 開き直しの最中にもう一度押された
                return
            self._restarting.add(name)
        try:
            ok = restart(on_asking=lambda: notify.send(
                f"{name} が終了の確認をしています",
                f"{name} の画面に出ている確認で、{button}を押してください（会話は消えません）。終了したら、UGC Analyzer が {name} を自動で起動します"))
        finally:
            with self._restart_lock:
                self._restarting.discard(name)
        if ok and after:
            notify.send(f"{name} を開き直しました", after)
        else:   # 通知は見落としやすいので、窓で出す（窓は画面のスレッドで）
            AppHelper.callAfter(rumps.alert, f"{name} を閉じられませんでした",
                                f"{name} を自分で終了（{name} を前に出して ⌘Q）してから、もう一度開いてください。"
                                f"開き直すと、UGC Analyzer が使えるようになります。")

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
        self._restart_ai("ChatGPT", codex_link.restart_chatgpt, "終了するほう",
                         f"ChatGPT の Work で「{START_PHRASE}」と言ってみてください")

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


# 起動のときに出す窓（run loop が回ってから、画面のタイマー CollectorApp.refresh が1つずつ出す。起動を止めないため）。
# {"title", "message", "ok", "cancel", "restart": [開き直す AI の名前], "open": 「OK」で開く URL（無くてよい）}
startup_alerts: list = []
# 窓で「今すぐアップデート」を押して入れ替えたあと、聞かずに開き直す AI の名前（update_notice・version_notice が積む）
startup_restarts: list = []

# アップデートの文言（2026-10-31 ユーザー「新しい版を確かめる、みたいな言い方は普通しない。UI 文言を洗練させて」。
# ほかの Mac のアプリと同じ言い方にそろえる: 動作は「アップデート」、番号は「バージョン」、AI のアプリを閉じて開くのは「再起動」）
UPDATE_CHECK = "アップデートを確認…"
UPDATE_WAIT_TEXT = "終わりしだい自動でアップデートします。Claude（ChatGPT）を開いている場合は、その前に確認します。"
UPDATE_FAIL_TEXT = (f"いまのバージョン（{VERSION}）はそのまま使えます。始め方のページの「アップデート」の手順で、"
                    "手動でアップデートしてください（10分ほど）。")


def update_wait_title(why) -> str:
    """作業中でアップデートを待っているときの文（メニューの灰色の行・窓の題）。why は「取得」か「AI の作業」"""
    return f"{why}が終わったらアップデートします"


def update_ask_text(s: dict, ai: list) -> tuple:
    """「UGC Analyzer X が利用できます」の窓の題と本文。1行目にこのアップデートの内容（release.sh --note。無ければ出さない）"""
    note = f"{_sentence(s['note'])}\n\n" if s.get("note") else ""
    if ai:
        who = "と".join(ai)
        body = (f"アップデートすると、{who} を再起動します（1分ほど）。会話や分析・レポートはそのまま残ります。\n"
                f"「あとで」を選んだ場合は、次に {who} を終了したときにアップデートします。")
    else:
        body = "アップデートすると、UGC Analyzer を再起動します（30秒ほど）。分析とレポートはそのまま残ります。"
    return f"{config.APP_NAME} {s['version']} が利用できます", note + body


def _sentence(text) -> str:
    """文の終わりの「。」をそろえる（アップデートの内容の1行を文の中に入れるとき）"""
    t = str(text or "").strip()
    return t if not t or t.endswith(("。", "！", "？", ")", "）")) else t + "。"


def update_notice(st: dict, log) -> dict | None:
    """自動の入れ替え（updater.apply）のあと初めて開いたとき。st は起動したときの state.json。
    入れ替えられた → 通知を出し、その記録（{"from", "to", "mode", "restart", "note"}）を返す（version_notice に渡す）。
    前の版のまま開いた（係が入れ替えられず、前の版を開いた）→ 窓で手の入れ替えを頼む。その版は、もう自分からは入れない"""
    p = st.get("update_pending")
    if not isinstance(p, dict):
        return None
    config.save_state(update_pending=None)
    if p.get("to") == VERSION:
        log.info("自動の入れ替えで %s → %s にしました（%s）", p.get("from"), VERSION, p.get("mode"))
        try:
            updater.tidy()
            updater.save(staged=None, failed=None, asked_on=None)
        except Exception as e:
            log.info("用意した版を片付けられませんでした: %s", e)
        notify.send(f"{config.APP_NAME} を {VERSION} にアップデートしました",
                    (_sentence(p["note"]) if p.get("note") else "") + "分析とレポートはそのまま残っています。")
        return p
    log.warning("自動の入れ替えができず、前の版（%s）のまま開きました（%s へ）。入れ替えの記録:\n%s",
                VERSION, p.get("to"), updater.log_tail())
    updater.save(failed={"version": p.get("to"), "at": updater.now_iso(), "why": updater.log_tail(2)[-300:]})
    startup_alerts.append({"title": "アップデートできませんでした", "message": UPDATE_FAIL_TEXT, "ok": "手順を開く",
                           "cancel": "閉じる", "restart": [], "open": updater.GUIDE_URL})
    return None


def version_notice(st: dict, log, updated: dict | None = None) -> None:
    """アプリを新しくしたあと初めて開いたとき（版が変わった）、開いている Claude（ChatGPT）を開き直すよう1回だけ知らせる。
    AI の道具（--mcp）は Claude（ChatGPT）が起こしたまま動き続けるので、開き直すまで前の版の中身のまま
    （.app を置き換えたあとは、前の版の道具が部品を読めずに止まることもある）。st は起動したときの state.json。
    前の版の道具の処理（--mcp・--wait。install.sh と同じ見分け方、system.old_tool_processes）が動いていれば、通知は見落とされやすいので
    窓でも出し、「開き直す」で 0.6.6 の開き直し（_restart_ai・system.relaunch_app）を使う（2026-10-12。友達は 0.7.5 に入れ替えたあと、
    開き直すまで 0.6.4 の道具で話していた。docs/PLAN_0_8.md 3-4）。動いていなければ今までどおり通知だけ。
    updated は自動の入れ替えの記録（update_notice）: 通知は update_notice が出したので重ねない。窓で「今すぐアップデート」を
    押していたら（mode="now"）、前の版の道具が動いている AI を聞かずに開き直す（0.9.0）"""
    if st.get("app_version") == VERSION:
        return
    config.save_state(app_version=VERSION)
    if not st.get("first_run_done"):
        return   # 初めて入れた（前の版は無い）
    names = [name for _, name, link, _, is_running in AI_LINKS
             if link.status() == "connected" and getattr(link, is_running)()]
    try:
        old = system.old_tool_processes()
    except Exception as e:
        log.info("前の版の道具の処理を確かめられませんでした: %s", e)
        old = []
    log.info("版が変わりました: %s → %s（開いている AI: %s・前の版の道具の処理: %s）", st.get("app_version") or "前の版", VERSION,
             "、".join(names) or "なし", "、".join(f"{p['kind']}（PID {p['pid']}・{p['version'] or '0.7.5 まで'}）" for p in old) or "なし")
    if names and not updated:
        notify.send(f"{config.APP_NAME} を {VERSION} にアップデートしました",
                    f"{'と'.join(names)} を再起動すると、新しいバージョンが使えるようになります（会話はそのまま残ります）。")
    # 開き直すのは、開いている AI（つないだかに関わらず。道具を起こしたのは開いている AI のどれか）
    running = [name for _, name, link, _, is_running in AI_LINKS if getattr(link, is_running)()]
    if updated and updated.get("mode") == "now":
        # 窓で「今すぐ」（「Claude を一度閉じて開き直します」と伝えた）: 前の版の道具が止まっていても、伝えたとおり開き直す
        want = [n for n in updated.get("restart") or [] if n in running] or (running if old else [])
        if want:
            startup_restarts.extend(want)
            return
    if not old:
        return
    who = "と".join(running) or "Claude（ChatGPT）"
    msg = (f"{who} を再起動すると、新しいバージョンが使えるようになります。それまでは、{who} の中では前のバージョンのまま動きます。"
           "会話はそのまま残ります。")
    startup_alerts.append({"title": f"{config.APP_NAME} を {VERSION} にアップデートしました",
                           "message": msg if running else msg + f"\n\n{who} を前に出して ⌘Q で終了し、もう一度開いてください。",
                           "ok": f"{who} を再起動" if running else "OK", "cancel": "あとで" if running else None,
                           "restart": running})


def duplicate_notice(log) -> None:
    """UGC Analyzer の .app が2つ以上ある（「両方とも残す」でできた「UGC Analyzer 2.app」など）と分かったら、窓で知らせる（2026-10-12）。
    2つあると、あとから開いた方が Claude・ChatGPT の設定を自分に書き直し、古い方を開くと前の版に戻る。自動では消さない（どれを残すかは利用者）"""
    try:
        apps = system.find_app_bundles()
    except Exception as e:
        log.info("入っている .app を確かめられませんでした: %s", e)
        return
    if len(apps) < 2:
        return
    newest, olds = apps[0], apps[1:]
    log.warning("UGC Analyzer が%s個あります: %s", len(apps),
                "、".join(f"{a['path']}（{a['version'] or '版不明'}{'・動いているもの' if a['running'] else ''}）" for a in apps))
    lines = [f"・{Path(a['path']).stem}（{a['version'] or '版が読めません'}{'・いま動いているもの' if a['running'] else ''}）"
             f"{' ← 新しい版' if a is newest else ''}" for a in apps]
    old_names = "」「".join(Path(a["path"]).stem for a in olds)
    msg = ("アプリケーションフォルダに UGC Analyzer が2つ以上あります。\n" + "\n".join(lines) + "\n\n"
           f"新しい版を1つ残して、ほか（「{old_names}」）をゴミ箱に入れてください。残した方は、名前がそのままでも使えます。\n"
           "2つあると、あとから開いた方が Claude・ChatGPT とのつなぎを自分に書き直すので、古い版に戻ってしまうことがあります。")
    if not newest["running"]:
        msg += (f"\n\nいま動いているのは古い方です。メニューバーの UGC Analyzer の「終了」で終えてから古い方をゴミ箱に入れ、"
                f"「{Path(newest['path']).stem}」を開いてください。")
    startup_alerts.append({"title": "UGC Analyzer が2つ入っています", "message": msg, "ok": "OK", "cancel": None,
                           "restart": []})


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
            version_notice(st, log, update_notice(st, log))
            duplicate_notice(log)
    ctl = Controller(code, log)
    system.watch_sleep(ctl.on_sleep, ctl.on_wake)
    app = CollectorApp(ctl)
    ctl.start()
    app.run()
