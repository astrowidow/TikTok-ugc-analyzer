"""（2026-10-07）楽曲ページのグリッドの裏（/api/music/item_list/）に何が来ているかを、いろいろなページで確かめる。
一覧の段と同じ手順（ログインなし・ヘッドレス・END キー）で、増えなくなるまでスクロールする。届いた中身はページごとに保存する。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path("/Users/belle/workspace/TikTok-ugc-analyzer")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "acquire"))
sys.path.insert(0, str(ROOT / "analysis"))
import scraper  # noqa: E402
from acquire import pipeline  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402

OUT = ROOT / "output" / "grid_probe"
OUT.mkdir(parents=True, exist_ok=True)

PAGES = [
    ("シルエット（古い・大）", "https://www.tiktok.com/music/x-6796126073986762753"),
    ("シルエット（2025）", "https://www.tiktok.com/music/x-7525407952354822928"),
    ("ビジュがレベチ", "https://www.tiktok.com/music/x-7401417644957633297"),
    ("きゃわ（60秒）", "https://www.tiktok.com/music/x-7644119804865808400"),
    ("きゃわ（別の公式）", "https://www.tiktok.com/music/x-7643096893593897748"),
    ("きゃわ（小さい公式）", "https://www.tiktok.com/music/x-7643107519673420545"),
    ("ファンの音源 쿠레아", "https://www.tiktok.com/music/x-7643306519803906823"),
]

HOOK = r"""
(() => {
  if (window.__cap) return;
  window.__cap = []; window.__api = {};
  const note = u => { try { const p = new URL(String(u), location.href).pathname; if (p.startsWith('/api/')) window.__api[p] = (window.__api[p] || 0) + 1; } catch (e) {} };
  const want = u => typeof u === 'string' && /\/api\/music\/item_list/.test(u);
  const of = window.fetch;
  window.fetch = async function(input, init) {
    const u = typeof input === 'string' ? input : (input && input.url);
    note(u);
    const r = await of.apply(this, arguments);
    if (want(u)) { try { const t = await r.clone().text(); window.__cap.push({u: String(u), body: t}); } catch(e) {} }
    return r;
  };
  const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__u = u; return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function() {
    note(this.__u);
    if (want(this.__u)) this.addEventListener('load', () => { try { window.__cap.push({u: String(this.__u), body: this.responseText}); } catch(e) {} });
    return os.apply(this, arguments);
  };
})();
"""

FIELDS = {
    "再生数": lambda it: (it.get("stats") or {}).get("playCount") is not None or (it.get("statsV2") or {}).get("playCount") is not None,
    "いいね": lambda it: (it.get("stats") or {}).get("diggCount") is not None,
    "コメント数": lambda it: (it.get("stats") or {}).get("commentCount") is not None,
    "シェア": lambda it: (it.get("stats") or {}).get("shareCount") is not None,
    "保存": lambda it: (it.get("stats") or {}).get("collectCount") is not None,
    "投稿日時": lambda it: bool(it.get("createTime")),
    "説明文": lambda it: "desc" in it,
    "ハッシュタグ等(textExtra)": lambda it: "textExtra" in it,
    "投稿者ID": lambda it: bool((it.get("author") or {}).get("uniqueId")),
    "フォロワー数": lambda it: (it.get("authorStats") or {}).get("followerCount") is not None,
    "動画の長さ": lambda it: (it.get("video") or {}).get("duration") is not None,
    "カバー画像": lambda it: bool((it.get("video") or {}).get("cover")),
    "音源id": lambda it: bool((it.get("music") or {}).get("id")),
    "画面の文字(stickersOnItem)": lambda it: bool(it.get("stickersOnItem")),
    "関連する検索の語(suggestedWords)": lambda it: bool(it.get("suggestedWords")),
    "地域(locationCreated)": lambda it: bool(it.get("locationCreated")),
    "ラベル(diversificationLabels)": lambda it: bool(it.get("diversificationLabels")),
}


def play(it):
    v = (it.get("statsV2") or {}).get("playCount")
    if v is None:
        v = (it.get("stats") or {}).get("playCount")
    return int(v) if v is not None else None


def probe(d, name, url, max_scrolls=60, stall_max=4):
    d.get(url)
    time.sleep(scraper.PAGE_LOAD_TIME)
    info = pipeline.read_music_page(d)
    last, stall, i = -1, 0, 0
    for i in range(max_scrolls):
        d.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
        time.sleep(scraper.SCROLL_PAUSE_TIME)
        n = d.execute_script(scraper._COUNT_LINKS_JS) or 0
        if n <= last:
            stall += 1
            if stall >= stall_max:
                break
        else:
            stall = 0
        last = n
    hrefs = d.execute_script("return Array.from(document.querySelectorAll('a[href*=\"/video/\"], a[href*=\"/photo/\"]')).map(a => a.href).filter(h => h);") or []
    grid = []
    for h in hrefs:
        vid = h.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if vid not in grid:
            grid.append(vid)
    cap = d.execute_script("return window.__cap || []") or []
    api = d.execute_script("return window.__api || {}") or {}
    items, tops, has_more = [], None, None
    for c in cap:
        try:
            j = json.loads(c["body"])
        except Exception:
            continue
        tops = sorted(j.keys())
        has_more = j.get("hasMore")
        items += j.get("itemList") or []
    ids = []
    for it in items:
        if str(it.get("id")) not in ids:
            ids.append(str(it.get("id")))
    with open(OUT / f"{pipeline.music_id_of(url)}.jsonl", "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    g, a = set(grid), set(ids)
    cov = {k: sum(1 for it in items if fn(it)) for k, fn in FIELDS.items()}
    plays = sorted(p for p in (play(it) for it in items) if p is not None)
    music_ids = {}
    for it in items:
        mid = str((it.get("music") or {}).get("id"))
        music_ids[mid] = music_ids.get(mid, 0) + 1
    return {"name": name, "url": url, "page": info, "scrolls": i + 1, "grid": len(grid), "api_items": len(items),
            "api_unique": len(ids), "both": len(g & a), "grid_only": len(g - a), "api_only": len(a - g),
            "responses": len(cap), "last_hasMore": has_more, "top_keys": tops, "api_paths": api,
            "coverage": {k: f"{v}/{len(items)}" for k, v in cov.items()},
            "plays": {"min": plays[0] if plays else None, "median": plays[len(plays) // 2] if plays else None,
                      "max": plays[-1] if plays else None, "under_10k": sum(1 for p in plays if p < 10_000),
                      "under_50k": sum(1 for p in plays if p < 50_000)},
            "music_ids": dict(sorted(music_ids.items(), key=lambda x: -x[1])[:5]),
            "grid_order_same": grid[:50] == ids[:50]}


def main():
    d = scraper.create_headless_driver()
    res = []
    try:
        d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})
        for name, url in PAGES:
            t0 = time.time()
            try:
                r = probe(d, name, url)
            except Exception as e:
                r = {"name": name, "url": url, "error": repr(e)}
            r["seconds"] = round(time.time() - t0)
            res.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
    finally:
        d.quit()
    (OUT / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
