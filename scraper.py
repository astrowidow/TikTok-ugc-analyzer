import os
import time
import datetime
import json
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By

# 進捗状況を保持するためのグローバル辞書（簡易的な実装）
# 本番環境ではRedisやDBを使うのが推奨されますが、ローカル用途ならこれで十分です
jobs = {}

def create_visible_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--mute-audio")
    return webdriver.Chrome(options=options)

def create_headless_driver():
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument("--mute-audio")
    return webdriver.Chrome(options=options)

def run_scraper(job_id: str, base_url: str, song_name: str, scroll_num: int, set_num: int = 4, is_headless_main: bool = False):
    """
    UGCのスクレイピングを実行し、結果をCSVに保存する。
    is_headless_main: 最初のURLリスト収集をバックグラウンド(非表示)で実行するかどうか
    """
    jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "message": "初期化中...",
        "total_urls": 0,
        "current_url_index": 0,
        "csv_path": None,
        "error": None
    }
    
    try:
        # WebDriverを設定して起動
        if is_headless_main:
            driver = create_headless_driver()
        else:
            driver = create_visible_driver()
            
        video_urls = set()
        scroll_pause_time = 3
        
        jobs[job_id]["message"] = f"ページを巡回してURLを収集しています... (セット数: {set_num})"

        for set_i in range(set_num):
            page_load_time = 5
            driver.get(base_url)
            time.sleep(page_load_time)

            for _ in range(scroll_num):
                driver.find_element(By.TAG_NAME, "body").send_keys(webdriver.common.keys.Keys.END)
                time.sleep(scroll_pause_time)

            video_elements = driver.find_elements(By.XPATH, '//a[contains(@href, "/video/") or contains(@href, "/photo/")]')
            for element in video_elements:
                video_url = element.get_attribute('href')
                if video_url:
                    video_urls.add(video_url)

        driver.quit()
        
        final_video_urls = list(video_urls)
        total_videos = len(final_video_urls)
        
        if total_videos == 0:
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["message"] = "URLが1件も見つかりませんでした。URLや条件を確認してください。"
            return

        jobs[job_id]["total_urls"] = total_videos
        jobs[job_id]["message"] = f"合計 {total_videos} 件の動画/写真URLを取得しました。詳細データの抽出を開始します..."

        video_data = []
        start_time = time.time()
        
        driver = create_headless_driver()
        html_load_time = 2
        restart_threshold = 30

        for idx, url in enumerate(final_video_urls):
            jobs[job_id]["current_url_index"] = idx + 1
            jobs[job_id]["progress"] = int(((idx + 1) / total_videos) * 100)
            
            if idx % restart_threshold == 0 and idx != 0:
                jobs[job_id]["message"] = "安定性のためWebDriverを再起動しています..."
                driver.quit()
                driver = create_headless_driver()
                jobs[job_id]["message"] = f"詳細データ抽出中... ({idx + 1}/{total_videos})"

            try:
                driver.get(url)
            except Exception as e:
                print(f"ページ読み込みエラー: {e}")
                continue
                
            time.sleep(html_load_time)

            try:
                if "/photo/" in url:
                    video_id_str = url.split("/")[-1]
                    video_id = int(video_id_str)
                    timestamp_bits = video_id >> 32
                    create_time = datetime.datetime.fromtimestamp(timestamp_bits).strftime('%Y-%m-%d %H:%M:%S')

                    video_data.append({
                        'URL': url,
                        'Created Date': create_time,
                        'Description': '',
                        'Likes': None,
                        'Shares': '',
                        'Comments': '',
                        'Plays': '',
                        'Saves': '',
                        'Reposts': '',
                        'Type': 'Photo'
                    })
                else:
                    script_tag = driver.find_element(By.XPATH, '//script[@id="__UNIVERSAL_DATA_FOR_REHYDRATION__"]')
                    json_text = script_tag.get_attribute('innerHTML')
                    data = json.loads(json_text)
                    video_info = data.get('__DEFAULT_SCOPE__', {}).get('webapp.video-detail', {}).get('itemInfo', {}).get('itemStruct', {})

                    create_time = int(video_info.get('createTime', None))
                    desc = video_info.get('desc', '')
                    author_info = video_info.get('author', {})
                    unique_id = author_info.get('uniqueId', '')

                    stats = video_info.get('statsV2', {})
                    if create_time:
                        create_time = datetime.datetime.fromtimestamp(create_time, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

                    digg_count = int(stats.get('diggCount', 0))
                    share_count = stats.get('shareCount', 0)
                    comment_count = stats.get('commentCount', 0)
                    play_count = stats.get('playCount', 0)
                    collect_count = stats.get('collectCount', 0)
                    repost_count = stats.get('repostCount', 0)

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

            elapsed_time = time.time() - start_time
            avg_time_per_video = elapsed_time / (idx + 1)
            estimated_remaining_time = avg_time_per_video * (total_videos - idx - 1)
            jobs[job_id]["message"] = f"詳細データ抽出中... ({idx + 1}/{total_videos}) 残り約 {int(estimated_remaining_time)}秒"

        # いいね数の補間
        jobs[job_id]["message"] = "データを整形し、CSVを作成しています..."
        for idx, data in enumerate(video_data):
            if data['Type'] == 'Photo' and data['Likes'] is None:
                previous_likes, next_likes = None, None
                previous_idx, next_idx = None, None

                for i in range(idx - 1, -1, -1):
                    if i >= 0 and video_data[i]['Likes'] is not None:
                        previous_likes = video_data[i]['Likes']
                        previous_idx = i
                        break

                for i in range(idx + 1, len(video_data)):
                    if i < len(video_data) and video_data[i]['Likes'] is not None:
                        next_likes = video_data[i]['Likes']
                        next_idx = i
                        break

                if previous_likes is not None and next_likes is not None:
                    data['Likes'] = previous_likes + ((next_likes - previous_likes) * (idx - previous_idx) // (next_idx - previous_idx))
                elif previous_likes is not None:
                    data['Likes'] = previous_likes
                elif next_likes is not None:
                    data['Likes'] = next_likes

        df = pd.DataFrame(video_data)
        if not df.empty:
            df.insert(0, 'Index', range(len(df)))
            df['Song Name'] = song_name
            
            output_dir = "output"
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
                
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            file_name = f"{song_name}_{timestamp}.csv"
            csv_path = os.path.join(output_dir, file_name)
            
            df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            
            jobs[job_id]["csv_path"] = csv_path
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["message"] = "処理が完了しました！"
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "データが取得できませんでした"

        driver.quit()

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["message"] = "エラーが発生しました。"
        try:
            driver.quit()
        except:
            pass
