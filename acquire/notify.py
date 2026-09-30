"""
取得の完了（または失敗）を利用者にメールで知らせる。

送り元はまだ決まっていない（docs/IMPLEMENTATION_LOG.md の「ユーザーへのお願い」U2）。
設定ファイル（既定 ~/ugc-secrets/mail.json、リポジトリ外・output 外）が無ければ**送らずにログだけ**残す。

  mail.json: {"host": "smtp.gmail.com", "port": 587, "starttls": true, "user": "...", "password": "...", "from": "..."}
宛先は利用者の対応表（~/ugc-secrets/mcp_users.json）の "email"。
"""
import datetime
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

MAIL_CONFIG = Path(os.environ.get("UGC_MAIL_CONFIG", Path.home() / "ugc-secrets" / "mail.json"))
USERS_FILE = Path(os.environ.get("UGC_MCP_USERS", Path.home() / "ugc-secrets" / "mcp_users.json"))


def _user_email(user_id: str):
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    for u in data.get("users", []):
        if u.get("user_id") == user_id:
            return u.get("email")
    return None


def _mask(addr: str) -> str:
    if not addr or "@" not in addr:
        return addr or ""
    name, dom = addr.split("@", 1)
    return name[:2] + "***@" + dom


def compose(meta: dict, failed: str | None = None) -> tuple:
    title = meta.get("title") or meta.get("analysis_id")
    if failed:
        subject = f"【UGC Analyzer】「{title}」の取得が止まりました"
        body = (f"「{title}」の取得が途中で止まりました。\n理由: {failed}\n\n"
                "運営が確認します。取れたところまでは保存してあり、直せば続きから再開できます。\n")
    else:
        acq = meta.get("acquisition") or {}
        c = ((acq.get("steps") or {}).get("comments") or {}).get("detail") or {}
        body = (f"「{title}」の取得が終わりました。\n"
                f"動画 {((acq.get('steps') or {}).get('enrich') or {}).get('detail', {}).get('list_rows', '?')}本の一覧と属性、"
                f"そのうち {c.get('videos_ok', '?')}本のコメント（{c.get('comments', '?')}件）を取りました。\n\n"
                f"AI のアプリ（Claude など）を開いて、\n\n    「{title}の分析を続けて」\n\nと言ってください。"
                "途中で界隈（どんな人たちが使ったか）の分け方を一度だけ確認します。\n")
        subject = f"【UGC Analyzer】「{title}」の取得が終わりました"
    return subject, body


def send(run, failed: str | None = None) -> dict:
    meta = run.meta
    subject, body = compose(meta, failed)
    to = _user_email(meta.get("owner", ""))
    res = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "subject": subject,
           "to": _mask(to), "sent": False}
    cfg = None
    try:
        cfg = json.loads(MAIL_CONFIG.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        pass
    if not cfg:
        res["reason"] = f"メールの送信設定（{MAIL_CONFIG}）が無いので送っていません"
    elif not to:
        res["reason"] = "利用者のメールアドレス（mcp_users.json の email）が無いので送っていません"
    else:
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = cfg.get("from") or cfg.get("user")
            msg["To"] = to
            msg.set_content(body)
            port = int(cfg.get("port", 587))
            if port == 465:
                s = smtplib.SMTP_SSL(cfg["host"], port, timeout=30)
            else:
                s = smtplib.SMTP(cfg["host"], port, timeout=30)
                if cfg.get("starttls", True):
                    s.starttls()
            if cfg.get("user"):
                s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
            s.quit()
            res["sent"] = True
        except Exception as e:
            res["reason"] = f"送信に失敗: {type(e).__name__}: {str(e)[:200]}"
    with open(run.dir / "fetch_log" / "notify.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n" + body + "\n---\n")
    run.log(f"    知らせ: {'送信した' if res['sent'] else res.get('reason')}")
    return res
