"""
Markdown の原稿を、note のエディタ（あなたのブラウザ）に段落ごとに貼り込む。
公開はしない（下書きまで）。note は「プレーンテキストの Markdown を貼ると見出し・太字・リンク・引用・箇条書きに変換」し、
「URL を単独で貼ると埋め込み（行末で Enter でも可）」なので、それをブロック単位で再現する。

使い方（Mac）:
  1) 専用プロファイルで Chrome を起動（初回は note にログインしておく。普段の Chrome とは別プロファイル）
       "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         --remote-debugging-port=9333 --user-data-dir="$HOME/.note-chrome" https://note.com/notes/new
  2) 本文欄を一度クリックしてから、このスクリプトを実行
       .venv/bin/python analysis/md2note.py output/trial_silhouette/e2e/report/NOTE_BODY.md --title output/trial_silhouette/e2e/report/NOTE_TITLE.txt
  3) 終わったらブラウザで目視 → 自分で公開

制約: note の利用規約は自動投稿ツールを制限している。本スクリプトは「自分のブラウザのエディタへの貼り付け補助」に限定し、
公開操作は行わない。実行は人が見ている状態で、ゆっくり（ブロックごとに 0.8〜1.5 秒）行う。
"""
import argparse
import random
import re
import subprocess
import sys
import time

from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

ap = argparse.ArgumentParser()
ap.add_argument("body")
ap.add_argument("--title", default=None)
ap.add_argument("--port", default="9333")
ap.add_argument("--start", type=int, default=0, help="このブロック番号から再開")
ap.add_argument("--dry", action="store_true", help="ブロック分割だけ表示して終了")
a = ap.parse_args()

URL_RE = re.compile(r"^https://www\.tiktok\.com/@\S+/video/\d+$")

text = open(a.body, encoding="utf-8").read()
# 空行で区切ってブロックにする。箇条書き・引用は連続行を1ブロックとして保つ
blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", text) if b.strip()]
print(f"ブロック数 {len(blocks)}（うち URL {sum(1 for b in blocks if URL_RE.match(b.strip()))}）")
if a.dry:
    for i, b in enumerate(blocks[:40]):
        print(f"[{i}] {'URL ' if URL_RE.match(b.strip()) else ''}{b[:60]!r}")
    sys.exit(0)


def clip(s):
    subprocess.run(["pbcopy"], input=s.encode("utf-8"), check=True)


o = webdriver.ChromeOptions()
o.debugger_address = f"127.0.0.1:{a.port}"
d = webdriver.Chrome(options=o)
print("接続:", d.current_url)
if "note.com" not in d.current_url:
    print("note のエディタページを開いてから実行してください（https://note.com/notes/new）")
    sys.exit(1)

# contenteditable を列挙: note は タイトル と 本文 が別の編集領域。最後の（大きい）ものを本文とみなす
eds = d.find_elements(By.CSS_SELECTOR, "[contenteditable='true']")
print(f"contenteditable 要素: {len(eds)}")
if not eds:
    print("編集領域が見つかりません。エディタが開いているか確認してください")
    sys.exit(1)
body = max(eds, key=lambda e: e.size["height"] * e.size["width"])
title_el = next((e for e in eds if e is not body), None)

ac = ActionChains(d)


def paste(s, el):
    clip(s)
    el.click()
    ActionChains(d).key_down(Keys.COMMAND).send_keys("v").key_up(Keys.COMMAND).perform()


if a.title and title_el is not None and a.start == 0:
    t = open(a.title, encoding="utf-8").read().strip()
    title_el.click()
    title_el.send_keys(t)
    time.sleep(0.8)
    print("タイトル入力:", t[:40])

body.click()
# 末尾へ
ActionChains(d).key_down(Keys.COMMAND).send_keys(Keys.END).key_up(Keys.COMMAND).perform()
time.sleep(0.5)

for i, b in enumerate(blocks):
    if i < a.start:
        continue
    is_url = bool(URL_RE.match(b.strip()))
    try:
        if is_url:
            # URL 単独の貼り付け → 埋め込み。念のため行末で Enter も送る
            clip(b.strip())
            ActionChains(d).key_down(Keys.COMMAND).send_keys("v").key_up(Keys.COMMAND).perform()
            time.sleep(1.2)
            ActionChains(d).send_keys(Keys.ENTER).perform()
            time.sleep(2.0 + random.random())
            n_embed = len(d.find_elements(By.CSS_SELECTOR, "iframe, figure, [class*='embed']"))
            print(f"[{i}] URL → 埋め込み要素数 {n_embed}: {b.strip()[-30:]}")
        else:
            clip(b + "\n")
            ActionChains(d).key_down(Keys.COMMAND).send_keys("v").key_up(Keys.COMMAND).perform()
            time.sleep(0.4)
            ActionChains(d).send_keys(Keys.ENTER).perform()
            time.sleep(0.8 + random.random() * 0.7)
            print(f"[{i}] {b[:50]!r}")
    except Exception as e:
        print(f"[{i}] エラー: {e}  → --start {i} で再開できます")
        break

print("完了。ブラウザで体裁と埋め込みを確認してから、自分で公開してください。")
