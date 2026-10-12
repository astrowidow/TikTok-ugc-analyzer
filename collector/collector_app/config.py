"""置き場所と定数。取得の部品（acquire/）と AI の相手（proto_runner・flow_w1）は環境変数で置き場所を受け取るので、
読み込む前に setup_env() を呼ぶ。"""
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
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
              "store.py", "analysis_info.py", "service_prompts"]


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
        rows = [_json.loads(x) for x in d.read_text(encoding="utf-8").split("\n") if x.strip()]
        have = {r.get("file") for r in rows}
        old = dst / d.name
        local = [_json.loads(x) for x in old.read_text(encoding="utf-8").split("\n") if x.strip()] if old.exists() else []
        rows += [r for r in local if r.get("from") == "kb_update" and r.get("file") not in have]
        text = "".join(_json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        if not old.exists() or old.read_text(encoding="utf-8") != text:
            old.write_text(text, encoding="utf-8")
            done.append(d.name)
    return done


SHIPPED_MARK = ".shipped.json"   # 知識ベースの置き場に置く: 運営の同梱物を届けた記録（ファイルごとの中身の印）
# この Mac で直していなければ、運営の新しい版に入れ替えるもの（distilled/ の中）
SHIPPED_REPLACE = ("README.md", "STYLE_GUIDE.md", "GLOSSARY.md")


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _shipped_stamp(src: Path) -> str:
    """同梱物の印（中身は読まず、名前・大きさ・時刻だけ。起動のたびに見るので軽く）"""
    h = hashlib.sha256(VERSION.encode())
    for p in sorted(src.rglob("*")):
        if p.is_file():
            st = p.stat()
            h.update(f"{p.relative_to(src)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


def sync_shipped_kb(src: Path, kb: Path) -> dict:
    """運営の同梱物（記事・INDEX.json・用語集・文体ガイド・README・カード）を、すでに入れた Mac にも届ける（2026-10-12。docs/PLAN_0_8.md 第6章）。
    前は最初の写しのあと、COMMUNITY_GUIDE.md と community_defs.jsonl しか届かなかった（sync_shipped_knowledge）。
    この Mac で足した分は失わない:
      - notes/: 無い記事は足す。ある記事は、この Mac で書き換えていなければ運営の版に（note から取った記事は残る）
      - INDEX.json: 運営の一覧に、この Mac で新着として足した記事（added_at のあるもの）を後ろに残す
      - distilled/cards.jsonl: 運営のカードに、この Mac で取り込んだ記事（update_state.json の ingested）のカードを後ろに残す
      - distilled/README.md・STYLE_GUIDE.md・GLOSSARY.md: この Mac で書き換えていなければ入れ替える。用語集を整理の仕事で
        書き直した Mac（kb_update が新しい記事の語を章に溶かし込んだ）や、利用者が手で直したものは、そのまま残す
      - GLOSSARY_ADDITIONS.json・update_state.json・COMMUNITY の2つ（sync_shipped_knowledge）には触らない
    「書き換えていない」の見分け: 前に届けたときの中身の印（.shipped.json）と同じか。記録の無い最初の1回（0.7.5 までに入れた Mac）は、
    kb_update の記録で見る: 用語集は整理の仕事をしていなければ（update_state.json の merged も GLOSSARY.md.bak-* も無い）、記事は
    この Mac で新着として取ったもの（INDEX.json の added_at）でなければ、書き換えていないとみなす。README・文体ガイドはアプリが書き換えない。
    （ファイルの時刻では見分けられない: 運営の Mac の知識ベースは、最初の写しのときの時刻になっていた。2026-10-12 に確かめた）
    記録の無い最初の1回に入れ替える distilled/ のファイルは、入れ替える前の中身を <名前>.bak-ship-<日時> に残す（手で直していても失わない）。
    kb_update と同じ錠を取る（AI の道具が同時に取り込みを書いているかもしれないため）。何をしたかを返す"""
    import kb_update
    mark_p = kb / SHIPPED_MARK
    try:
        mark = json.loads(mark_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        mark = {}
    mark = mark if isinstance(mark, dict) else {}
    stamp = _shipped_stamp(src)
    if mark.get("stamp") == stamp:
        return {}
    known = dict(mark.get("files") or {})
    stamp_now = time.strftime("%Y%m%d-%H%M%S")
    done = {"replaced": [], "kept": [], "backup": [], "notes_added": 0, "notes_updated": 0, "index": "", "cards": ""}
    files = {}
    try:
        same = Path(kb_update.kb_dir()).resolve() == Path(kb).resolve()
    except OSError:
        same = False
    with (kb_update._locked() if same else contextlib.nullcontext()):   # noqa: SLF001
        try:
            kb_state = json.loads((kb / "update_state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kb_state = {}
        kb_state = kb_state if isinstance(kb_state, dict) else {}
        try:
            cur_index = json.loads((kb / "INDEX.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cur_index = []
        cur_index = [x for x in cur_index if isinstance(x, dict)] if isinstance(cur_index, list) else []
        fetched_here = {x.get("file") for x in cur_index if x.get("added_at")}   # この Mac で note から取った記事
        merged_here = bool(kb_state.get("merged")) or any(   # kb_update の整理が残す控え（GLOSSARY.md.bak-<日時>）
            re.fullmatch(r"GLOSSARY\.md\.bak-\d{8}-\d{6}", x.name) for x in (kb / "distilled").glob("GLOSSARY.md.bak-*"))

        def untouched(p: Path, rel: str) -> bool:
            if not p.exists():
                return True
            if rel in known:
                return _sha_file(p) == known[rel]
            if rel.startswith("notes/"):
                return p.name not in fetched_here
            if rel == "distilled/GLOSSARY.md":
                return not merged_here
            return True
        # 記事
        notes = src / "notes" if (src / "notes").exists() else src
        (kb / "notes").mkdir(parents=True, exist_ok=True)
        for f in sorted(notes.glob("*.md")):
            rel = f"notes/{f.name}"
            p = kb / rel
            data = f.read_bytes()
            if p.exists() and p.read_bytes() == data:
                files[rel] = hashlib.sha256(data).hexdigest()
                continue
            if not untouched(p, rel):
                continue   # この Mac で note から取り直した記事など
            existed = p.exists()
            shutil.copy2(f, p)
            files[rel] = hashlib.sha256(data).hexdigest()
            done["notes_updated" if existed else "notes_added"] += 1
        # 記事の一覧
        if (src / "INDEX.json").exists():
            ship = json.loads((src / "INDEX.json").read_text(encoding="utf-8"))
            cur = cur_index
            keys = {x.get("key") for x in ship}
            mine = [x for x in cur if isinstance(x, dict) and x.get("added_at") and x.get("key") not in keys]
            new = ship + mine
            if new != cur:
                text = json.dumps(new, ensure_ascii=False, indent=1)
                tmp = kb / f"INDEX.json.tmp{os.getpid()}"
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, kb / "INDEX.json")
                done["index"] = f"{len(cur)}→{len(new)}本"
        # 用語集・文体ガイド・README
        dd = kb / "distilled"
        for name in SHIPPED_REPLACE:
            f = src / "distilled" / name
            if not f.exists():
                continue
            rel = f"distilled/{name}"
            p = dd / name
            data = f.read_bytes()
            if p.exists() and p.read_bytes() == data:
                files[rel] = hashlib.sha256(data).hexdigest()
                continue
            if not untouched(p, rel):
                done["kept"].append(name)
                continue
            if p.exists() and rel not in known:   # 記録の無い最初の1回: 入れ替える前の中身を残す
                b = dd / f"{name}.bak-ship-{stamp_now}"
                shutil.copy2(p, b)
                done["backup"].append(b.name)
            tmp = dd / f"{name}.tmp{os.getpid()}"
            shutil.copy2(f, tmp)
            os.replace(tmp, p)
            files[rel] = hashlib.sha256(data).hexdigest()
            done["replaced"].append(name)
        # カード
        f = src / "distilled" / "cards.jsonl"
        if f.exists():
            ship_lines = [x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
            p = dd / "cards.jsonl"
            cur_lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []

            def file_of(line):
                try:
                    return json.loads(line).get("file")
                except (ValueError, AttributeError):
                    return None
            have = {file_of(x) for x in ship_lines}
            here = {x.get("file") for x in kb_state.get("ingested") or [] if isinstance(x, dict)}
            mine = [x for x in cur_lines if file_of(x) in here and file_of(x) not in have]
            new = ship_lines + mine
            if new != cur_lines:
                tmp = dd / f"cards.jsonl.tmp{os.getpid()}"
                tmp.write_text("\n".join(new) + "\n", encoding="utf-8")
                os.replace(tmp, p)
                done["cards"] = f"{len(cur_lines)}→{len(new)}本"
        mark = {"stamp": stamp, "version": VERSION, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "files": {**known, **files}, "kept": done["kept"]}
        tmp = kb / f"{SHIPPED_MARK}.tmp{os.getpid()}"
        tmp.write_text(json.dumps(mark, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, mark_p)
    return {k: v for k, v in done.items() if v}


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
        try:   # 用語集・文体ガイド・記事・カードも（届けられなくても、アプリと道具の起動は止めない。次の起動でまた試す）
            ship = sync_shipped_kb(src, KB_DIR)
            if ship:
                done["knowledge_ship"] = ship
        except Exception as e:
            done["knowledge_ship_error"] = f"{type(e).__name__}: {e}"
    try:
        import prompt_store
        done["prompts"] = prompt_store.seed()
    except Exception as e:   # 指示書が置けなくても、初期の指示書で動く
        done["prompts_error"] = str(e)
    if log and (done.get("knowledge") or done.get("knowledge_sync") or done.get("knowledge_ship")
                or any((done.get("prompts") or {}).get(k) for k in ("copied", "updated"))):
        log.info("知識ベース・指示書を置きました: %s", done)
    if log and done.get("knowledge_ship_error"):
        log.info("知識ベースの同梱物を届けられませんでした（次の起動でまた試します）: %s", done["knowledge_ship_error"])
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
