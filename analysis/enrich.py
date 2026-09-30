"""
既存スクレイパーの CSV（動画 URL 一覧）から、動画ページの itemStruct にある属性を取り尽くして JSONL に足す。
既存スクレイパーと同じ経路（未ログイン・ヘッドレス Chrome・__UNIVERSAL_DATA_FOR_REHYDRATION__）。
サムネイル（cover）は URL が約24時間で失効するので、その場で画像も保存する。

  python3 analysis/enrich.py "シルエット.csv" output/trial_silhouette/enriched.jsonl [--covers output/trial_silhouette/covers] [--limit N]

再開可能: 出力 JSONL に既にある video_id は飛ばす。
分析の取得の流れ（acquire/pipeline.py）からは run() を呼ぶ。between(i) を渡すと、20本ごとに呼ぶ
（待っている CSV ジョブに TikTok のロックを譲るため）。
"""
import argparse
import csv
import datetime
import json
import pathlib
import sys
import time

import requests
from selenium.webdriver.common.by import By

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scraper import create_headless_driver  # noqa: E402


def find_item_struct(data):
    """webapp.video-detail 以外（写真投稿など）にも備えて itemStruct を探す"""
    scope = data.get("__DEFAULT_SCOPE__", {})
    for key in ("webapp.video-detail", "webapp.photo-detail"):
        it = scope.get(key, {}).get("itemInfo", {}).get("itemStruct")
        if it:
            return it, key
    for key, val in scope.items():
        if isinstance(val, dict):
            it = val.get("itemInfo", {}).get("itemStruct") if isinstance(val.get("itemInfo"), dict) else None
            if it:
                return it, key
    return None, None


def g(d, *ks, default=None):
    for k in ks:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d


def extract(it):
    author = it.get("author") or {}
    astats = it.get("authorStats") or {}
    stats = it.get("statsV2") or it.get("stats") or {}
    video = it.get("video") or {}
    music = it.get("music") or {}
    te = it.get("textExtra") or []
    ct = it.get("createTime")
    sw = []
    for s in (g(it, "videoSuggestWordsList", "video_suggest_words_struct") or []):
        for w in s.get("words") or []:
            if w.get("word"):
                sw.append(w["word"])
    return {
        "video_id": it.get("id"),
        "create_time": datetime.datetime.fromtimestamp(int(ct), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ct else None,
        "desc": it.get("desc"),
        "contents": [c.get("desc") for c in (it.get("contents") or []) if c.get("desc")],
        "hashtags": [x.get("hashtagName") for x in te if x.get("hashtagName")],
        "mentions": [x.get("userUniqueId") for x in te if x.get("userUniqueId")],
        "challenges": [c.get("title") for c in (it.get("challenges") or []) if c.get("title")],
        "sticker_texts": [t for s in (it.get("stickersOnItem") or []) for t in (s.get("stickerText") or [])],
        "effect_stickers": [s.get("name") for s in (it.get("effectStickers") or []) if s.get("name")],
        "suggested_words": it.get("suggestedWords") or [],
        "video_suggest_words": sw,
        "text_language": it.get("textLanguage"),
        "location_created": it.get("locationCreated"),
        "category_type": it.get("CategoryType"),
        "channel_tags": it.get("channelTags"),
        "diversification_labels": it.get("diversificationLabels"),
        "is_ad": it.get("isAd"),
        "is_aigc": it.get("IsAigc"),
        "aigc_description": it.get("AIGCDescription"),
        "official_item": it.get("officalItem"),
        "original_item": it.get("originalItem"),
        "duet_enabled": it.get("duetEnabled"),
        "stitch_enabled": it.get("stitchEnabled"),
        "comment_status": it.get("itemCommentStatus"),
        "author": {
            "unique_id": author.get("uniqueId"),
            "nickname": author.get("nickname"),
            "signature": author.get("signature"),
            "verified": author.get("verified"),
            "private": author.get("privateAccount"),
            "tt_seller": author.get("ttSeller"),
            "created": author.get("createTime"),
            "follower_count": astats.get("followerCount"),
            "following_count": astats.get("followingCount"),
            "heart_count": astats.get("heartCount") or astats.get("heart"),
            "video_count": astats.get("videoCount"),
        },
        "stats": {k: stats.get(k) for k in ("playCount", "diggCount", "commentCount", "shareCount", "collectCount", "repostCount")},
        "video": {
            "duration": video.get("duration"),
            "width": video.get("width"),
            "height": video.get("height"),
            "ratio": video.get("ratio"),
            "cover": video.get("cover"),
            "origin_cover": video.get("originCover"),
            "subtitle_langs": sorted({(s.get("LanguageCodeName") or "") for s in (video.get("subtitleInfos") or [])} - {""}),
        },
        "music": {
            "id": music.get("id"),
            "title": music.get("title"),
            "author": music.get("authorName"),
            "original": music.get("original"),
            "duration": music.get("duration"),
        },
        "preloaded_comments": it.get("comments") if isinstance(it.get("comments"), list) and len(json.dumps(it.get("comments"))) < 20000 else None,
        "raw_keys": sorted(it.keys()),
    }


def load_urls(csv_path, done):
    urls = []
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            u = (r.get("URL") or "").strip()
            if not u:
                continue
            vid = u.rstrip("/").rsplit("/", 1)[-1]
            if vid in done:
                continue
            urls.append((vid, u, r))
    return urls


def load_done(out):
    done = set()
    if out.exists():
        for l in open(out, encoding="utf-8"):
            try:
                done.add(json.loads(l)["video_id"])
            except Exception:
                pass
    return done


def run(csv_path, out_path, covers=None, limit=0, sleep=2.0, between=None, log=print, restart_every=30):
    """属性を取り尽くす。返り値 {"ok", "fail", "skipped"}"""
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    covers = pathlib.Path(covers) if covers else None
    if covers:
        covers.mkdir(parents=True, exist_ok=True)
    done = load_done(out)
    urls = load_urls(csv_path, done)
    if limit:
        urls = urls[:limit]
    log(f"対象 {len(urls)}本（既に取得済み {len(done)}本を除く）")

    driver = create_headless_driver()
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
    ok = fail = 0
    t0 = time.time()
    try:
        with open(out, "a", encoding="utf-8") as fo:
            for i, (vid, url, row) in enumerate(urls):
                if i and restart_every and i % restart_every == 0:
                    # scraper.py と同じく、長く使うと不安定になるので作り直す
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = create_headless_driver()
                rec = {"video_id": vid, "url": url, "csv_type": row.get("Type"), "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
                try:
                    driver.get(url)
                    time.sleep(sleep)
                    tag = driver.find_element(By.XPATH, '//script[@id="__UNIVERSAL_DATA_FOR_REHYDRATION__"]')
                    data = json.loads(tag.get_attribute("innerHTML"))
                    it, where = find_item_struct(data)
                    if not it:
                        rec["error"] = "itemStruct not found"
                        rec["scope_keys"] = sorted(data.get("__DEFAULT_SCOPE__", {}).keys())
                        st = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {}).get("statusCode")
                        rec["status_code"] = st
                        fail += 1
                    else:
                        rec.update(extract(it))
                        rec["found_in"] = where
                        if covers and rec["video"].get("cover"):
                            p = covers / f"{vid}.jpg"
                            if not p.exists():
                                try:
                                    r = sess.get(rec["video"]["cover"], timeout=15)
                                    if r.ok and r.content:
                                        p.write_bytes(r.content)
                                        rec["cover_file"] = str(p)
                                except Exception as e:
                                    rec["cover_error"] = str(e)[:100]
                            else:
                                rec["cover_file"] = str(p)
                        ok += 1
                except Exception as e:
                    rec["error"] = str(e)[:200]
                    fail += 1
                fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fo.flush()
                if (i + 1) % 20 == 0 or i + 1 == len(urls):
                    el = time.time() - t0
                    log(f"  {i+1}/{len(urls)}  ok={ok} fail={fail}  {el/ (i+1):.1f}s/本  残り約{el/(i+1)*(len(urls)-i-1)/60:.0f}分")
                    if between and i + 1 < len(urls):
                        between(i + 1)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    log(f"完了 ok={ok} fail={fail}")
    return {"ok": ok, "fail": fail, "skipped": len(done)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("out")
    ap.add_argument("--covers", default=None, help="サムネイル保存先ディレクトリ")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=2.0)
    a = ap.parse_args()
    run(a.csv, a.out, covers=a.covers, limit=a.limit, sleep=a.sleep, log=lambda m: print(m, flush=True), restart_every=0)
