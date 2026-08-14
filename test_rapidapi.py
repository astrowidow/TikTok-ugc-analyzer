import requests
import json

url = "https://tiktok-scraper2.p.rapidapi.com/video/comments"
headers = {
    "X-RapidAPI-Key": "xxx",
    "X-RapidAPI-Host": "tiktok-scraper2.p.rapidapi.com"
}
querystring = {"video_url":"https://www.tiktok.com/@giriseishun/video/7648562810393251090"}
r1 = requests.get(url, headers=headers, params=querystring)
print("Test 1 (/video/comments, video_url):", r1.status_code, r1.text[:200])

querystring2 = {"aweme_id":"7648562810393251090"}
r2 = requests.get(url, headers=headers, params=querystring2)
print("Test 2 (/video/comments, aweme_id):", r2.status_code, r2.text[:200])

url2 = "https://tiktok-scraper2.p.rapidapi.com/post/comments"
r3 = requests.get(url2, headers=headers, params=querystring)
print("Test 3 (/post/comments, video_url):", r3.status_code, r3.text[:200])

