r"""
モニタ無し Windows で Chrome の描画が回っているかを、TikTok に触らずに確かめる。
spatest.py の check_rendering() と同じ JS を about:blank 上で走らせる（引き継ぎ書 2-9）。

  .venv\Scripts\python.exe docs\win-render-check.py [--port 9222]

判定:
  visibility=visible かつ フレーム>0  → OK（実画面の Mac と同じ条件）
  それ以外                            → ★止まっている。この状態で収集すると全動画20件で終わる
"""
import argparse
import json
import sys

from selenium import webdriver

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="9222")
ap.add_argument("--focus", action="store_true",
                help="止まっていた場合に Emulation.setFocusEmulationEnabled を試す（2-9 の対処）")
a = ap.parse_args()

o = webdriver.ChromeOptions()
o.debugger_address = f"127.0.0.1:{a.port}"
d = webdriver.Chrome(options=o)
d.set_script_timeout(30)

JS = r"""
  const done = arguments[arguments.length-1];
  let frames = 0; const t0 = performance.now();
  const tick = () => { frames++; if (performance.now()-t0 < 800) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  setTimeout(() => done({
    vis: document.visibilityState, frames: frames,
    hasFocus: document.hasFocus(),
    inner: [window.innerWidth, window.innerHeight],
    screen: [screen.width, screen.height, screen.colorDepth],
    dpr: window.devicePixelRatio,
    url: location.href
  }), 1500);
"""


def measure(label):
    r = d.execute_async_script(JS)
    ok = r.get("vis") == "visible" and r.get("frames", 0) > 0
    print(f"[{label}] 描画状態: visibility={r.get('vis')} フレーム={r.get('frames')}回/0.8秒 "
          f"{'OK' if ok else '★止まっている'}")
    print(f"        hasFocus={r.get('hasFocus')} window={r.get('inner')} "
          f"screen={r.get('screen')} dpr={r.get('dpr')} url={r.get('url')}")
    return ok


print("Chrome:", json.dumps(d.capabilities.get("browserVersion")),
      "/ windows:", len(d.window_handles))
ok = measure("素の状態")
if not ok and a.focus:
    d.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
    ok = measure("setFocusEmulationEnabled 後")
sys.exit(0 if ok else 1)
