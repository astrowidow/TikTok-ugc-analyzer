import requests
import csv
import os
import time

# ==========================================
# 【設定項目】
# 1. 取得したいTikTokの動画URL
TARGET_URL = "https://www.tiktok.com/@giriseishun/video/7648562810393251090"
OUTPUT_CSV = "comments_rapidapi.csv"

# 2. RapidAPIの認証情報
# RapidAPIにログインし、対象の「TikTok Scraper」APIにサブスクライブして取得してください。
# ※ソースコードに直接書くか、環境変数 `RAPIDAPI_KEY` に設定してください。
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "xxx")

# 採用したAPIに合わせてホスト名とエンドポイントURLを変更
RAPIDAPI_HOST = "tiktok-scraper2.p.rapidapi.com" 
ENDPOINT_URL = f"https://{RAPIDAPI_HOST}/video/comments"
# ==========================================


def extract_comments():
    print(f"RapidAPIを使用して {TARGET_URL} のコメントを取得します...")
    
    # URLから動画ID(aweme_id)を抽出
    # 例: https://www.tiktok.com/@user/video/1234567890 -> 1234567890
    video_id = TARGET_URL.rstrip('/').split('/')[-1].split('?')[0]
    print(f"抽出した動画ID: {video_id}")
    
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST
    }

    comments_data = []
    cursor = 0 # ページネーション用
    has_more = True
    
    # APIによっては1回で取得できる上限があるため、ループで回します
    while has_more:
        # tiktok-scraper2 の場合は 'video_url' というパラメータ名で渡すのが一般的です
        querystring = {
            "video_url": TARGET_URL
            # もしページネーション用の cursor 指定があればここに追加します
        }
        
        try:
            response = requests.get(ENDPOINT_URL, headers=headers, params=querystring)
            
            if response.status_code != 200:
                print(f"APIエラーが発生しました: {response.status_code}")
                print(response.text)
                break
                
            data = response.json()
            
            # APIのレスポンス形式に合わせてデータを抽出 (APIによってキー名が変わる可能性があります)
            # 多くのTikTok APIは 'data' や 'comments' というキーの中に配列でコメントを返します
            comments_list = data.get('data', {}).get('comments', []) 
            if not comments_list:
                # 別の一般的な構造パターン
                comments_list = data.get('comments', [])
                
            if not comments_list:
                print("コメントがこれ以上見つかりません、またはレスポンスの形式が異なります。")
                break
                
            for comment in comments_list:
                text = comment.get('text', '')
                user = comment.get('user', {})
                nickname = user.get('nickname', 'Unknown')
                unique_id = user.get('unique_id', 'Unknown')
                likes = comment.get('digg_count', 0)
                
                comments_data.append([unique_id, nickname, text, likes])
                print(f"取得: {nickname} - {text[:30]}...")
            
            # 次のページがあるか確認
            has_more = data.get('data', {}).get('has_more', False) or data.get('has_more', False)
            if has_more:
                cursor = data.get('data', {}).get('cursor', cursor + 30) or data.get('cursor', cursor + 30)
                time.sleep(1) # APIのレートリミット（連続アクセス制限）を避けるため少し待機
            else:
                break
                
        except Exception as e:
            print(f"通信中にエラーが発生しました: {e}")
            break

    # CSV出力
    with open(OUTPUT_CSV, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["UserID", "Nickname", "Comment", "Likes"])
        writer.writerows(comments_data)
        
    print(f"\n抽出完了: 合計 {len(comments_data)} 件のコメントを '{OUTPUT_CSV}' に保存しました。")

if __name__ == "__main__":
    extract_comments()
