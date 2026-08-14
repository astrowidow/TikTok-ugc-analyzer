import asyncio
import csv
from playwright.async_api import async_playwright

# --- 設定 ---
TARGET_URL = "https://www.tiktok.com/@giriseishun/video/7648562810393251090"
OUTPUT_CSV = "comments.csv"
# ------------

async def extract_comments():
    print("Playwrightを使用してブラウザを立ち上げます...")
    
    async with async_playwright() as p:
        # headless=False で実際のブラウザ画面を表示させます
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = await context.new_page()
        
        print(f"\n{TARGET_URL} を開いています...")
        await page.goto(TARGET_URL)
        
        print("\n====================================================================")
        print("【手動操作のお願い】")
        print("ブラウザ画面を確認し、もしパズル（CAPTCHA）が出た場合は手動で解いてください。")
        print("動画の右側（または下部）にコメント欄が表示されるのを確認してください。")
        print("====================================================================\n")
        
        input("準備ができたら、ここで Enterキー を押してください...: ")
        
        print("\nコメントを読み込むためにスクロールしています...")
        
        # 下にスクロールしてコメントを読み込ませる（回数は動画のコメント数に応じて調整してください）
        for i in range(10):
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(2000) # 2秒待機して読み込みを待つ
            print(f"スクロール {i+1}/10 回目完了")
            
        print("\n画面からコメントを抽出中...")
        comments_data = []
        
        # TikTokのコメントのテキスト部分を取得（data-e2e属性を使用）
        # ※仕様変更によりセレクタが変わる可能性があります
        comment_elements = await page.locator('p[data-e2e="comment-level-1"]').all()
        
        # ユーザー名を取得するためのセレクタ
        username_elements = await page.locator('span[data-e2e="comment-username-1"]').all()
        
        # 取得できた要素数が一致していれば、ユーザー名とコメントをセットで保存
        for i in range(len(comment_elements)):
            try:
                text = await comment_elements[i].text_content()
                
                # ユーザー名も取れれば取る（取れなければ Unknown にする）
                username = "Unknown"
                if i < len(username_elements):
                    username = await username_elements[i].text_content()
                    
                comments_data.append([username, text])
                print(f"取得: {username} - {text[:30]}...")
            except Exception as e:
                print(f"1件取得スキップ: {e}")
                
        # CSVに出力
        with open(OUTPUT_CSV, mode='w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Username", "Comment"])
            writer.writerows(comments_data)
            
        print(f"\n抽出完了: 合計 {len(comments_data)} 件のコメントを '{OUTPUT_CSV}' に保存しました。")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(extract_comments())
