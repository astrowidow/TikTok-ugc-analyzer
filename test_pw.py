from playwright.sync_api import sync_playwright
import time

def scrape():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://www.tiktok.com/@giriseishun/video/7648562810393251090")
        time.sleep(5)
        # scroll down to load comments
        page.mouse.wheel(0, 1000)
        time.sleep(5)
        html = page.content()
        with open("page.html", "w") as f:
            f.write(html)
        browser.close()

if __name__ == "__main__":
    scrape()
