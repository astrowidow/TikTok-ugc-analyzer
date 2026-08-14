import asyncio
import csv
import os
from TikTokApi import TikTokApi

# --- 設定 ---
# 抽出したいTikTok動画のURLをここに代入してください
TARGET_URL = "https://www.tiktok.com/@giriseishun/video/7648562810393251090"
# 出力するCSVファイルの名前
OUTPUT_CSV = "comments.csv"
# ------------

async def extract_comments():
    # TikTok APIの仕様上、ms_tokenが必要になることが多いです
    # 環境変数から取得するか、直接文字列で指定してください
    ms_token = os.environ.get("ms_token", "xxx")
    
    # 既存の apiTest.py などで使用している ms_token があれば以下のように置き換え可能です
    # ms_token = "YOUR_MS_TOKEN_HERE"

    print(f"動画URL: {TARGET_URL} からコメントを抽出します...")
    
    async with TikTokApi() as api:
        # セッションの作成 (Bot検知を回避するため headless=False 等に設定)
        await api.create_sessions(ms_tokens=[ms_token], num_sessions=1, sleep_after=3, headless=False, browser='webkit')
        
        # 指定URLの動画オブジェクトを取得
        video = api.video(url=TARGET_URL)
        
        comments_data = []
        try:
            # コメントの取得 (countで最大取得件数を指定。必要に応じて増やしてください)
            # 全件取得を目指す場合は大きめの数値を設定します
            async for comment in video.comments(count=1000):
                # comment.as_dict から必要な情報を抽出
                comment_dict = comment.as_dict
                
                text = comment_dict.get('text', '')
                
                # ユーザー情報
                user = comment_dict.get('user', {})
                unique_id = user.get('unique_id', 'unknown')
                nickname = user.get('nickname', '')
                
                # いいね数など
                digg_count = comment_dict.get('digg_count', 0)
                reply_comment_total = comment_dict.get('reply_comment_total', 0)
                
                # リストに追加
                comments_data.append([unique_id, nickname, text, digg_count, reply_comment_total])
                print(f"取得: {unique_id} - {text[:30]}...")
                
        except Exception as e:
            print(f"コメント取得中にエラーが発生しました: {e}")
            
        # CSVに出力
        with open(OUTPUT_CSV, mode='w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            # ヘッダー行
            writer.writerow(["User ID", "Nickname", "Comment Text", "Likes", "Replies"])
            writer.writerows(comments_data)
            
        print(f"\n抽出完了: 合計 {len(comments_data)} 件のコメントを '{OUTPUT_CSV}' に保存しました。")

if __name__ == "__main__":
    asyncio.run(extract_comments())
