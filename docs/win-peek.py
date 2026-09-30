r"""
走行中の収集用 Chrome の状態を、介入せずに読む（spatest.py の page_state 相当 + 描画 + コメント欄の高さ）。
  .venv\Scripts\python.exe win-peek.py [--port 9222]
"""
import argparse
import json

from selenium import webdriver

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="9222")
a = ap.parse_args()

o = webdriver.ChromeOptions()
o.debugger_address = f"127.0.0.1:{a.port}"
d = webdriver.Chrome(options=o)
d.set_script_timeout(30)

st = d.execute_script(r"""
  const t = (document.body.innerText || '');
  const cap = window.__cap || {};
  const hits = cap.hits || [];
  const main = document.querySelector('[class*="DivCommentMain"], [class*="DivCommentListContainer"]');
  return {
    url: location.href,
    vis: document.visibilityState,
    doc: cap.doc || null,
    icon: !!document.querySelector('[data-e2e="comment-icon"]'),
    items: document.querySelectorAll('[data-e2e="comment-level-1"]').length,
    skeleton: document.querySelectorAll('[class*="DivVirtualItemSkeleton"]').length,
    hits: hits.length,
    bad: hits.filter(h => h.status >= 400).length,
    empty: hits.filter(h => !(h.body || '').length).length,
    lastHit: hits.length ? {status: hits[hits.length-1].status, len: (hits[hits.length-1].body||'').length} : null,
    panel: main ? {scrollHeight: main.scrollHeight, clientHeight: main.clientHeight, scrollTop: main.scrollTop} : null,
    commentCountText: (document.querySelector('[data-e2e="comment-count"]') || {}).innerText || null,
    challenge: t.includes('Please wait') || t.includes('アクセスが拒否'),
    textHead: t.replace(/\s+/g, ' ').slice(0, 200)
  };
""")
r = d.execute_async_script(r"""
  const done = arguments[arguments.length-1];
  let frames = 0; const t0 = performance.now();
  const tick = () => { frames++; if (performance.now()-t0 < 800) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  setTimeout(() => done(frames), 1200);
""")
st["frames_per_0.8s"] = r
print(json.dumps(st, ensure_ascii=False, indent=1))
