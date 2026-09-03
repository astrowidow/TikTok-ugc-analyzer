from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
import json
import pandas as pd
import datetime
import time

# ファイル名
song_name = "愛をくださいませ"

# 解析するURL
base_url = "https://www.tiktok.com/music/オリジナル楽曲-ノイミーTikTok-7640468291809151765"  # タグページなどからURLを抽出する場合の例

# WebDriverを設定して起動
options = webdriver.ChromeOptions()
options.add_argument("--start-maximized")
options.add_argument("--mute-audio")  # ミュート設定を追加
driver = webdriver.Chrome(options=options)

# 取得したURLリストを保持するための変数
video_urls = set()  # 重複防止のためにセットを使用

# ページをスクロールしてコンテンツを読み込む
scroll_pause_time = 3  # スクロール後の待機時間（秒）
scroll_num = 40
set_num = 4

for set_i in range(set_num):
    page_load_time = 5
    # TikTokのURLにアクセス
    driver.get(base_url)
    time.sleep(page_load_time)  # ページが完全に読み込まれるまで待機

    for _ in range(scroll_num):  # スクロールの回数を適宜調整
        driver.find_element(By.TAG_NAME, "body").send_keys(webdriver.common.keys.Keys.END)
        time.sleep(scroll_pause_time)

    # 動画・写真のURLを取得
    video_elements = driver.find_elements(By.XPATH, '//a[contains(@href, "/video/") or contains(@href, "/photo/")]')
    for element in video_elements:
        video_url = element.get_attribute('href')
        if video_url:
            video_urls.add(video_url)

# 最終的なURLリストをリスト形式に変換（セットなので自動で重複は削除されている）
final_video_urls = list(video_urls)

# 動画データを格納するリスト
video_data = []

# 各動画の情報を抽出
start_time = time.time()
total_videos = len(video_urls)


# WebDriverを再設定して起動
def create_driver():
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')  # headlessモードを有効にする
    options.add_argument('--disable-gpu')  # headlessモードで必要な場合がある
    options.add_argument('--no-sandbox')  # 環境によっては必要
    options.add_argument('--disable-dev-shm-usage')  # メモリの問題を防ぐ
    options.add_argument("--mute-audio")  # ミュート設定を追加
    return webdriver.Chrome(options=options)


html_load_time = 2
restart_threshold = 30  # 50回ごとにWebDriverを再起動
driver = create_driver()

for idx, url in enumerate(video_urls):
    # 一定回数ごとにWebDriverを再起動
    if idx % restart_threshold == 0 and idx != 0:
        print("WebDriverを再起動します...")
        driver.quit()
        driver = create_driver()

    try:
        driver.get(url)
    except Exception as e:
        print(f"ページ読み込みエラー: {e}")
        continue  # エラーが発生した場合はスキップして次のループへ
    if idx == 0:
        # input("ログイン作業を済ませたらなにかキーを押してください")
        time.sleep(html_load_time)  # ページが完全に読み込まれるまで待機
    else:
        time.sleep(html_load_time)  # ページが完全に読み込まれるまで待機

    try:
        # URLに 'photo' が含まれている場合
        if "/photo/" in url:
            # 動画IDを取得 (URLの末尾の数字部分)
            video_id_str = url.split("/")[-1]
            video_id = int(video_id_str)

            # 32ビット右シフトで作成日時を推定
            timestamp_bits = video_id >> 32
            create_time = datetime.datetime.fromtimestamp(timestamp_bits).strftime('%Y-%m-%d %H:%M:%S')

            # データをリストに格納
            video_data.append({
                'URL': url,
                'Created Date': create_time,
                'Description': '',
                'Likes': None,  # いいね数は後で内挿/外挿
                'Shares': '',
                'Comments': '',
                'Plays': '',
                'Saves': '',
                'Reposts': '',
                'Type': 'Photo'
            })
        else:
            # JSONデータが含まれている<script>タグを取得
            script_tag = driver.find_element(By.XPATH, '//script[@id="__UNIVERSAL_DATA_FOR_REHYDRATION__"]')
            json_text = script_tag.get_attribute('innerHTML')

            # JSONデータを辞書として読み込む
            data = json.loads(json_text)

            # 必要な情報を抽出
            video_info = data.get('__DEFAULT_SCOPE__', {}).get('webapp.video-detail', {}).get('itemInfo', {}).get('itemStruct', {})

            create_time = int(video_info.get('createTime', None))
            desc = video_info.get('desc', '')
            author_info = video_info.get('author', {})
            unique_id = author_info.get('uniqueId', '')

            stats = video_info.get('statsV2', {})
            # 作成日の形式を変換
            if create_time:
                create_time = datetime.datetime.fromtimestamp(create_time, datetime.UTC).strftime('%Y-%m-%d %H:%M:%S')

            # いいね数、シェア数、コメント数、再生数、保存数、リポスト数を取得
            digg_count = int(stats.get('diggCount', 0))  # 数値に変換
            share_count = stats.get('shareCount', 0)
            comment_count = stats.get('commentCount', 0)
            play_count = stats.get('playCount', 0)
            collect_count = stats.get('collectCount', 0)
            repost_count = stats.get('repostCount', 0)

            # データをリストに格納
            video_data.append({
                'URL': url,
                'Created Date': create_time,
                'Description': desc,
                'Likes': digg_count,
                'Shares': share_count,
                'Comments': comment_count,
                'Plays': play_count,
                'Saves': collect_count,
                'Reposts': repost_count,
                'Type': 'Video',
                'Username': unique_id,
            })

    except Exception as e:
        print(f"Failed to extract data for {url}: {e}")

    # 進捗状況を出力
    elapsed_time = time.time() - start_time
    avg_time_per_video = elapsed_time / (idx + 1)
    estimated_remaining_time = avg_time_per_video * (total_videos - idx - 1)
    print(f"Progress: {idx + 1}/{total_videos}, Estimated remaining time: {estimated_remaining_time:.2f} seconds")

# いいね数を内挿または外挿して算出
for idx, data in enumerate(video_data):
    if data['Type'] == 'Photo' and data['Likes'] is None:
        previous_likes = None
        next_likes = None
        previous_idx = None
        next_idx = None

        # 前方のいいね数を探す
        for i in range(idx - 1, -1, -1):
            if i >= 0:
                if video_data[i]['Likes'] is not None:
                    previous_likes = video_data[i]['Likes']
                    previous_idx = i
                    break

        # 後方のいいね数を探す
        for i in range(idx + 1, len(video_data)):
            if i < len(video_data):
                if video_data[i]['Likes'] is not None:
                    next_likes = video_data[i]['Likes']
                    next_idx = i
                    break

        # 内挿または外挿
        if previous_likes is not None and next_likes is not None:
            # 内挿：前後のいいね数が存在する場合は線形補間
            data['Likes'] = previous_likes + ((next_likes - previous_likes) * (idx - previous_idx) // (next_idx - previous_idx))
        elif previous_likes is not None:
            # 外挿：前方のいいね数が存在する場合
            data['Likes'] = previous_likes
        elif next_likes is not None:
            # 外挿：後方のいいね数が存在する場合
            data['Likes'] = next_likes

# 順番、楽曲名を追加しデータフレームに変換
df = pd.DataFrame(video_data)
df.insert(0, 'Index', range(len(df)))
df['Song Name'] = song_name

file_name = song_name + ".csv"
# CSVファイルに保存
df.to_csv(file_name, index=False, encoding='utf-8-sig')

# ブラウザを閉じる
driver.quit()
