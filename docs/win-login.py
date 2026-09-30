r"""
収集用 Chrome（127.0.0.1:9222）で TikTok にメール／パスワードでログインする。
ToDo-3 の分担どおり ID/パスはこちらが入力し、CAPTCHA・認証コードは人が画面で操作する。

  .venv\Scripts\python.exe win-login.py --creds creds.json [--port 9222] [--wait 240]

creds.json: {"email": "...", "password": "..."}   ← 実行後に必ず消すこと
終了コード: 0=ログイン済み / 2=CAPTCHA等で人の操作待ちのまま時間切れ / 1=失敗
"""
import argparse
import json
import re
import sys
import time

from selenium import webdriver
from selenium.webdriver.common.by import By

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="9222")
ap.add_argument("--creds", required=True)
ap.add_argument("--wait", type=int, default=240, help="送信後に人の操作（CAPTCHA等）を待つ秒数")
ap.add_argument("--shot", default="login_state.png")
a = ap.parse_args()

with open(a.creds, encoding="utf-8") as f:
    c = json.load(f)

o = webdriver.ChromeOptions()
o.debugger_address = f"127.0.0.1:{a.port}"
d = webdriver.Chrome(options=o)
d.set_script_timeout(30)


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def state():
    try:
        txt = d.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        txt = ""
    txt = re.sub(r"\s+", " ", txt)
    has_session = d.get_cookie("sessionid") is not None
    captcha = d.execute_script("""
      const sel = '[class*="captcha"],[id*="captcha"],[class*="Captcha"],iframe[src*="captcha"]';
      return Array.from(document.querySelectorAll(sel)).some(e => e.offsetWidth > 0 && e.offsetHeight > 0);
    """)
    return {"url": d.current_url, "session": has_session, "captcha": bool(captcha),
            "challenge": ("Please wait" in txt) or ("アクセスが拒否" in txt), "text": txt[:200]}


def find_first(selectors, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for s in selectors:
            els = [e for e in d.find_elements(By.CSS_SELECTOR, s) if e.is_displayed()]
            if els:
                return els[0]
        time.sleep(0.5)
    return None


st = state()
if st["session"]:
    log(f"既にログイン済み: {st['url']}")
    sys.exit(0)

d.execute_cdp_cmd("Page.bringToFront", {})
if "/login/phone-or-email/email" not in d.current_url:
    d.get("https://www.tiktok.com/login/phone-or-email/email")
    time.sleep(3)

user = find_first(['input[name="username"]', 'input[placeholder*="メール"]', 'input[type="text"]'])
pw = find_first(['input[type="password"]'])
if not user or not pw:
    log(f"入力欄が見つからない: user={bool(user)} pw={bool(pw)} url={d.current_url}")
    d.save_screenshot(a.shot)
    sys.exit(1)

user.click(); user.clear(); user.send_keys(c["email"])
time.sleep(0.8)
pw.click(); pw.clear(); pw.send_keys(c["password"])
time.sleep(0.8)
log("ID/パスを入力した")

btn = find_first(['button[data-e2e="login-button"]', 'button[type="submit"]'], timeout=5)
if not btn:
    log("ログインボタンが見つからない")
    d.save_screenshot(a.shot)
    sys.exit(1)
btn.click()
log("送信した。人の操作（CAPTCHA/認証コード）が要るならここで待つ")

t0 = time.time()
last = None
while time.time() - t0 < a.wait:
    time.sleep(3)
    st = state()
    key = (st["session"], st["captcha"], st["challenge"], st["url"])
    if key != last:
        log(f"session={st['session']} captcha={st['captcha']} challenge={st['challenge']} url={st['url']}")
        log(f"  text: {st['text']}")
        last = key
    if st["session"]:
        d.save_screenshot(a.shot)
        log("ログイン成立（sessionid Cookie あり）")
        sys.exit(0)
    if st["challenge"]:
        d.save_screenshot(a.shot)
        log("チャレンジ画面。中断")
        sys.exit(1)

d.save_screenshot(a.shot)
log("時間切れ。画面の状態は login_state.png")
sys.exit(2)
