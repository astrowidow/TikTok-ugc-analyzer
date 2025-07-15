from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import time
import pandas as pd

# WebDriverを設定して起動
options = webdriver.ChromeOptions()
options.add_argument("--start-maximized")
driver = webdriver.Chrome(options=options)

# TikTokのURLにアクセス
tiktok_url = 'https://www.tiktok.com/music/一目惚れ-7361610680082630673'
driver.get(tiktok_url)

# ページの読み込み待機
wait = WebDriverWait(driver, 10)
wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

wait_time = 7
time.sleep(wait_time)

# ページをスクロールしてコンテンツを読み込む
scroll_pause_time = wait_time  # スクロール後の待機時間（秒）
for i in range(10):  # スクロールの回数を適宜調整
    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
    time.sleep(scroll_pause_time)

# ページソースを取得してBeautifulSoupで解析
page_source = driver.page_source
soup = BeautifulSoup(page_source, 'html.parser')

# 動画のURLを抽出
video_elements = soup.find_all('a', href=True)  # TikTokの動画リンクを含む要素を特定
video_urls = []

for element in video_elements:
    href = element['href']
    if 'video' in href or 'photo' in href:  # 動画URLのパターンにマッチするかをチェック
        video_urls.append(href)

# 重複を排除してURLリストを取得
unique_video_urls = list(set(video_urls))

# ページアクセスして各種情報取得


# Pandasでデータフレームに保存（必要に応じてCSVに書き出し）
df = pd.DataFrame(unique_video_urls, columns=['Video URL'])
df.to_csv('tiktok_video_urls.csv', index=False)

# ブラウザを閉じる
driver.quit()