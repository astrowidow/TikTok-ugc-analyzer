"""
TikTok に触る処理を1つずつにするロック（プロセスをまたぐ）。

既存の CSV ジョブ（Web サービスの中の待ち行列）と、分析の取得の係（別プロセス、acquire/worker.py）は、
同じ IP から TikTok に触る。同時に走らないよう、このロックで直列にする（docs/IMPLEMENTATION_HANDOVER.md 5-3）。

- ロックはファイル（output/locks/tiktok.lock）。中身は持ち主・PID・取った時刻。作るのは O_EXCL で1つだけ
- 持ち主のプロセスが死んでいたら（PID で確かめる）古いロックとして取り除く
- 待っている人は output/locks/wait-*.json を置く。長い処理（取得の係）は区切りごとに someone_waiting() を見て、
  待っている人がいれば一度ロックを譲る（CSV ジョブを13時間待たせない）

PID の生存確認: Windows では os.kill(pid, 0) がプロセスを終わらせてしまうので使わない（ctypes の OpenProcess で見る）。
"""
import contextlib
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOCK_DIR = Path(os.environ.get("UGC_LOCK_DIR", BASE_DIR / "output" / "locks"))
LOCK_FILE = LOCK_DIR / "tiktok.lock"


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def holder():
    """今の持ち主（無ければ None。死んだ持ち主のロックは取り除く）"""
    info = _read(LOCK_FILE)
    if info is None:
        if LOCK_FILE.exists():  # 書きかけ。少し待てば読める
            return {"owner": "?", "pid": -1}
        return None
    if not pid_alive(info.get("pid", -1)):
        with contextlib.suppress(FileNotFoundError):
            LOCK_FILE.unlink()
        return None
    return info


def try_acquire(owner: str) -> bool:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    holder()  # 死んだ持ち主を掃除
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"owner": owner, "pid": os.getpid(),
                   "since": datetime.datetime.now().isoformat(timespec="seconds")}, f, ensure_ascii=False)
    return True


def release(owner: str) -> None:
    info = _read(LOCK_FILE)
    if info and info.get("pid") == os.getpid() and info.get("owner") == owner:
        with contextlib.suppress(FileNotFoundError):
            LOCK_FILE.unlink()


def _wait_file(owner: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", owner)[:60]
    return LOCK_DIR / f"wait-{os.getpid()}-{safe}.json"


def acquire(owner: str, poll: float = 5.0, on_wait=None, timeout: float | None = None) -> bool:
    """取れるまで待つ。待っている間は wait-*.json を置き、on_wait(持ち主) を呼ぶ"""
    if try_acquire(owner):
        return True
    wf = _wait_file(owner)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    wf.write_text(json.dumps({"owner": owner, "pid": os.getpid(),
                              "since": datetime.datetime.now().isoformat(timespec="seconds")},
                             ensure_ascii=False), encoding="utf-8")
    start = time.time()
    notified = False
    try:
        while True:
            if try_acquire(owner):
                return True
            if on_wait and not notified:
                with contextlib.suppress(Exception):
                    on_wait(holder())
                notified = True
            if timeout is not None and time.time() - start > timeout:
                return False
            time.sleep(poll)
    finally:
        with contextlib.suppress(FileNotFoundError):
            wf.unlink()


def someone_waiting(exclude_pid: int | None = None) -> list:
    """ロックを待っている人（死んだ人の待ち札は取り除く）"""
    out = []
    if not LOCK_DIR.exists():
        return out
    me = exclude_pid or os.getpid()
    for p in LOCK_DIR.glob("wait-*.json"):
        info = _read(p)
        if not info:
            continue
        if not pid_alive(info.get("pid", -1)):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
            continue
        if info.get("pid") != me:
            out.append(info)
    return out


@contextlib.contextmanager
def held(owner: str, on_wait=None):
    acquire(owner, on_wait=on_wait)
    try:
        yield
    finally:
        release(owner)


def yield_if_waiting(owner: str, log=None) -> float:
    """待っている人がいれば一度ロックを譲り、取り戻すまで待つ。譲っていた秒数を返す"""
    waiters = someone_waiting()
    if not waiters:
        return 0.0
    t0 = time.time()
    if log:
        log(f"TikTok のロックを譲ります（待っている: {', '.join(w.get('owner', '?') for w in waiters)}）")
    release(owner)
    # 相手が取るまで少し待つ（取られる前に取り戻さないように）。相手がもう取って返し終えていれば待たない
    for _ in range(120):
        h = holder()
        if (h and h.get("pid") != os.getpid()) or not someone_waiting():
            break
        time.sleep(0.5)
    acquire(owner)
    waited = time.time() - t0
    if log:
        log(f"TikTok のロックを取り戻しました（{waited / 60:.1f}分譲った）")
    return waited
