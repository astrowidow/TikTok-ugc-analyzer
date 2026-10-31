"""新しい版を自分で見つけて入れ替える（0.9.0。docs/AUTO_UPDATE.md）。

2026-10-31 ユーザー「バージョンアップが不便。ほかのアプリみたいに通知が出て、自然にアップデートがかかるように」。
友達に届くのは、GitHub のリリースの「最新」（latest）だけ。運営が「友達に出して」と言ったときに collector/release.sh --friends が
試し版（prerelease）を最新に切り替える（ユーザー決定: 試していない版を友達に届けない）。運営の Mac（DATA_DIR/operator がある）は
試し版も受け取り、友達と同じ入れ替えの道を先に通る。

流れ（app.Controller が裏のスレッドで回す。窓は画面のスレッド）:
  1. 見に行く（latest_release）: アプリを開いた1分後・Mac が起きた1分後・3時間おき・メニューの「新しい版を確かめる…」
  2. 落として確かめる（stage）: .dmg を落とし、大きさと sha256（GitHub の digest）を確かめ、開いて中の .app の版と識別子を見て
     updates/staged/<版>/ に写す。写した .app を --selftest で起こして、動くことを確かめる。だめなら入れ替えない
  3. 入れ替える（decide・apply）: 取得中・AI の作業中は待つ。Claude・ChatGPT が閉じていれば黙って、開いていれば窓で聞いてから
     （ユーザー決定「空いていれば黙って」）。アプリが終わったあと、別のプロセス（swap.sh）が .app を入れ替えて開く。
     前の版は updates/backup/ に1つだけ残す（ゴミ箱には入れない。友達が戻してしまわないように）
  4. 新しい版が開いたら（app.update_notice）: 通知。窓で「今すぐ」を押していたら Claude（ChatGPT）を開き直す。
     前の版のまま開いたら（入れ替えられなかった）、窓で手の入れ替えを頼む。その版は、もう自分からは入れない（メニューからは試せる）

アプリが自分で落としたファイルには、Mac のダウンロードの印（com.apple.quarantine）が付かないので、2回目からは「このまま開く」が要らない。
"""
import datetime
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import VERSION, config, system

REPO = "astrowidow/TikTok-ugc-analyzer"
ASSET = "UGC-Analyzer.dmg"   # release.sh が付けるファイルの名前（版なし。友達のリンクと同じ）
GUIDE_URL = "https://claude.ai/artifact/Tp343YwoMtSrFWKuJcCLmC#update"   # 始め方のページの「新しい版にするとき」
CHECK_EVERY = 3 * 3600        # 新しい版を見に行く間隔（秒）
FIRST_CHECK = 60              # 開いた・起きたあと、見に行くまで（秒。ネットがつながるのを待つ）
AI_QUIET = 15 * 60            # AI が道具を最後に使ってから、作業が終わったとみなすまで（秒）
SELFTEST_TIMEOUT = 300
NOTE_RE = re.compile(r"^今回の版[:：]\s*(.+?)\s*$", re.M)   # リリースの説明の、友達に見せる1行（release.sh --friends が書く）
VERSION_RE = re.compile(r"\d+(\.\d+)*")


class UpdateError(Exception):
    """新しい版を用意できない・入れ替えられない（文は記録に残す。友達には窓の決まった文を出す）"""


def api_base() -> str:
    """リリースの API（試験では UGC_UPDATE_API で手元の偽物に向ける）"""
    return (os.environ.get("UGC_UPDATE_API") or f"https://api.github.com/repos/{REPO}/releases").rstrip("/")


def updates_dir() -> Path:
    return config.DATA_DIR / "updates"


def update_log() -> Path:
    return config.LOG_DIR / "update.log"


def test_channel() -> bool:
    """試し版も受け取るか（運営の Mac だけ）"""
    return config.OPERATOR_FLAG.exists()


def today() -> str:
    return datetime.date.today().isoformat()


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# --- 1. 見に行く ---
def note_of(body) -> str | None:
    """リリースの説明から、友達に見せる「今回の版: …」の1行"""
    m = NOTE_RE.search(str(body or ""))
    return m.group(1) if m else None


def _digest(d) -> str | None:
    """GitHub の digest（"sha256:…"）の16進。無ければ None（古いリリース）"""
    m = re.fullmatch(r"sha256:([0-9a-f]{64})", str(d or ""))
    return m.group(1) if m else None


def pick(releases, include_pre: bool) -> dict | None:
    """リリースの一覧から、入れる候補（いちばん新しい版で .dmg が付いているもの）。下書きは見ない。
    返すのは {"version", "url", "size", "sha256", "note", "prerelease"}"""
    best = None
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        if rel.get("prerelease") and not include_pre:
            continue
        v = str(rel.get("tag_name") or "").lstrip("v")
        asset = next((a for a in rel.get("assets") or [] if isinstance(a, dict) and a.get("name") == ASSET), None)
        if not asset or not VERSION_RE.fullmatch(v) or not asset.get("browser_download_url"):
            continue
        cand = {"version": v, "url": asset["browser_download_url"], "size": int(asset.get("size") or 0),
                "sha256": _digest(asset.get("digest")), "note": note_of(rel.get("body")),
                "prerelease": bool(rel.get("prerelease"))}
        if best is None or system.version_key(v) > system.version_key(best["version"]):
            best = cand
    return best


def _headers() -> dict:
    return {"Accept": "application/vnd.github+json", "User-Agent": f"UGC-Analyzer/{VERSION}"}


def _get_json(url: str):
    import requests
    r = requests.get(url, headers=_headers(), timeout=20)
    if r.status_code == 404:   # リリースが1つも無い
        return None
    r.raise_for_status()
    return r.json()


def latest_release(include_pre: bool | None = None) -> dict | None:
    """いま入れられるいちばん新しい版（pick の形）。友達は「最新」だけ、運営の Mac は試し版も"""
    include_pre = test_channel() if include_pre is None else include_pre
    if include_pre:
        rels = _get_json(api_base() + "?per_page=20") or []
    else:
        one = _get_json(api_base() + "/latest")
        rels = [one] if one else []
    return pick(rels, include_pre)


def is_newer(version) -> bool:
    return system.version_key(version) > system.version_key(VERSION)


# --- 2. 落として確かめる ---
def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(rel: dict, log=None) -> Path:
    """.dmg を updates/ に落とす（落とし終えたものがあれば使う）。大きさと sha256 が合わなければ消して UpdateError"""
    import requests
    d = updates_dir()
    d.mkdir(parents=True, exist_ok=True)
    dmg = d / f"UGC-Analyzer-{rel['version']}.dmg"
    if dmg.exists() and (not rel.get("size") or dmg.stat().st_size == rel["size"]) and \
            (not rel.get("sha256") or _sha256(dmg) == rel["sha256"]):
        return dmg
    part = d / (dmg.name + ".part")
    h = hashlib.sha256()
    n = 0
    t0 = time.time()
    try:
        with requests.get(rel["url"], headers=_headers(), stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
        if rel.get("size") and n != rel["size"]:
            raise UpdateError(f"落とした .dmg の大きさが違います（{n} / {rel['size']} バイト）")
        if rel.get("sha256") and h.hexdigest() != rel["sha256"]:
            raise UpdateError("落とした .dmg の中身の印（sha256）が、リリースのものと合いません")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dmg)
    if log:
        log.info("新しい版 %s の .dmg を落としました（%.1fMB・%.0f秒）", rel["version"], n / 1e6, time.time() - t0)
    return dmg


def _run(cmd, timeout: float):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise UpdateError(f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} が失敗（{r.returncode}）: "
                          f"{(r.stderr or r.stdout).strip()[-300:]}")
    return r


def bundle_info(app: Path) -> dict:
    with open(app / "Contents" / "Info.plist", "rb") as f:
        return plistlib.load(f)


def clean_env(**extra) -> dict:
    """別の .app（新しい版）を起こすときの環境。PyInstaller の起動の印を持ち越さない（持ち越すと、起こした方の中身を読むことがある）"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI", "_MEI", "PYINSTALLER_"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.update(extra)
    return env


def selftest(app: Path, log=None) -> None:
    """用意した .app を --selftest で起こす（一時の置き場で。TikTok にも Claude にも触らない。build.sh の自己点検1と同じ）"""
    exe = app / "Contents" / "MacOS" / config.APP_NAME
    home = Path(tempfile.mkdtemp(prefix="ugc-update-selftest-"))
    try:
        r = subprocess.run([str(exe), "--selftest"], env=clean_env(UGC_COLLECTOR_HOME=str(home)),
                           capture_output=True, text=True, timeout=SELFTEST_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise UpdateError("新しい版の自己点検が時間内に終わりませんでした") from e
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if r.returncode != 0:
        raise UpdateError(f"新しい版の自己点検が通りませんでした（{r.returncode}）: {(r.stdout + r.stderr).strip()[-400:]}")
    if log:
        log.info("新しい版の自己点検が通りました: %s", app)


def stage(rel: dict, log=None) -> dict:
    """新しい版を落として、入れ替えられる形（updates/staged/<版>/UGC Analyzer.app）にする。
    返すのは {"version", "app", "note", "staged_at"}。だめなら UpdateError（途中のものは消す）"""
    dmg = download(rel, log)
    d = updates_dir()
    dest = d / "staged" / rel["version"]
    app_dst = dest / f"{config.APP_NAME}.app"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    mnt = Path(tempfile.mkdtemp(prefix="ugc-update-mnt-"))
    try:
        _run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", str(mnt), str(dmg)], 180)
        try:
            src = mnt / f"{config.APP_NAME}.app"
            if not src.is_dir():
                raise UpdateError(f".dmg の中に「{config.APP_NAME}.app」がありません")
            info = bundle_info(src)
            if info.get("CFBundleIdentifier") != config.APP_ID:
                raise UpdateError(f".dmg の中のアプリの識別子が違います（{info.get('CFBundleIdentifier')}）")
            if info.get("CFBundleShortVersionString") != rel["version"]:
                raise UpdateError(f".dmg の中のアプリの版が違います（{info.get('CFBundleShortVersionString')} / {rel['version']}）")
            _run(["/usr/bin/ditto", str(src), str(app_dst)], 600)
        finally:
            if subprocess.run(["/usr/bin/hdiutil", "detach", str(mnt)], capture_output=True, timeout=60).returncode != 0:
                subprocess.run(["/usr/bin/hdiutil", "detach", "-force", str(mnt)], capture_output=True, timeout=60)
        selftest(app_dst, log)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(mnt, ignore_errors=True)
    tidy(keep=rel["version"])
    return {"version": rel["version"], "app": str(app_dst), "note": rel.get("note"), "staged_at": now_iso()}


def tidy(keep: str | None = None) -> None:
    """用意した版・落とした .dmg のうち、keep 以外を片付ける（前の版の控え updates/backup/ は残す）"""
    d = updates_dir()
    for p in (d / "staged").glob("*") if (d / "staged").exists() else []:
        if p.name != keep:
            shutil.rmtree(p, ignore_errors=True)
    for p in d.glob("UGC-Analyzer-*.dmg*"):
        if keep is None or p.name not in (f"UGC-Analyzer-{keep}.dmg", f"UGC-Analyzer-{keep}.dmg.part"):
            p.unlink(missing_ok=True)


def staged_from_state(st: dict | None = None) -> dict | None:
    """前に用意した版（state.json の update.staged）。今の版より新しく、.app が残っているときだけ"""
    s = ((st if st is not None else config.load_state()).get("update") or {}).get("staged")
    if not isinstance(s, dict) or not is_newer(s.get("version")):
        return None
    app = Path(str(s.get("app") or ""))
    return s if (app / "Contents" / "MacOS" / config.APP_NAME).exists() else None


def save(**kv) -> dict:
    """state.json の update（staged・asked_on・failed・checked_at）を部分的に書く"""
    st = config.load_state()
    u = dict(st.get("update") or {})
    u.update(kv)
    config.save_state(update=u)
    return u


# --- 3. 入れ替えるか決める ---
# 道具の記録（logs/mcp.log）の、道具を1回使い終えたときの行（mcp_proto._call が書く。前の版の道具も同じ形）
_CALL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*道具の中身 ")


def last_tool_call() -> float | None:
    """AI が UGC Analyzer の道具を最後に使った時刻（epoch 秒）。道具の記録の終わりのほう（入れ替わった1つ前の記録も）から探す。
    道具の処理を開いた・閉じたの行は数えない（Claude を開いただけで作業中にしない）"""
    for name in ("mcp.log", "mcp.log.1"):
        p = config.LOG_DIR / name
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 512_000))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for ln in reversed(lines):
            m = _CALL_RE.match(ln)
            if m:
                try:
                    return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
    return None


def ai_active(now: float | None = None) -> bool:
    """AI が UGC Analyzer の道具を使っている途中か: 道具を最後に使ってから15分以内（logs/mcp.log。Claude・ChatGPT の道具の処理が
    全部書く）、または Claude の Code タブの待つ命令（--wait）が動いている"""
    now = time.time() if now is None else now
    t = last_tool_call()
    if t is not None and now - t < AI_QUIET:
        return True
    try:
        return any(p["kind"] == "wait" for p in system.tool_processes())
    except Exception:
        return False


def decide(staged: dict | None, busy: str | None, ai_open: list, state: dict, manual: bool = False) -> str | None:
    """用意した版をどうするか: None（何もしない）・"wait"（作業が終わるまで待つ）・"silent"（黙って入れ替える）・"ask"（窓で聞く）。
    state は state.json の update。manual はメニューから頼まれたとき（入れ替えられなかった版も試す・今日聞いたかを見ない）"""
    if not staged:
        return None
    if busy:
        return "wait"
    if manual:
        return "ask"
    if (state.get("failed") or {}).get("version") == staged.get("version"):
        return None   # 自分からは入れ直さない（メニューからは試せる）
    if not ai_open:
        return "silent"
    if state.get("asked_on") == today():
        return None
    return "ask"


# --- 3. 入れ替える ---
# アプリが終わるのを待ってから、.app を入れ替えて開く（アプリの中からは自分を置き換えられない。動いている部品を読み直して壊れる）。
# 前の版は控えの場所へ（1つだけ残す）。どこで失敗しても、前の版を元の場所に戻して開く（新しい版のアプリが update_notice で気づく）
SWAP_SH = r"""#!/bin/bash
# UGC Analyzer の入れ替え（collector_app/updater.py の apply が起こす）。
# 引数: 待つ PID・今の .app・新しい .app・前の版の控えの場所・記録・開く命令
# 変数はいつも ${…} で囲む（macOS の bash 3.2 は、変数のすぐ後ろの全角の文字の一部まで名前として読む）
pid="$1"; app="$2"; new="$3"; bk="$4"; log="$5"; opener="${6:-/usr/bin/open}"
exec >>"${log}" 2>&1
say() { echo "$(date '+%Y-%m-%d %H:%M:%S') swap[$$]: $*"; }
say "始め: ${app} ← ${new}（アプリ PID ${pid} の終わりを待つ）"
for _ in $(seq 1 240); do kill -0 "${pid}" 2>/dev/null || break; sleep 0.5; done
if kill -0 "${pid}" 2>/dev/null; then say "アプリが2分で終わらないので、入れ替えない"; exit 1; fi
if [ ! -d "${new}" ]; then say "新しい .app が無いので、入れ替えない"; "${opener}" "${app}"; exit 1; fi
mkdir -p "$(dirname "${bk}")"
rm -rf "${bk}.prev"
[ -e "${bk}" ] && mv "${bk}" "${bk}.prev"
if ! mv "${app}" "${bk}"; then
  say "今の .app を動かせない（入れ替えない）"
  [ -e "${bk}.prev" ] && mv "${bk}.prev" "${bk}"
  "${opener}" "${app}"; exit 1
fi
if ! mv "${new}" "${app}"; then
  say "新しい .app を置けない → 前の版に戻す"
  [ -e "${app}" ] && rm -rf "${app}"
  mv "${bk}" "${app}"
  [ -e "${bk}.prev" ] && mv "${bk}.prev" "${bk}"
  "${opener}" "${app}"; exit 1
fi
rm -rf "${bk}.prev"
say "入れ替えた。開く"
"${opener}" "${app}"
"""


def current_bundle() -> Path | None:
    """入れ替える .app（いま動いているもの）。アプリケーションフォルダの外・仮の場所（App Translocation）なら None"""
    if not config.FROZEN or system.is_translocated() or not system.in_applications():
        return None
    return system.bundle_of(sys.executable)


def apply(staged: dict, mode: str, restart: list, log=None, opener: str = "/usr/bin/open") -> None:
    """入れ替えの係（swap.sh）を起こす。呼び元はこのあとアプリを終える（係は、アプリの PID が消えるのを待ってから入れ替える）。
    mode: "silent"（黙って）・"now"（窓で「今すぐ」）。restart: 新しい版が開いたあとに開き直す AI の名前"""
    here = current_bundle()
    if here is None:
        raise UpdateError("アプリケーションフォルダの外で動いているので、自分では入れ替えられません")
    new = Path(staged["app"])
    if not (new / "Contents" / "MacOS" / config.APP_NAME).exists():
        raise UpdateError(f"用意した新しい版が見つかりません: {new}")
    d = updates_dir()
    d.mkdir(parents=True, exist_ok=True)
    script = d / "swap.sh"
    script.write_text(SWAP_SH, encoding="utf-8")
    script.chmod(0o755)
    bk = d / "backup" / f"{config.APP_NAME}.app"
    config.save_state(update_pending={"from": VERSION, "to": staged["version"], "mode": mode, "restart": list(restart or []),
                                      "note": staged.get("note"), "app": str(here), "at": now_iso()})
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["/bin/bash", str(script), str(os.getpid()), str(here), str(new), str(bk), str(update_log()), opener],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=clean_env())
    if log:
        log.info("入れ替えの係を起こしました: %s → %s（%s・開き直す AI: %s）", VERSION, staged["version"], mode,
                 "、".join(restart or []) or "なし")


def log_tail(n: int = 6) -> str:
    try:
        return "\n".join(update_log().read_text(encoding="utf-8").splitlines()[-n:])
    except OSError:
        return ""
