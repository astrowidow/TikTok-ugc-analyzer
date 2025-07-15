from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
import json
import time

# WebDriverを設定して起動
options = webdriver.ChromeOptions()
options.add_argument("--start-maximized")
driver = webdriver.Chrome(options=options)

# TikTok動画のURL（サンプル）
video_url = "https://www.tiktok.com/@oimo_shiba_inu/video/7374900768513543442"

# URLにアクセス
driver.get(video_url)
time.sleep(5)  # ページが完全に読み込まれるまで待機

try:
    # JSONデータが含まれている<script>タグを取得
    script_tag = driver.find_element(By.XPATH, '//script[@id="__UNIVERSAL_DATA_FOR_REHYDRATION__"]')
    json_text = script_tag.get_attribute('innerHTML')

    # JSONデータを辞書として読み込む
    data = json.loads(json_text)

    # JSONデータをファイルに保存
    with open('tiktok_video_data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    print("JSONデータが 'tiktok_video_data.json' として保存されました。")

except Exception as e:
    print(f"Failed to extract JSON data: {e}")

# ブラウザを閉じる
driver.quit()