"""置き場所と定数。取得の部品（acquire/）と AI の相手（proto_runner・flow_w1）は環境変数で置き場所を受け取るので、
読み込む前に setup_env() を呼ぶ。"""
import hashlib
import json
import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import VERSION

APP_NAME = "UGC Analyzer"   # 利用者に見える呼び方はこれだけ（2026-10-05 ユーザー「表記ゆれやめよう。全て UGC Analyzer に統一」）
APP_ID = "jp.ugc-analyzer.collector"
OWNER = "local"   # この Mac の利用者（1人で固定。0.1.0 の分析もこの名前で作ってある）

DATA_DIR = Path(os.environ.get("UGC_COLLECTOR_HOME") or Path.home() / "Library" / "Application Support" / APP_NAME)
ANALYSES_DIR = DATA_DIR / "analyses"
LOCK_DIR = DATA_DIR / "locks"
PROFILE_DIR = DATA_DIR / "chrome-profile"   # 取得専用の Chrome のプロファイル（捨て垢のログインはここだけに残る）
CODE_ROOT = DATA_DIR / "code"
LOG_DIR = DATA_DIR / "logs"
PROMPTS_DIR = DATA_DIR / "prompts"          # 利用者が編集できる指示書（prompt_store.py）
KB_DIR = DATA_DIR / "knowledge"             # 知識ベース（kb_update.py）
USERS_DIR = DATA_DIR / "users"              # 会話で変える設定（user_settings.py）
REPORTS_DIR = DATA_DIR / "レポート"          # できあがったレポート（曲ごとのフォルダ）
STATE_FILE = DATA_DIR / "state.json"
PID_FILE = DATA_DIR / "app.pid"
OPERATOR_FLAG = DATA_DIR / "operator"       # このファイルがあれば、メニューに運営向けの項目を出す
CHROME_PORT = int(os.environ.get("UGC_COLLECTOR_PORT") or 9250)   # 本線の Windows 機の 9222 とは別

# ちょいとり（約5分）: 一覧を先頭20本（属性1分ほど）、コメントは2本・各30件、返信は開かない。
# 切るのは worker_entry.py。要求の速さ（calls_per_min など）は変えない。
# 上限は20件にしない（20件ちょうどで止まると「描画が止まって20件で頭打ち」の確かめが誤報になる。2026-10-02）
TRIAL_SETTINGS = {"list_sets": 1, "list_scrolls": 1, "trial_links": 20, "pool_budget": 2, "trial_pool": 2,
                  "trial_cap": 30, "reply_top": 0, "reply_questions": 0, "reply_author": 0,
                  "comment_deadline_hours": 0.25}

FROZEN = getattr(sys, "frozen", False)
# 本線のリポジトリの直下（ソースから動かすとき）。固めたアプリでは同梱の code/ を DATA_DIR に写して使う
REPO_ROOT = Path(__file__).resolve().parents[2]
# 同梱する部品（UGCCollector.spec も同じ一覧を使う）
CODE_FILES = ["acquire", "analysis/pool.py", "analysis/attr_cluster.py", "analysis/enrich.py", "analysis/build_llm_input.py",
              "analysis/prep_sample.py", "analysis/prep_comments.py", "scraper.py", "log_setup.py", "tiktok_lock.py",
              "proto_runner.py", "flow_w1.py", "user_settings.py", "mcp_proto.py", "kb_update.py", "prompt_store.py",
              "store.py", "service_prompts"]


def bundled_code_dir() -> Path:
    if FROZEN:
        return Path(sys._MEIPASS) / "code"  # noqa: SLF001
    return REPO_ROOT


def bundled_knowledge_dir():
    """最初の知識ベース。固めたアプリでは同梱の knowledge/（notes/ と distilled/）、
    ソースからは本線の output/notes_corpus/（記事が直下にある形。リポジトリには入れていない）"""
    p = Path(sys._MEIPASS) / "knowledge" if FROZEN else REPO_ROOT / "output" / "notes_corpus"  # noqa: SLF001
    return p if (p / "distilled" / "GLOSSARY.md").exists() else None


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.name == ".complete":
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def code_dir() -> Path:
    """取得の部品を動かす場所。ソースならリポジトリそのもの。

    固めたアプリでは、同梱の部品を DATA_DIR/code/<版> に写して使う。アプリ本体の中は書き込めない
    （ダウンロードしたまま開くと読み取り専用の場所で動く＝App Translocation）のに、部品はログを横に書くため。
    """
    if not FROZEN:
        return REPO_ROOT
    src = bundled_code_dir()
    dst = CODE_ROOT / f"{VERSION}-{_tree_digest(src)}"
    if not (dst / ".complete").exists():
        tmp = dst.with_name(dst.name + f".tmp{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        (tmp / ".complete").write_text(VERSION, encoding="utf-8")
        try:
            tmp.rename(dst)
        except OSError:   # 同時に起動したもう1つのプロセス（メニューと Claude の道具）が先に写し終えた
            shutil.rmtree(tmp, ignore_errors=True)
    return dst


def setup_env() -> Path:
    """置き場所を作り、部品が読む環境変数を入れ、部品を import できるようにする"""
    for d in (DATA_DIR, ANALYSES_DIR, LOCK_DIR, PROFILE_DIR, LOG_DIR, USERS_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    os.environ["UGC_ANALYSES_DIR"] = str(ANALYSES_DIR)
    os.environ["UGC_LOCK_DIR"] = str(LOCK_DIR)
    os.environ["UGC_PROMPTS_DIR"] = str(PROMPTS_DIR)
    os.environ["UGC_KB_DIR"] = str(KB_DIR)
    os.environ["UGC_USERS_DIR"] = str(USERS_DIR)
    os.environ["UGC_REPORTS_DIR"] = str(REPORTS_DIR)
    os.environ["UGC_LOCAL_APP"] = "1"
    os.environ["PYTHONUTF8"] = "1"
    code = code_dir()
    for p in (code / "analysis", code):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    return code


def sync_shipped_knowledge(src: Path, dst: Path) -> list:
    """運営が作ってアプリに入れる蒸留物を、すでに入れた Mac にも届ける（最初の写しのあとに足したもの）。
    - COMMUNITY_GUIDE.md（界隈の名付け方の手引き）: 運営のものなので、アプリのものと違えば入れ替える
    - community_defs.jsonl（レポートごとの界隈の名前と見分け方）: アプリの分に、この Mac で新しい記事から足した分（from=kb_update）を残して合わせる
    何をしたかの一覧を返す"""
    import json as _json
    done = []
    g = src / "COMMUNITY_GUIDE.md"
    if g.exists() and (not (dst / g.name).exists() or (dst / g.name).read_bytes() != g.read_bytes()):
        shutil.copy2(g, dst / g.name)
        done.append(g.name)
    d = src / "community_defs.jsonl"
    if d.exists():
        rows = [_json.loads(x) for x in d.read_text(encoding="utf-8").splitlines() if x.strip()]
        have = {r.get("file") for r in rows}
        old = dst / d.name
        local = [_json.loads(x) for x in old.read_text(encoding="utf-8").splitlines() if x.strip()] if old.exists() else []
        rows += [r for r in local if r.get("from") == "kb_update" and r.get("file") not in have]
        text = "".join(_json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        if not old.exists() or old.read_text(encoding="utf-8") != text:
            old.write_text(text, encoding="utf-8")
            done.append(d.name)
    return done


def seed_data(log=None) -> dict:
    """最初の起動（とアプリを新しくしたとき）: 知識ベースと指示書を置く。何をしたかを返す"""
    done = {}
    src = bundled_knowledge_dir()
    if src and not (KB_DIR / "distilled" / "GLOSSARY.md").exists():
        tmp = KB_DIR.with_name(KB_DIR.name + f".tmp{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        (tmp / "notes").mkdir(parents=True)
        shutil.copytree(src / "distilled", tmp / "distilled",
                        ignore=shutil.ignore_patterns("part*", "*.bak-*"))
        notes = src / "notes" if (src / "notes").exists() else src
        for f in notes.glob("*.md"):
            shutil.copy2(f, tmp / "notes" / f.name)
        if (src / "INDEX.json").exists():
            shutil.copy2(src / "INDEX.json", tmp / "INDEX.json")
        try:
            tmp.rename(KB_DIR)
            done["knowledge"] = "copied"
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
    if src and (KB_DIR / "distilled").exists():
        done["knowledge_sync"] = sync_shipped_knowledge(src / "distilled", KB_DIR / "distilled")
    try:
        import prompt_store
        done["prompts"] = prompt_store.seed()
    except Exception as e:   # 指示書が置けなくても、初期の指示書で動く
        done["prompts_error"] = str(e)
    if log and (done.get("knowledge") or done.get("knowledge_sync")
                or any((done.get("prompts") or {}).get(k) for k in ("copied", "updated"))):
        log.info("知識ベース・指示書を置きました: %s", done)
    return done


def logger(name: str = "collector") -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    return log


# --- 小さな状態（ログインを確かめた時刻・画面を消さない設定など） ---
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(**kv) -> dict:
    s = load_state()
    s.update(kv)
    tmp = STATE_FILE.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)
    return s
