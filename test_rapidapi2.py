import requests

url1 = "https://tiktok-scraper2.p.rapidapi.com/comments"
url2 = "https://tiktok-scraper2.p.rapidapi.com/video/info"
headers = {
    "X-RapidAPI-Key": "xxx",
    "X-RapidAPI-Host": "tiktok-scraper2.p.rapidapi.com"
}
querystring = {"video_url":"https://www.tiktok.com/@giriseishun/video/7648562810393251090"}

r1 = requests.get(url1, headers=headers, params=querystring)
print("Test 1 (/comments):", r1.status_code, r1.text[:200])

r2 = requests.get(url2, headers=headers, params=querystring)
print("Test 2 (/video/info):", r2.status_code, r2.text[:200])

