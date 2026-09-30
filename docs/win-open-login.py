r"""
収集用 Chrome（127.0.0.1:9222）で TikTok のログインページを開き、前面に出す。
Phase 6 でユーザーが手動ログインするための準備。TikTok へのアクセスはこの1ページのみ。

  .venv\Scripts\python.exe win-open-login.py [--port 9222]
"""
import argparse
import re
import time

from selenium import webdriver

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="9222")
ap.add_argument("--url", default="https://www.tiktok.com/login")
a = ap.parse_args()

o = webdriver.ChromeOptions()
o.debugger_address = f"127.0.0.1:{a.port}"
d = webdriver.Chrome(options=o)
d.set_script_timeout(30)

d.maximize_window()
d.get(a.url)
d.execute_cdp_cmd("Page.bringToFront", {})
time.sleep(5)

txt = d.execute_script("return document.body ? document.body.innerText : ''") or ""
txt = re.sub(r"\s+", " ", txt)[:400]
print("url  :", d.current_url)
print("title:", d.title)
print("vis  :", d.execute_script("return document.visibilityState"))
print("text :", txt)
print("challenge:", ("Please wait" in txt) or ("アクセスが拒否" in txt))
