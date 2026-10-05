"""
仮説B の検証: 楽曲ページから「クリックで動画を開く」(SPA遷移) なら 3本の壁を越えられるか。

pagetest.py との違いはナビゲーション方式**だけ**。対照実験として成立させるため、
フック / サブリソースブロック / チャレンジ判定 / 間隔は pagetest.py をそのまま踏襲する。

  pagetest.py : 動画URLごとに driver.get()            → 3〜4本でブロック（実測済み）
  spatest.py  : 楽曲ページを1回だけ開き、以降はグリッドのリンクをクリック → 戻る

実測で分かっているDOMの事情（diag_spa.py / diag_spa2.py）:
  - リンクの中心にはホバープレビューの <video> があり、実マウスイベントは飲まれる。
    リンク要素に click を送る方式でのみ遷移する
  - 遷移先はモーダルではなく動画ページそのもの（browse-close は存在しない）。
    戻るのは history.back()

判定したいのは1点だけ:「3本の壁を越えられるか」。
  20本連続で取得できたら成功 / 途中でブロックされたら失敗。

重要な計測: doc_id（下記フックが新しいドキュメントごとに振る乱数ID）。
これが動画を開いても変わらない = 本当にSPA遷移している証拠。
変わっていたらフルリロードしており、仮説Bを検証したことにならない。

使い方:
  # ① 下見（動画は1本も開かない。DOM構造だけ確認する。ほぼ無リスク）
  python spatest.py --music-url "https://www.tiktok.com/music/..." --probe

  # ② 本番（20本連続で取れるか）
  python spatest.py --music-url "https://www.tiktok.com/music/..." --limit 20 --interval 60

--- 2026-09-30 リポジトリに取り込み（~/tiktok-overnight/spatest.py 9/11 17:06 版が元。docs/IMPLEMENTATION_LOG.md D15）---
分析の取得の流れ（acquire/pipeline.py）から使うために足したもの。既存の引数と動きはそのまま:
  --pool FILE        候補プール（analysis/pool.py の pool.tsv）。**グリッドに見えている順**に取る（必ず入れる動画を先に）。
                     プールにあってグリッドに無い動画は、同じ週のプール外でグリッドに見えている動画（再生最大）に差し替え、
                     差し替えと到達できなかった動画を --subs-out に書く。動画ごとのコメント上限は pool.tsv の cap 列
  --candidates FILE  差し替え候補の属性（records.jsonl: video_id / week / plays / author.id）
  --reply-policy targets
                     返信欄を「返信数の多いコメント上位 --reply-top 件＋質問形の親（返信1件以上）--reply-questions 件まで
                     ＋投稿者本人のコメント --reply-author 件まで」だけ開く。1スレッドは最初に開いたときの3件まで
                     （--reply-more で「あとM件表示」を押す回数）。E2E の ABLATION の提案（界隈に依らない部分）
  --deadline-hours H 取得に使う時間の上限（ロックを譲っていた時間は数えない）。超えたら区切りで止める
  between_videos     ライブラリとして使うときの口。動画の間に呼ぶ（待っている CSV ジョブにロックを譲る）
グリッドのリンク集めと対象のリンク探しは、要素ごとに get_attribute を呼ばず JS 1回で読む（TikTok への要求は変わらない）。

--- 2026-10-05 速さ（要求の間隔の設定は変えずに、待ちの無駄と無駄な要求を削る。既定はどれも切）---
  --stop-on-no-more  この動画の最新の応答が「続き無し」（has_more=0）なら、最低件数に届かなくても底で粘らない
                     （シルエット本番で41本が約75秒ずつ粘り、増えた動画は0本だった）
  --prescroll        次の要求を待っている間に、底の手前（--prescroll-margin px）まで送っておく。
                     待ちが明けてからスクロールを始めると、1ページあたり約8秒遅れていた
  --only-open-video  取る予定の無い動画への1ページ目（隣の動画の先読み）を送らない（フックが「失敗」として返す）。
                     シルエット本番で要求の14%（130回）が取らない動画への先読みだった。隣（グリッドで次）が取らない動画なら
                     開く瞬間の要求は1回なので、待ちも need=1。取る予定の動画への先読みは止めない
                     （止めるとページが失敗を覚え、その動画を開いても取り直さない。2026-10-05 の試験で0件になった）
  --remount-on-stall 開き直しを最初からはせず、底で止まって続きがあるのに増えないときだけ開き直す
                     （開き直しは先読みのデータで描かれた欄のためのもの。--only-open-video なら先読みは無い）
"""
import argparse
import collections
import csv
import datetime
import json
import os
import random
import re
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# --- ここから pagetest.py と同一 -------------------------------------------
BLOCKED = [
    "*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif", "*.svg", "*.ico",
    "*.mp4", "*.m4a", "*.m4s", "*.ts", "*.m3u8", "*.webm",
    "*.woff", "*.woff2", "*.ttf", "*.otf",
]
# テレメトリ（mon-sg.tiktokv.com / monitor_browser / monitor_web / analytics）は
# **あえて遮断しない**。帯域は軽いうえ、自分の素性を報告する経路だけを塞ぐのは
# 検知回避に見える。止めてよいのは帯域を食うメディアとフォントまで。

# 新しいドキュメントごとに実行される。__cap.doc は「このドキュメントの識別子」で、
# 動画を開く前後で変わらなければフルリロードしていない＝SPA遷移だったと言える。
HOOK = r"""
(() => {
  if (window.__cap) return;
  window.__cap = { hits: [], doc: Math.random().toString(36).slice(2, 10) };
  window.__capReset = () => { window.__cap.hits.length = 0; };
  const want = u => typeof u === 'string' && /\/api\/comment\/list/.test(u);
  // 要求した時刻とURL（cursor入り）も残す。重複要求の切り分けに要る
  // via は記録元。同じ要求が fetch と XHR の両方で拾われていないかの判別に使う
  const rec = (u, t0, status, body, via) => window.__cap.hits.push({
    url: String(u), sentAt: Math.round(t0), gotAt: Math.round(performance.now()),
    status: status, body: body, via: via });
  // ページは同じコメント要求を約100ms差で2回投げる（実測。パス・aweme_id・cursor・count が同一）。
  // 飛行中に同じ要求が来たら送らず、1件目の応答を渡す。
  // TikTokに届く要求が半分になる。取得内容は変わらない。
  const keyOf = u => {
    try { const x = new URL(u, location.origin);
          return x.pathname + '|' + (x.searchParams.get('aweme_id') || '')
               + '|' + (x.searchParams.get('cursor') || '')
               + '|' + (x.searchParams.get('count') || '')
               + '|' + (x.searchParams.get('reply_id') || ''); }
    catch (e) { return String(u); }
  };
  const inflight = new Map();
  const of = window.fetch;
  // --only-open-video: 取る予定の無い動画（__capAllow に無い）の1ページ目は送らず、失敗として返す。
  // 取る予定の動画への先読みは止めない。止めるとページが失敗を覚え、その動画を開いても取り直さない
  // （2026-10-05 の試験で、止めた隣の動画を次に開いたら0件のままだった）
  const skip = u => {
    const allow = window.__capAllow;
    if (!allow) return false;
    try { const x = new URL(u, location.origin);
          const aw = x.searchParams.get('aweme_id');
          return x.pathname.indexOf('/reply') < 0 && !!aw && !allow.has(aw)
                 && (x.searchParams.get('cursor') || '0') === '0'; }
    catch (e) { return false; }
  };
  window.fetch = function (...a) {
    const u = (a[0] && a[0].url) || a[0];
    if (!want(u)) return of.apply(this, a);
    if (skip(u)) {
      let aw = null;
      try { aw = new URL(u, location.origin).searchParams.get('aweme_id'); } catch (e) {}
      (window.__cap.drops = window.__cap.drops || []).push({aweme: aw, at: Math.round(performance.now())});
      return Promise.reject(new TypeError('Failed to fetch'));
    }
    const k = keyOf(u);
    const prev = inflight.get(k);
    if (prev) {
      window.__cap.coalesced = (window.__cap.coalesced || 0) + 1;
      // 2件目には同じ中身の応答を作って渡す（本物は1件目が読む）
      return prev.then(o => new Response(o.text, {
        status: o.status,
        headers: o.ctype ? {'content-type': o.ctype} : undefined }));
    }
    const t0 = performance.now();
    window.__cap.sent = (window.__cap.sent || 0) + 1;
    const p = of.apply(this, a).then(r =>
      r.clone().text().then(t => {
        rec(u, t0, r.status, t, 'fetch');
        return {r: r, text: t, status: r.status, ctype: r.headers.get('content-type')};
      }, () => ({r: r, text: '', status: r.status, ctype: null})));
    inflight.set(k, p);
    p.catch(() => {}).then(() => setTimeout(() => inflight.delete(k), 5000));
    return p.then(o => o.r);
  };
  const oo = XMLHttpRequest.prototype.open, os_ = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u, ...r) { this.__u = u; return oo.call(this, m, u, ...r); };
  XMLHttpRequest.prototype.send = function (...a) {
    const t0 = performance.now();
    if (want(this.__u)) window.__cap.sent = (window.__cap.sent || 0) + 1;
    this.addEventListener('load', () => {
      if (want(this.__u)) rec(this.__u, t0, this.status, this.responseText, 'xhr');
    });
    return os_.apply(this, a);
  };
})();
"""

# コメント欄を1段だけ下へ送り、その場の状態を返す。
#
# 実測（diag_scroll.py / diag_virt.py）で分かったこと:
#   - スクロール容器は [class*="DivCommentMain"]。
#     前任者のコードが掴んでいた DivCommentListContainer は**スクロールしない要素**で、
#     ここに scrollTop を代入しても何も起きなかった（第3章11の「発火しない」の正体）
#   - 一気に scrollHeight まで飛ばすと発火しない。仮想リストが途中位置を処理しないため。
#     300px ずつ刻んで送ると、ページが自分で次ページを要求する
# この動画の本体のコメントのうち、ブラウザに届いている分の cid（reached_cap）
CIDS_JS = r"""
const vid = String(arguments[0]);
const out = [];
for (const h of ((window.__cap || {}).hits || [])) {
  try {
    for (const c of (JSON.parse(h.body).comments || [])) {
      if (String(c.aweme_id) === vid && String(c.reply_id || '0') === '0') out.push(String(c.cid));
    }
  } catch (e) {}
}
return out;
"""
# 上限がこれ以下なら「1ページだけ取る」（analysis/pool.py の CAP_PAGE。TikTok は1回の要求で約20件返す）
PAGE_CAP = 20

STEP_JS = r"""
const m = document.querySelector('[class*="DivCommentMain"]');
if (!m) return null;
m.scrollTop = Math.min(m.scrollTop + arguments[0], m.scrollHeight);
const vid = String(arguments[1]);
const hs = ((window.__cap || {}).hits || []);
const seen = new Set();
let zeros = 0, mine = 0;
const topSeen = new Set();
const reps = new Set();
let lastCur = -1, lastMore = null;   // この動画の本体の応答のうち、いちばん先のページの has_more
for (const h of hs) {
  try {
    const b = JSON.parse(h.body);
    const cs = b.comments || [];
    if (cs.length && String(cs[0].aweme_id) === vid && String(cs[0].reply_id || '0') === '0'
        && Number(b.cursor || 0) > lastCur) { lastCur = Number(b.cursor || 0); lastMore = b.has_more; }
    for (const c of cs) {
      // 隣の動画の先読みが混ざるので、対象の動画のものだけ数える
      if (String(c.aweme_id) !== vid) continue;
      // reply_id が '0' 以外は返信（値は親コメントID）。本体と混ぜない
      if (String(c.reply_id || '0') !== '0') { reps.add(c.cid); continue; }
      seen.add(c.cid);
      // sort_tags.top_list=1 は TikTok の「上位コメント」ランキングの印。
      // これが尽きた後は反応の無い尾部（いいね0〜2）が続くだけ
      try { if (JSON.parse(c.sort_tags || '{}').top_list === 1) topSeen.add(c.cid); }
      catch (e) {}
    }
    // 0件応答は「もう無い」の合図。アプリはこれを何度もリトライするので、
    // 見つけたらこちらから切り上げて動画を離れる（実測で396回のリトライが起きた）
    if (cs.length === 0 && /[?&]aweme_id=/.test(h.url || '')) {
      if ((/[?&]aweme_id=(\d+)/.exec(h.url) || [])[1] === vid) zeros++;
    }
    if (cs.length && String(cs[0].aweme_id) === vid) mine++;
  } catch (e) {}
}
return {top: Math.round(m.scrollTop), h: m.scrollHeight, client: m.clientHeight,
        atBottom: m.scrollTop + m.clientHeight >= m.scrollHeight - 5,
        hits: hs.length, got: seen.size, topn: topSeen.size, replies: reps.size,
        zeros: zeros, mine: mine, more0: lastMore === 0 || lastMore === false,
        dist: m.scrollHeight - m.scrollTop - m.clientHeight,
        empty: hs.filter(h => !(h.body || '').length).length};
"""

# --prescroll: 底から margin px より手前までだけ送る（次のページを呼ばせない）。動いたかと、その場の状態を返す
PRESCROLL_JS = r"""
const m = document.querySelector('[class*="DivCommentMain"]');
if (!m) return null;
const room = m.scrollHeight - m.scrollTop - m.clientHeight - arguments[1];
if (room > 0) m.scrollTop = m.scrollTop + Math.min(arguments[0], room);
return {moved: room > 0, dist: m.scrollHeight - m.scrollTop - m.clientHeight,
        sent: (window.__cap || {}).sent || 0};
"""
# --- ここまで pagetest.py と同一 -------------------------------------------

VIDEO_LINK_XPATH = '//a[contains(@href, "/video/")]'

# 返信欄の操作。すべてページのUIを押すだけで、要求はページに組み立てさせる。
#   ① 「N件の返信を表示」で最初の3件（cursor=0）
#   ② 「あとM件表示」で3件ずつ（cursor=3, 6, …）
#   ③ 「非表示」で畳む。畳まないと次のコメントの操作対象が曖昧になる
REPLY_OPEN_JS = r"""
const minN = arguments[0];
for (const el of document.querySelectorAll('[class*="TUXButton-label"]')) {
  const t = (el.innerText || '').trim();
  const m = /^(\d+)\s*件の返信を表示$/.exec(t);
  if (!m || parseInt(m[1]) < minN) continue;
  const host = el.closest('[class*="DivVirtualItemContainer"]') || el.parentElement;
  // 畳むと「N件の返信を表示」が復活するので、処理済みには印を付けて飛ばす
  if (host && host.getAttribute('data-reply-done')) continue;
  document.querySelectorAll('[data-reply-host]')
    .forEach(x => x.removeAttribute('data-reply-host'));
  if (host) host.setAttribute('data-reply-host', '1');
  (el.closest('button,[role="button"]') || el).click();
  return parseInt(m[1]);
}
return null;
"""
REPLY_MORE_JS = r"""
// いま開いているコメントの中だけを探す。画面に複数の「あとM件表示」が
// 残っていると、どれが誰のものか分からなくなるため
const host = document.querySelector('[data-reply-host]') || document;
for (const el of host.querySelectorAll('span,div,p')) {
  if (el.childElementCount) continue;
  const t = (el.innerText || '').trim();
  const m = /^あと\s*(\d+)\s*件表示$/.exec(t);
  if (m) {
    (el.closest('button,[role="button"]') || el.parentElement || el).click();
    return parseInt(m[1]);
  }
}
return null;
"""
REPLY_COLLAPSE_JS = r"""
const host = document.querySelector('[data-reply-host]');
if (!host) return 0;
let n = 0;
for (const el of host.querySelectorAll('span,div,p')) {
  if (el.childElementCount) continue;
  if ((el.innerText || '').trim() === '非表示') {
    (el.closest('button,[role="button"]') || el.parentElement || el).click();
    n++;
    break;
  }
}
host.setAttribute('data-reply-done', '1');
host.removeAttribute('data-reply-host');
return n;
"""

# 狙ったコメントの返信欄だけを開く（--reply-policy targets）。
# 「N件の返信を表示」のボタンを、そのコメントの本文の先頭（snip）で見分ける。要求はページに組み立てさせる
REPLY_OPEN_TARGET_JS = r"""
const targets = arguments[0];
for (const el of document.querySelectorAll('[class*="TUXButton-label"]')) {
  const t = (el.innerText || '').trim();
  const m = /^(\d+)\s*件の返信を表示$/.exec(t);
  if (!m) continue;
  const host = el.closest('[class*="DivVirtualItemContainer"]') || el.parentElement;
  if (host && host.getAttribute('data-reply-done')) continue;
  const text = (host ? host.innerText : '').replace(/\s+/g, ' ');
  for (const tg of targets) {
    if (tg.snip && text.indexOf(tg.snip) >= 0) {
      document.querySelectorAll('[data-reply-host]')
        .forEach(x => x.removeAttribute('data-reply-host'));
      if (host) host.setAttribute('data-reply-host', '1');
      (el.closest('button,[role="button"]') || el).click();
      return tg.key;
    }
  }
}
return null;
"""

# 質問形の親コメント（返信に答えが付きやすい。E2E の reply_value.md で打率が最も高かった）
QUESTION_RE = re.compile(
    r"[?？]|どこ|なに|何の|なんの|何て|なんて曲|だれ|誰|どう|いつ|なんで|なぜ|どれ|教えて|曲名|"
    r"\bwhat\b|\bwhere\b|\bwho\b|\bhow\b|\bwhy\b|\bwhich\b|\bsong\b", re.I)

# グリッドのリンクを JS 1回で読む（要素ごとの get_attribute は 2,800 本で数十分かかる）
LINKS_JS = ("return Array.from(document.querySelectorAll('a[href*=\"/video/\"]'))"
            ".map(a => a.href).filter(h => h);")
FIND_ANCHOR_JS = (r"const vid = arguments[0];"
                  r"return Array.from(document.querySelectorAll('a[href*=\"/video/\"]'))"
                  r".find(a => (a.href || '').indexOf('/video/' + vid) >= 0) || null;")


def _snip(text, n=14):
    """ボタンの隣の本文と照合する先頭の文字列（空白を詰める）"""
    return re.sub(r"\s+", " ", (text or "")).strip()[:n]


def _is_top(c):
    """TikTokの「上位コメント」ランキングに入っているか。"""
    try:
        return json.loads(c.get("sort_tags") or "{}").get("top_list") == 1
    except Exception:
        return False


def vid_of(href):
    """URLから動画IDを取り出す。無ければ None。"""
    m = re.search(r"/video/(\d{6,})", href or "")
    return m.group(1) if m else None


class Blocked(Exception):
    pass


class SpaCollector:
    def __init__(self, a):
        self.a = a
        self.d = None
        self.rows = []
        self.started = time.time()
        self.base_doc = None       # 楽曲ページを開いた時点のドキュメントID
        # コメントAPIの呼び出し間隔を守るための状態。
        # 2-8のとおりコメントAPIには独自の上限があり、速く叩くと空応答が返る
        # 平均は calls_per_min、瞬間の最大は max_calls_per_min を超えないようにする。
        # 間隔にゆらぎを持たせるが、下限を max 側で固定するので上限は破らない。
        self.call_spacing = 60.0 / a.calls_per_min if a.calls_per_min else 0.0
        self.min_spacing = 60.0 / a.max_calls_per_min if a.max_calls_per_min else 0.0
        self.last_call_at = 0.0
        self.calls = 0
        self.call_times = []      # 実際に要求が飛んだ時刻。直近60秒の上限判定に使う
        self.pending_spacing = self.min_spacing
        # 捕捉したコメントを aweme_id ごとに仕分けて溜める。
        # ページは「次の動画」のコメントを先読みするので、素直に受け取ると
        # 1本ずれた別動画のデータを掴む（実測で19/20本が別動画だった）。
        # 溜めておけば先読みぶんは無駄にならず、その動画の1ページ目として使える。
        self.pool = {}
        # コメントAPIの要求の記録（cursor・送信時刻・返却件数）。重複要求の監視用
        self.req_log = []
        # ページが二重に投げようとした要求を、こちらで止めた回数
        self.coalesced = 0
        # --pool のとき: 動画ごとのコメント上限・プールの理由・投稿者（返信の対象選びに使う）
        self.caps = {}
        self.why = {}
        self.authors = {}
        self.cur_cap = a.cap
        # ライブラリとして使うときの口。動画の間に呼び、ロックを譲っていた秒数を返す
        self.between_videos = None
        self.yielded = 0.0
        # 動画ごとの本体の応答のうち、いちばん先のページの (cursor, has_more)。--stop-on-no-more の判定に使う
        # （先読みで取った1ページ目はブラウザ側の記録から消えているので、こちらに控える）
        self.page_state_of = {}
        # --prescroll: 底からこれだけ手前まで送っておく。待ちの間に次のページを呼んでしまったら広げる
        self.margin = a.prescroll_margin
        self.early = 0
        # --only-open-video: フックが送らなかった1ページ目（累計）
        self.drops = []
        self.noted_hits = 0          # 開いた瞬間に数えた要求の数（ブラウザ側の記録の件数）
        self.trigger_dists = []      # 次のページを呼んだときの底までの距離
        self.grid_ids = []           # 楽曲ページのグリッドの並び（隣の先読みの見込みに使う）
        self.allow = set()           # 取る予定の動画（--only-open-video で先読みを止めない動画）

    # ------------------------------------------------------------------
    def log(self, m):
        line = f"[{datetime.datetime.now():%H:%M:%S}] {m}"
        print(line, flush=True)
        with open(self.a.log, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def connect(self):
        o = webdriver.ChromeOptions()
        o.debugger_address = f"127.0.0.1:{self.a.port}"
        self.d = webdriver.Chrome(options=o)
        self.d.set_script_timeout(120)
        self.d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})
        # ウィンドウが他のウィンドウに隠れていると Chrome は描画を止める。
        # すると IntersectionObserver が発火せず、コメントの仮想リストが
        # スケルトンのまま実体を描画しない → 次ページも要求されない（実測で確認）。
        # これは描画を続けさせるだけで、TikTokに送る内容は何も変わらない。
        self.d.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
        if not self.a.no_block:
            self.d.execute_cdp_cmd("Network.enable", {})
            self.d.execute_cdp_cmd("Network.setBlockedURLs", {"urls": BLOCKED})
            self.log(f"サブリソースをブロック中（{len(BLOCKED)}パターン）")

    # ------------------------------------------------------------------
    def check_rendering(self):
        """描画が動いているか確かめる。止まっているとコメントが1ページ目で頭打ちになる。"""
        r = self.d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          let frames = 0; const t0 = performance.now();
          const tick = () => { frames++; if (performance.now()-t0 < 800) requestAnimationFrame(tick); };
          requestAnimationFrame(tick);
          setTimeout(() => done({vis: document.visibilityState, frames: frames}), 1500);
        """)
        ok = r.get("vis") == "visible" and r.get("frames", 0) > 0
        self.log(f"描画状態: visibility={r.get('vis')} フレーム={r.get('frames')}回/0.8秒 "
                 f"{'OK' if ok else '★止まっている'}")
        if not ok:
            self.log("  描画が止まっていると仮想リストがスケルトンのままになり、"
                     "1動画20件で頭打ちになる")
        return ok

    def page_state(self):
        try:
            return self.d.execute_script(r"""
              const t = (document.body.innerText || '');
              return {
                url: location.href,
                doc: (window.__cap || {}).doc || null,
                desc: !!document.querySelector('[data-e2e="video-desc"], [data-e2e="browse-video-desc"]'),
                modal: !!document.querySelector('[data-e2e="browse-video-desc"], [data-e2e="browse-close"]'),
                icon: !!document.querySelector('[data-e2e="comment-icon"]'),
                items: document.querySelectorAll('[data-e2e="comment-level-1"]').length,
                hits: ((window.__cap || {}).hits || []).length,
                bad: (((window.__cap || {}).hits) || []).filter(h => h.status >= 400).length,
                // HTTP 200 でも本文が空なら TikTok 側の停止シグナル。叩き続けないこと
                empty: (((window.__cap || {}).hits) || []).filter(h => !(h.body || '').length).length,
                links: document.querySelectorAll('a[href*="/video/"]').length,
                resources: performance.getEntriesByType('resource').length,
                challenge: t.includes('Please wait') || t.includes('アクセスが拒否')
              };
            """)
        except Exception as e:
            if "SecurityError" in str(e) or "Access is denied" in str(e):
                raise Blocked("403ページに遷移した（JS実行不可）")
            return {}

    def guard_challenge(self, st, since):
        """チャレンジ画面はSPA初期化中に一瞬出るので、継続時間で判定する（pagetest.py と同じ）"""
        if st.get("challenge"):
            since = since or time.time()
            if time.time() - since > self.a.challenge_grace:
                raise Blocked(f"チャレンジ画面が{self.a.challenge_grace}秒以上続いた")
            return since
        return None

    def wait_for(self, cond, timeout):
        """cond(st) が真になるまで待つ。待っている間もブロックを監視する"""
        end = time.time() + timeout
        since = None
        st = {}
        while time.time() < end:
            st = self.page_state()
            if cond(st):
                return st
            since = self.guard_challenge(st, since)
            time.sleep(1.0)
        return st

    # ------------------------------------------------------------------
    def open_music_page(self):
        """楽曲ページを1回だけフルロードする。以降の遷移は全てクリック"""
        self.log(f"楽曲ページを開く: {self.a.music_url}")
        self.d.get(self.a.music_url)
        st = self.wait_for(lambda s: s.get("links", 0) > 0, 45)
        if not st.get("links"):
            if st.get("challenge"):
                raise Blocked("楽曲ページがチャレンジ画面。まだブロック中の可能性が高い")
            raise Blocked("楽曲ページに動画リンクが1件も無い（未ログイン/表示制限を確認）")
        self.base_doc = st.get("doc")
        self.log(f"楽曲ページ読み込み完了: リンク{st['links']}件 / doc={self.base_doc}")
        self.check_rendering()

    def collect_links(self, need, want_ids=None):
        """リンクを集める（scraper.py と同じ集め方）。

        want_ids を渡した場合は、**その全部が見つかるまで**スクロールを続ける。
        対象動画がグリッドの深い位置にあることがあるため、件数だけで打ち切らない。
        """
        seen = []
        seen_set = set()
        want = set(want_ids or [])
        for i in range(self.a.collect_scrolls + 1):
            try:
                hrefs = self.d.execute_script(LINKS_JS) or []
            except Exception:
                hrefs = []
            for href in hrefs:
                if href and "/video/" in href and href not in seen_set:
                    seen.append(href)
                    seen_set.add(href)
            if want:
                got = {vid_of(h) for h in seen}
                if want <= got:
                    break          # 対象が全部見つかった
            elif len(seen) >= need:
                break
            if i < self.a.collect_scrolls:
                self.d.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
                time.sleep(2.0)
        self.log(f"リンク収集: {len(seen)}件（必要{need}件）")
        self.grid_ids = [vid_of(h) for h in seen]
        return seen

    def find_anchor(self, href):
        """グリッドから対象のリンクを探す。

        戻ると再描画されうるので毎回引き直す。URL全体ではなく**動画IDで照合**する
        （同じ動画でもユーザー名部分の表記が変わることがあるため）。
        """
        want = vid_of(href)
        for attempt in range(self.a.collect_scrolls + 1):
            try:
                el = self.d.execute_script(FIND_ANCHOR_JS, want)
            except Exception:
                el = None
            if el is not None:
                return el
            self.d.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
            time.sleep(2.0)
        return None

    # ------------------------------------------------------------------
    def open_video(self, href):
        """グリッドのリンクをクリックして動画を開く。

        ネイティブクリック / ActionChains / CDPの実マウスイベントは
        いずれも遷移しない（diag_spa2.py で確認）。リンクの中心にあるのは
        ホバープレビューの <video> 要素で、そこでイベントが飲まれるため。
        リンク要素そのものに click を送るとアプリのルータが処理して遷移する。
        """
        el = self.find_anchor(href)
        if el is None:
            return None, "anchor_not_found"
        self.d.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.5)
        target = el.get_attribute("target")
        handles_before = len(self.d.window_handles)
        self.d.execute_script("arguments[0].click();", el)
        if len(self.d.window_handles) > handles_before:
            return None, f"new_tab(target={target})"
        return "js", None

    def close_video(self):
        """楽曲ページに戻る。

        このビルドの楽曲ページはモーダルを開かず動画ページへ遷移するため、
        通常は履歴を戻る。モーダルだった場合に備えて閉じるボタンも見る。
        """
        els = self.d.find_elements(By.CSS_SELECTOR, '[data-e2e="browse-close"]')
        if els:
            try:
                els[0].click()
                how = "close_button"
            except Exception:
                self.d.execute_script("window.history.back();")
                how = "history_back"
        else:
            self.d.execute_script("window.history.back();")
            how = "history_back"
        # 楽曲ページのグリッドが戻るまで待つ。戻れないと次の動画を開けない
        self.wait_for(lambda s: "/music/" in (s.get("url") or "") and s.get("links", 0) > 0, 20)
        return how

    # ------------------------------------------------------------------
    def collect_one(self, href):
        t0 = time.time()
        vid = href.rsplit("/", 1)[-1]
        # --pool のとき動画ごとの上限（標準40、起点・大型ヒット・本人・公式は120）
        self.cur_cap = self.caps.get(vid, self.a.cap)
        res_before = self.page_state().get("resources", 0)
        # リセットせず、溜めてから進む。直前の動画にいる間に先読みされた
        # この動画のコメントが、ここで pool に入る（1ページ目が無料で手に入る）
        self.harvest()
        drops_before = len(self.drops)
        early_before = self.early
        req_before = len(self.req_log)

        # 動画を開くと「この動画の1ページ目」と「隣の動画の先読み」が
        # ほぼ同時に飛ぶ（実測111ms差）。2本ぶんの枠が空くまで待つ。
        # --only-open-video なら、隣（グリッドで次に並ぶ動画）が取る予定の無い動画のときは先読みを送らないので1本ぶん
        nb = self.neighbor_of(vid) if self.a.only_open_video else None
        need = 1 if (self.a.only_open_video and nb is not None and nb not in self.allow) else 2
        self.pace_wait(need=need)

        how, err = self.open_video(href)
        if err:
            return {"video_id": vid, "status": "open_failed", "detail": err,
                    "seconds": round(time.time() - t0, 1)}

        st = self.wait_for(lambda s: s.get("desc") and (s.get("icon") or s.get("hits")), 45)
        t_load = time.time() - t0
        doc_now = st.get("doc")
        spa = (doc_now == self.base_doc)     # ドキュメントが変わっていない＝SPA遷移

        if not st.get("desc"):
            self.close_video()
            return {"video_id": vid, "status": "no_video", "click": how, "spa": spa,
                    "seconds": round(time.time() - t0, 1)}

        if st.get("empty"):
            raise Blocked("コメントAPIが空応答（HTTP 200 / 0バイト）を返した")
        self.noted_hits = 0
        if st.get("hits"):
            self.note_call(st["hits"])   # 開いた瞬間の自動読み込みも1回に数える
            self.noted_hits = st["hits"]

        # モーダルが最初からコメントを読んでいるか（＝アイコンのクリックが不要か）
        auto = st.get("hits", 0) > 0
        if not auto:
            st = self.wait_for(lambda s: s.get("hits", 0) > 0, self.a.auto_wait)
            auto = st.get("hits", 0) > 0
        clicked_icon = False
        if not auto and st.get("icon"):
            els = self.d.find_elements(By.CSS_SELECTOR, '[data-e2e="comment-icon"]')
            if els:
                self.d.execute_script("arguments[0].click();", els[0])
                clicked_icon = True
                st = self.wait_for(lambda s: s.get("hits", 0) > 0, 25)

        # コメントパネルを開き直す。
        # 先読みされたデータで描画されたパネルは読み込み位置の状態を持たないらしく、
        # そのままスクロールしても次ページを要求しない（実測: 底に55秒留めても飛ばない）。
        # 閉じて開き直すと要求するようになる。
        # --remount-on-stall なら最初は開き直さず、底で止まったときだけ開き直す（scroll_for_more）
        self.remounted = 0
        if self.a.remount and not self.a.remount_on_stall:
            self.remount_panel()

        # コメント欄を刻んでスクロールし、ページ自身に次ページを読ませる
        scrolls, got = self.scroll_for_more(vid)

        # 返信を集める（--replies のとき）。本体を取り切ってから行う
        rep_opened = rep_got = 0
        rep_targets = []
        meta_pre, hits_pre = None, 0
        if self.a.reply_policy == "targets":
            # 対象選びには本体のコメントが要るので、ここで一度ブラウザ側の記録を読み出す。
            # harvest() は記録を消すので、総数・要求回数・異常の有無は先に控える
            meta_pre = self.api_meta(vid)
            st_pre = self.page_state()
            hits_pre = st_pre.get("hits", 0)
            if st_pre.get("bad"):
                raise Blocked(f"コメントAPIが4xx/5xxを返した（{st_pre['bad']}件）")
            if st_pre.get("empty"):
                raise Blocked("コメントAPIが空応答（HTTP 200 / 0バイト）を返した")
            self.harvest()
            rep_targets = self.reply_targets(vid, self.gather(vid))
            if rep_targets:
                rep_opened, rep_got = self.collect_reply_targets(vid, rep_targets)
        elif self.a.replies:
            rep_opened, rep_got = self.collect_replies(vid)

        # harvest() はブラウザ側の記録を消すので、消す前に読む
        meta = self.api_meta(vid) or meta_pre or {}
        st = self.page_state()
        st["hits"] = st.get("hits", 0) + hits_pre
        self.harvest()
        comments = self.gather(vid)
        reply_rows = self.gather(vid, "reply")
        if st.get("bad"):
            raise Blocked(f"コメントAPIが4xx/5xxを返した（{st['bad']}件）")
        if st.get("empty"):
            raise Blocked("コメントAPIが空応答（HTTP 200 / 0バイト）を返した")

        closed = self.close_video()
        self.wait_for(lambda s: not s.get("modal"), 15)
        if self.a.only_open_video:
            self.drops = self.d.execute_script("return ((window.__cap||{}).drops||[]).slice();") or self.drops

        return {
            "video_id": vid, "status": "ok" if comments else "no_comments",
            "fetched": len(comments), "seconds": round(time.time() - t0, 1),
            "load_seconds": round(t_load, 1), "click": how, "spa": spa,
            "doc": doc_now, "auto_comments": auto, "clicked_icon": clicked_icon,
            "scrolls": scrolls, "api_calls": st.get("hits", 0),
            # ブラウザ側ではなく取得済みコメントから数える。
            # 動画を閉じた後だとDOMが消えていて読めないため（記録がNoneになっていた）
            "top_list": sum(1 for c in comments if _is_top(c)),
            "replies_opened": rep_opened, "replies": rep_got,
            "reply_targets": [{k: t.get(k) for k in ("key", "why", "n", "opened")} for t in rep_targets],
            "cap": self.cur_cap, "pool_reason": self.why.get(vid),
            "total": meta.get("total"), "has_more": meta.get("has_more"),
            "cursor": meta.get("cursor"),
            "resources_delta": max(st.get("resources", 0) - res_before, 0),
            "closed_by": closed, "comments": comments, "reply_comments": reply_rows,
            # 2026-10-05 速さの切り替えの記録（送らなかった先読み・待ちの間に呼んでしまった回数・開き直した回数・底の手前の幅）
            "dropped": len(self.drops) - drops_before, "early": self.early - early_before,
            "remounted": self.remounted, "margin": self.margin if self.a.prescroll else None,
            # 隣の見込み（グリッドで次）と、実際に先読みされた／止めた動画。見込みが外れていないかの確かめ
            "neighbor_pred": nb, "need": need,
            "neighbor_seen": sorted({d.get("aweme") for d in self.drops[drops_before:]}
                                    | {q.get("req_aweme") for q in self.req_log[req_before:]
                                       if q.get("url_cursor") == "0" and q.get("req_aweme")
                                       and q.get("req_aweme") != vid and "reply" not in (q.get("path") or "")}),
        }

    def neighbor_of(self, vid):
        """グリッドで vid の次に並ぶ動画（ページはこれを先読みする）。分からなければ None"""
        try:
            i = self.grid_ids.index(str(vid))
        except ValueError:
            return None
        return self.grid_ids[i + 1] if i + 1 < len(self.grid_ids) else None

    def remount_panel(self):
        """コメント欄を閉じて開き直す（要求するかもしれないので間隔を空けてから開く）"""
        els = self.d.find_elements(By.CSS_SELECTOR, '[data-e2e="comment-icon"]')
        if not els:
            return
        self.d.execute_script("arguments[0].click();", els[0])   # 閉じる
        time.sleep(self.a.remount_pause)
        self.pace_wait()
        self.d.execute_script("arguments[0].click();", els[0])   # 開き直す
        time.sleep(self.a.remount_pause)
        self.remounted += 1

    def no_more(self, vid, st=None):
        """この動画の本体はもう続きが無い（いちばん先のページの応答が has_more=0）"""
        if st and st.get("more0"):
            return True
        s = self.page_state_of.get(str(vid))
        return bool(s) and not s[1]

    def pace_ready_at(self, need=1):
        """pace_wait が待たずに返る時刻（①前回からの間隔 ②直近60秒の本数）"""
        t = self.last_call_at + self.pending_spacing if self.call_spacing else 0.0
        if self.a.max_calls_per_min:
            now = time.time()
            ts = sorted(x for x in self.call_times if now - x < 60.0)
            k = len(ts) + need - int(self.a.max_calls_per_min)
            if k > 0:
                t = max(t, ts[k - 1] + 60.0)
        return t

    def prescroll_wait(self):
        """--prescroll: 次の要求を出してよい時刻まで、底の手前まで送っておく。
        待ちの間に次のページを呼んでしまったら（ページが早めに読む作りなら）、手前の幅を広げて記録する。
        呼んだかは送った数（__cap.sent）で見る。届いた数だと、前の要求の応答が遅れて届いたのを取り違える"""
        until = self.pace_ready_at()
        sent0 = None
        moved = False
        while time.time() < until - self.a.step_pause:
            p = self.d.execute_script(PRESCROLL_JS, self.a.step_px, self.margin)
            if p is None:
                break
            if sent0 is None:
                sent0 = p.get("sent", 0)
            elif p.get("sent", 0) > sent0 and moved:
                # 送ったのがこちらが動かした直後のときだけ数える（1ページ目が短い動画では、ページが自分で2ページ目を呼ぶ）
                self.early += 1
                self.margin = int(self.margin * 1.5) + 300
                self.log(f"    待ちの間に次のページを呼んだ（底まで{p.get('dist')}px）→ 手前の幅を{self.margin}pxに広げる")
                return
            moved = bool(p.get("moved"))
            if not moved:
                break
            time.sleep(self.a.step_pause)
        self.pace_wait()

    def next_spacing(self):
        """次の要求まで空ける秒数。平均は calls_per_min、最短でも min_spacing。"""
        if not self.call_spacing:
            return 0.0
        lo = self.min_spacing
        hi = max(2 * self.call_spacing - lo, lo)
        return random.uniform(lo, hi)

    def pace_wait(self, need=1):
        """次の要求を出してよい時刻まで待つ。

        ① 前回からの間隔（平均 calls_per_min、最短 min_spacing）
        ② 直近60秒の実要求数が max_calls_per_min を超えないこと
        の両方を満たすまで待つ。

        need は「これから何本飛ぶ見込みか」。動画を開く瞬間は
        その動画の1ページ目と隣の動画の先読みが同時に飛ぶので need=2 で呼ぶ。
        """
        if not self.call_spacing:
            return
        wait = self.last_call_at + self.pending_spacing - time.time()
        if wait > 0:
            time.sleep(wait)
        # 直近60秒の本数で頭打ちにする（①をすり抜けた分の保険）
        if self.a.max_calls_per_min:
            while True:
                now = time.time()
                self.call_times = [t for t in self.call_times if now - t < 60.0]
                if len(self.call_times) + need <= self.a.max_calls_per_min:
                    break
                time.sleep(max(60.0 - (now - self.call_times[0]), 0.5))

    def note_call(self, n=1):
        """コメントAPIが呼ばれたことを記録する。"""
        now = time.time()
        self.last_call_at = now
        self.calls += n
        self.call_times.extend([now] * max(n, 1))
        self.pending_spacing = self.next_spacing()

    def reached_cap(self, vid) -> bool:
        """この動画のコメント（本体）が、送らなくても上限に届いているか。ブラウザに届いた分と、先読みで溜めた分を cid で重ねて数える。
        上限が1ページ（PAGE_CAP 以下）なら、件数ではなく「1ページでも届いたか」で見る（1ページ目が19件のこともある。2026-10-06 の実走）"""
        have = len(set(self.d.execute_script(CIDS_JS, vid) or []) | {str(c.get("cid")) for c in self.gather(vid)})
        if have and self.cur_cap <= PAGE_CAP:
            self.log(f"    1ページ目が届いている（{have}件、上限{self.cur_cap}）ので送らない")
            return True
        if have >= self.cur_cap:
            self.log(f"    上限{self.cur_cap}件に届いている（{have}件）ので送らない")
            return True
        return False

    def scroll_for_more(self, vid):
        """コメント欄を少しずつ送って、ページ自身に次ページを要求させる。

        一気に底へ飛ばすと発火しない（仮想リストが途中を処理しないため）。
        底に着いても次ページは数秒遅れて届くので、すぐには諦めない。
        それでも増えないときは一度上に戻してから下り直す（実ユーザーと同じ動き）。
        読み込み判定の領域に入り直させるためで、これで復帰することがある。
        """
        steps = 0
        stall = 0
        nudges = 0
        last_hits = -1
        # 開いた瞬間の要求は collect_one で数え済み。今までは最初の1段でもう一度数えていた（開き直しの60秒待ちに隠れていた）。
        # 開き直しを最初にしない走り方では、数え直すと「直近60秒に2回」に引っかかるので、数え済みから始める
        if self.a.only_open_video or self.a.prescroll or self.a.remount_on_stall:
            last_hits = self.noted_hits
        last_got = -1
        prev_dist = None
        while steps < self.a.max_steps:
            # 2026-10-06: 上限（1本1ページなら20件）にもう届いていれば、送らずに終える。送ってから数えると、
            # 先読み（--prescroll）と1段の送りで次のページを呼んでしまう（実走で上限20件の動画が要求3回・58〜59件になった）。
            # 待つ前と、待ったあと送る直前の2回見る（待つ間に1ページ目が届くことがある）
            if self.reached_cap(vid):
                break
            # 次の要求が飛ぶ前に間隔を空ける（--prescroll なら、待つ間に底の手前まで送っておく）
            if self.a.prescroll:
                self.prescroll_wait()
            else:
                self.pace_wait()
            if self.reached_cap(vid):
                break
            st = self.d.execute_script(STEP_JS, self.a.step_px, vid)
            if st is None:
                if self.a.scroll_log:
                    self.log("    スクロール容器が見つからない（DivCommentMain なし）")
                break
            steps += 1
            if self.a.scroll_log and steps % self.a.scroll_log == 0:
                self.log(f"    {steps:>3}段 位置{st['top']}/{st['h']} 底={st['atBottom']} "
                         f"この動画{st['got']}件 全API{st['hits']}回 "
                         f"停滞{stall} 下り直し{nudges}")
            time.sleep(self.a.step_pause)
            if st.get("empty"):
                raise Blocked("コメントAPIが空応答（HTTP 200 / 0バイト）を返した")
            if st.get("zeros"):
                # これ以上は返ってこない。留まるとアプリが同じ要求を延々リトライする
                if self.a.scroll_log:
                    self.log(f"    0件応答を受けたので打ち切り（{st['got']}件で終了）")
                break
            if self.a.stop_on_no_more and self.no_more(vid, st):
                # 応答が「続き無し」と言っている。最低件数に届かなくても、粘って増えることはない
                if self.a.scroll_log:
                    self.log(f"    続き無し（has_more=0）なので打ち切り（{st['got']}件で終了）")
                break
            # 上位リストを抜けた（＝印なしのコメントが現れた）ら、そこで十分。
            # ただし --min-comments に満たないうちは続ける
            if (st["got"] > st.get("topn", 0)
                    and st["got"] >= self.a.min_comments):
                if self.a.scroll_log:
                    self.log(f"    上位リストを取り切った（上位{st['topn']}件 / "
                             f"計{st['got']}件）")
                break
            if st["got"] >= self.cur_cap:
                self.log(f"    上限{self.cur_cap}件に達した（上位リストはまだ続いている）")
                break
            if st.get("mine", 0) >= self.a.max_calls_per_video:
                self.log(f"    この動画への要求が{st['mine']}回に達したので打ち切り"
                         f"（{st['got']}件）")
                break
            if st["hits"] > last_hits and self.a.api_gap:
                time.sleep(self.a.api_gap)   # 次ページが来た直後は間を空ける
            if st["hits"] > last_hits:
                self.note_call(st["hits"] - max(last_hits, 0))
                last_hits = st["hits"]
                if prev_dist is not None:
                    self.trigger_dists.append(prev_dist)   # 次のページを呼んだときの底までの距離（--prescroll の幅の目安）
            prev_dist = st.get("dist")
            # 停滞は「その動画の取得数が増えたか」で見る。
            # hits には隣の動画の先読みも混ざるので判定に使えない
            if st["got"] > last_got:
                last_got = st["got"]
                stall = 0
                nudges = 0
            elif st["atBottom"]:
                stall += 1
                if stall >= self.a.stall_limit:
                    if self.a.remount_on_stall and not self.remounted and not self.no_more(vid, st):
                        # 続きがあるのに次のページを呼ばない欄。開き直すと呼ぶようになる（collect_one の開き直しの説明）
                        self.log(f"    底で止まった（{st['got']}件・続きあり）ので開き直す")
                        self.remount_panel()
                        stall = 0
                        continue
                    if nudges >= self.a.max_nudges:
                        break          # 戻して下り直しても増えない＝もう無い
                    self.d.execute_script(
                        "const m=document.querySelector('[class*=\"DivCommentMain\"]');"
                        "if(m) m.scrollTop = Math.max(0, m.scrollTop - arguments[0]);",
                        self.a.nudge_px)
                    time.sleep(self.a.step_pause)
                    nudges += 1
                    stall = 0
            else:
                stall = 0
        st = self.d.execute_script(STEP_JS, 0, vid) or {}
        return steps, st.get("got", 0)

    def api_meta(self):
        """最後のレスポンスが申告している総数と続きの有無。
        「20件で止まった」が完了なのか取りこぼしなのかを区別するために要る。"""
        return self.d.execute_script(r"""
          const hs = ((window.__cap||{}).hits||[]);
          for (let i = hs.length - 1; i >= 0; i--) {
            try { const b = JSON.parse(hs[i].body);
                  if (b && b.total !== undefined)
                    return {total: b.total, has_more: b.has_more, cursor: b.cursor}; }
            catch (e) {}
          }
          return null;
        """)

    def harvest(self):
        """捕捉したレスポンスを aweme_id ごとに仕分けて溜め、ブラウザ側は空にする。

        リセットしてから捕捉するのではなく、捕捉してから仕分ける。
        先読みされた隣の動画のコメントも、その動画の取り分として残せる。
        """
        recs = self.d.execute_script(r"""
          return ((window.__cap||{}).hits||[]).map(h => {
            let cur = null, n = -1, aweme = null, more = null, main = null;
            try { const b = JSON.parse(h.body);
                  cur = b.cursor; n = (b.comments||[]).length; more = b.has_more;
                  aweme = (b.comments||[])[0] ? String(b.comments[0].aweme_id) : null;
                  main = (b.comments||[])[0] ? String(b.comments[0].reply_id || '0') === '0' : null; }
            catch (e) {}
            const m = /[?&]cursor=(\d+)/.exec(h.url || '');
            const am = /[?&]aweme_id=(\d+)/.exec(h.url || '');
            const cm = /[?&]count=(\d+)/.exec(h.url || '');
            let path = null;
            try { path = new URL(h.url, location.origin).pathname; } catch (e) {}
            return {path: path, req_aweme: am ? am[1] : null, req_count: cm ? cm[1] : null,
                    url_cursor: m ? m[1] : null, via: h.via, sentAt: h.sentAt, gotAt: h.gotAt,
                    status: h.status, len: (h.body||'').length,
                    n: n, resp_cursor: cur, aweme: aweme, has_more: more, main: main, body: h.body};
          });
        """)
        for r in recs:
            # 本体の応答のうち、いちばん先のページの has_more を控える（--stop-on-no-more）
            if r.get("main") and r.get("aweme"):
                cur = int(r.get("resp_cursor") or 0)
                prev = self.page_state_of.get(r["aweme"])
                if prev is None or cur > prev[0]:
                    self.page_state_of[r["aweme"]] = (cur, r.get("has_more"))
        self.coalesced = self.d.execute_script(
            "return (window.__cap||{}).coalesced || 0;") or self.coalesced
        self.d.execute_script("window.__capReset && window.__capReset();")
        # 要求の記録は残す。同じ cursor を何度も要求していないかの監視に使う
        for r in recs:
            self.req_log.append({k: v for k, v in r.items() if k != "body"})
        bodies = [r["body"] for r in recs]
        added = 0
        for b in bodies:
            try:
                cs = json.loads(b).get("comments") or []
            except Exception:
                continue
            for c in cs:
                aid = str(c.get("aweme_id"))
                if not aid or aid == "None":
                    continue
                bucket = self.pool.setdefault(aid, {})
                if c.get("cid") not in bucket:
                    bucket[c["cid"]] = c
                    added += 1
        return added

    def collect_replies(self, vid):
        """返信を集める。ページのUIを押すだけで、要求はページに任せる。

        1コメントずつ「N件の返信を表示」→「あとM件表示」を繰り返し、
        上限まで取ったら「非表示」で畳む。畳むのは、次のコメントを操作するときに
        どの「あとM件表示」が誰のものか分からなくなるのを避けるため。
        """
        # 本体の収集直後はパネルが最下部にある。返信を持つコメントは上方にあり、
        # 仮想リストなので画面外の項目にはボタンが存在しない。
        # 最上部へ戻さないと1件も見つからない（実測: 10本中8本で0件だった）
        self.d.execute_script(
            "const m=document.querySelector('[class*=\"DivCommentMain\"]');"
            "if(m) m.scrollTop = 0;"
            "document.querySelectorAll('[data-reply-done],[data-reply-host]')"
            "  .forEach(x => {x.removeAttribute('data-reply-done');"
            "                 x.removeAttribute('data-reply-host');});")
        time.sleep(1.5)

        opened = 0
        got_total = 0
        while opened < self.a.reply_max_comments:
            self.pace_wait()
            n = self.d.execute_script(REPLY_OPEN_JS, self.a.reply_min)
            if n is None:
                # 画面内に対象が無い。下へ送って探す
                st = self.d.execute_script(STEP_JS, self.a.step_px, vid)
                if st is None or st.get("atBottom"):
                    break
                time.sleep(self.a.step_pause)
                continue
            opened += 1
            # wait_replies が返すのは動画全体の返信の累計。
            # 1コメントぶんは「開く前」との差で数える（累計で判定すると
            # 2コメント目以降が即座に上限扱いになり3件で止まる。実測で起きた）
            base = (self.d.execute_script(STEP_JS, 0, vid) or {}).get("replies", 0)
            got = self.wait_replies(vid, base)
            while (got - base) < self.a.reply_max:
                self.pace_wait()
                more = self.d.execute_script(REPLY_MORE_JS)
                if more is None:
                    break
                before = got
                got = self.wait_replies(vid, before)
                if got <= before:
                    break        # 増えなくなった＝もう無い
            got_total += got - base
            self.d.execute_script(REPLY_COLLAPSE_JS)
            time.sleep(0.5)
            if self.a.scroll_log:
                self.log(f"    返信: {n}件のコメントを開いて{got - base}件取得")
        return opened, got_total

    def reply_targets(self, vid, comments):
        """返信欄を開くコメントを選ぶ（--reply-policy targets）。

        返信数の多い上位 --reply-top 件、質問形の親（返信1件以上）--reply-questions 件まで、
        投稿者本人のコメント --reply-author 件まで。返信が全部同梱で届いているものは開かない。
        """
        def unseen(c):
            total = c.get("reply_comment_total") or 0
            return total >= 1 and total > len(c.get("reply_comment") or [])

        cands = [c for c in comments if unseen(c) and _snip(c.get("text"))]
        chosen, keys = [], set()

        def take(cs, why, k):
            n = 0
            for c in cs:
                if n >= k:
                    break
                if c["cid"] in keys:
                    continue
                keys.add(c["cid"])
                chosen.append({"key": str(c["cid"]), "snip": _snip(c.get("text")),
                               "n": c.get("reply_comment_total") or 0, "why": why, "opened": False})
                n += 1

        take(sorted(cands, key=lambda c: -(c.get("reply_comment_total") or 0)), "top", self.a.reply_top)
        take(sorted([c for c in cands if QUESTION_RE.search(c.get("text") or "")],
                    key=lambda c: -(c.get("digg_count") or 0)), "question", self.a.reply_questions)
        author = self.authors.get(str(vid))
        if author:
            take([c for c in cands if (c.get("user") or {}).get("unique_id") == author],
                 "author", self.a.reply_author)
        return chosen

    def collect_reply_targets(self, vid, targets):
        """選んだコメントの返信欄だけを開く。ページのUIを押すだけで、要求はページに任せる"""
        self.d.execute_script(
            "const m=document.querySelector('[class*=\"DivCommentMain\"]');"
            "if(m) m.scrollTop = 0;"
            "document.querySelectorAll('[data-reply-done],[data-reply-host]')"
            "  .forEach(x => {x.removeAttribute('data-reply-done');"
            "                 x.removeAttribute('data-reply-host');});")
        time.sleep(1.5)
        opened = got_total = steps = 0
        remaining = list(targets)
        while remaining and steps < self.a.max_steps:
            self.pace_wait()
            key = self.d.execute_script(REPLY_OPEN_TARGET_JS,
                                        [{"key": t["key"], "snip": t["snip"]} for t in remaining])
            if key is None:
                st = self.d.execute_script(STEP_JS, self.a.step_px, vid)
                steps += 1
                if st is None or st.get("atBottom"):
                    break
                time.sleep(self.a.step_pause)
                continue
            t = next((x for x in remaining if x["key"] == key), None)
            if t:
                t["opened"] = True
                remaining.remove(t)
            opened += 1
            base = (self.d.execute_script(STEP_JS, 0, vid) or {}).get("replies", 0)
            got = self.wait_replies(vid, base)
            more = 0
            while more < self.a.reply_more:
                self.pace_wait()
                if self.d.execute_script(REPLY_MORE_JS) is None:
                    break
                before = got
                got = self.wait_replies(vid, before)
                more += 1
                if got <= before:
                    break
            got_total += got - base
            self.d.execute_script(REPLY_COLLAPSE_JS)
            time.sleep(0.5)
        if remaining and self.a.scroll_log:
            self.log(f"    返信: 見つからなかった対象 {len(remaining)}件")
        return opened, got_total

    def wait_replies(self, vid, before):
        """返信が届くのを待って、その時点の返信数を返す。"""
        end = time.time() + 15
        last = before
        while time.time() < end:
            time.sleep(1.0)
            st = self.d.execute_script(STEP_JS, 0, vid) or {}
            if st.get("empty"):
                raise Blocked("コメントAPIが空応答（HTTP 200 / 0バイト）を返した")
            n = st.get("replies", 0)
            if n > last:
                self.note_call()
                last = n
                end = time.time() + 4      # 続けて届くことがあるので少し待つ
        return last

    def gather(self, vid, kind="main"):
        """その動画のコメントを返す。

        kind='main' は本体（reply_id が '0'）、'reply' は返信。
        親コメントは返信側の reply_id で辿れる。
        """
        vals = self.pool.get(str(vid), {}).values()
        if kind == "reply":
            return [c for c in vals if str(c.get("reply_id") or "0") != "0"]
        return [c for c in vals if str(c.get("reply_id") or "0") == "0"]

    def api_meta(self, vid):
        """その動画のレスポンスが申告している総数と続きの有無。

        隣の動画のレスポンスを読んでしまうと総数がずれるので、
        コメントの aweme_id が一致するものだけを見る。
        """
        return self.d.execute_script(r"""
          const vid = String(arguments[0]);
          const hs = ((window.__cap||{}).hits||[]);
          for (let i = hs.length - 1; i >= 0; i--) {
            try {
              const b = JSON.parse(hs[i].body);
              const cs = b.comments || [];
              // 返信のレスポンス（reply_id が '0' 以外）は総数の意味が違うので除く。
              // 混ぜると「上位300件なのに総数73」のような表示になる
              if (cs.length && String(cs[0].aweme_id) === vid
                  && String(cs[0].reply_id || '0') === '0')
                return {total: b.total, has_more: b.has_more, cursor: b.cursor};
            } catch (e) {}
          }
          return null;
        """, str(vid))

    def load_targets(self):
        """対象動画のリストをファイルから読む。

        1行に1件。CSV / TSV / URLだけの行のいずれでもよく、
        行の中から TikTok の動画URL（または19桁のID）を拾う。
        既存スクレイパーのCSVをそのまま渡せる。
        """
        ids, order = set(), []
        with open(self.a.videos, encoding="utf-8-sig") as f:
            for line in f:
                v = vid_of(line)
                if not v:
                    m = re.search(r"\b(\d{15,25})\b", line)
                    v = m.group(1) if m else None
                if v and v not in ids:
                    ids.add(v)
                    order.append(v)
        return order

    def match_targets(self, targets):
        """対象動画がグリッド上に出てくるか突き合わせる。

        SPA遷移はグリッドのリンクを押す方式なので、**グリッドに出ない動画には到達できない**。
        どれが見つからなかったかを必ず報告すること。
        """
        # 対象が全部見つかるまでスクロールする（件数では打ち切らない）
        grid = self.collect_links(10 ** 9, want_ids=targets)
        by_id = {}
        for h in grid:
            v = vid_of(h)
            if v and v not in by_id:
                by_id[v] = h
        found = [(v, by_id[v]) for v in targets if v in by_id]
        missing = [v for v in targets if v not in by_id]
        self.log(f"対象動画の突き合わせ: 指定{len(targets)}本 / "
                 f"グリッドで見つかった{len(found)}本 / 見つからない{len(missing)}本"
                 f"（グリッド収集{len(grid)}本）")
        if missing:
            self.log(f"  見つからない動画（SPA遷移では到達できない）: "
                     f"{', '.join(missing[:10])}{' ほか' if len(missing) > 10 else ''}")
        return found, missing

    def pool_targets(self, done):
        """--pool: プールの動画をグリッドに見えている順に並べる（必ず入れる動画を先に）。

        グリッドに無いプールの動画は、同じ週のプール外でグリッドに見えている動画（再生最大）に差し替える。
        楽曲ページのグリッドは推薦順で入れ替わるので、プールの全部に届くとは限らない（E2E で 65本中38本が不到達）。
        """
        rows = list(csv.DictReader(open(self.a.pool, encoding="utf-8"), delimiter="\t"))
        info = {}
        label_only = set()
        for r in rows:
            if str(r.get("cap") or "").strip() == "0":   # cap 0 はコメントを取らない動画（ラベルを付けるだけ。analysis/pool.py の page）
                label_only.add(r["video_id"])
                continue
            info[r["video_id"]] = r
            self.caps[r["video_id"]] = int(r.get("cap") or self.a.cap)
            self.why[r["video_id"]] = r.get("reasons")
        cand = {}
        if self.a.candidates:
            for line in open(self.a.candidates, encoding="utf-8"):
                o = json.loads(line)
                cand[str(o["video_id"])] = o
                aid = (o.get("author") or {}).get("id")
                if aid:
                    self.authors[str(o["video_id"])] = aid
        # 前回までの走行で差し替え済みのプールの動画（再開で二重に差し替えない）
        covered = set()
        if os.path.exists(self.a.out):
            for line in open(self.a.out, encoding="utf-8"):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("status") == "ok" and str(r.get("pool_reason") or "").startswith("substitute_for:"):
                    covered.add(r["pool_reason"].split(":", 1)[1])
                    self.why[r["video_id"]] = r["pool_reason"]
        want = [v for v in info if v not in done and v not in covered]
        grid = self.collect_links(10 ** 9, want_ids=want)
        pos, by_id = {}, {}
        for i, h in enumerate(grid):
            v = vid_of(h)
            if v and v not in pos:
                pos[v] = i
                by_id[v] = h
        found = [v for v in want if v in pos]
        missing = [v for v in want if v not in pos]
        used = set(info) | set(done) | label_only   # 差し替え先はプールの外から（前と同じ）
        subs = []
        for v in sorted(missing, key=lambda v: (info[v].get("priority") or "2", -int(info[v].get("plays") or 0))):
            wk = info[v].get("week")
            pick, best = None, -1
            for u in pos:
                c = cand.get(u)
                if u in used or not c or c.get("week") != wk:
                    continue
                if (c.get("plays") or 0) > best:
                    pick, best = u, c.get("plays") or 0
            if pick:
                used.add(pick)
                self.caps[pick] = self.a.subs_cap
                self.why[pick] = f"substitute_for:{v}"
            subs.append({"pool_video": v, "week": wk, "priority": info[v].get("priority"),
                         "reasons": info[v].get("reasons"), "substitute": pick,
                         "substitute_plays": best if pick else None})
        self.missing = [s["pool_video"] for s in subs if not s["substitute"]]
        self.substitutions = subs
        if self.a.subs_out:
            with open(self.a.subs_out, "w", encoding="utf-8", newline="\n") as f:
                f.write(f"# {datetime.datetime.now().isoformat(timespec='seconds')} "
                        f"グリッド{len(grid)}本 / プール{len(info)}本（取得済み{len(done)}・差し替え済み{len(covered)}）"
                        f" / 見つかった{len(found)} / 見つからない{len(missing)} / 差し替え{sum(1 for s in subs if s['substitute'])}\n")
                f.write("pool_video\tweek\tpriority\treasons\tsubstitute\tsubstitute_plays\n")
                for s in subs:
                    f.write(f"{s['pool_video']}\t{s['week']}\t{s['priority']}\t{s['reasons']}\t"
                            f"{s['substitute'] or ''}\t{s['substitute_plays'] if s['substitute_plays'] is not None else ''}\n")
        self.log(f"プールの突き合わせ: プール{len(info)}本（取得済み{len(done)}・差し替え済み{len(covered)}を除く{len(want)}本）/ "
                 f"グリッドで見つかった{len(found)}本 / 見つからない{len(missing)}本 → "
                 f"差し替え{sum(1 for s in subs if s['substitute'])}本・差し替え先なし{len(self.missing)}本（グリッド{len(grid)}本）")
        picks = [s["substitute"] for s in subs if s["substitute"]]
        first = sorted([v for v in found if (info[v].get("priority") or "2") == "1"], key=lambda v: pos[v])
        rest = sorted([v for v in found if (info[v].get("priority") or "2") != "1"] + picks, key=lambda v: pos[v])
        return [by_id[v] for v in first + rest]

    def load_done(self):
        """既に取得できている動画を出力ファイルから読み出す。

        途中で止まっても取り直さないため。ブロックされて中断したときも
        そこまでのぶんは書き出し済みなので、そのまま続きから走らせられる。
        """
        if not self.a.resume or not os.path.exists(self.a.out):
            return set()
        done = set()
        with open(self.a.out, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("status") != "ok":
                    continue
                # 取り切れていないものは、指示があれば取り直す
                if (self.a.redo_partial and r.get("has_more")
                        and len(r.get("comments") or []) < self.a.cap):
                    continue
                done.add(r["video_id"])
        return done

    def probe(self):
        """下見: 動画は1本も開かず、DOM構造だけ報告する。

        --videos を付けると「指定した動画にグリッドから到達できるか」も確かめる。
        動画を1本も開かないので、ブロックの予算を消費しない。
        """
        self.connect()
        self.open_music_page()
        if self.a.videos:
            targets = self.load_targets()
            self.log(f"対象動画リスト: {len(targets)}本")
            found, missing = self.match_targets(targets)
            print("\n" + "=" * 60)
            print(f"指定    : {len(targets)}本")
            print(f"到達可能: {len(found)}本 ({100*len(found)/len(targets) if targets else 0:.0f}%)")
            print(f"到達不可: {len(missing)}本")
            if missing:
                print("\nグリッドに出てこなかった動画（SPA遷移では取れない）:")
                for v in missing[:20]:
                    print(f"  {v}")
                if len(missing) > 20:
                    print(f"  …ほか{len(missing)-20}本")
            print("\n※ このモードでは動画を1本も開いていません。")
            print("=" * 60)
            return
        links = self.collect_links(self.a.limit)
        info = self.d.execute_script("""
          const as = [...document.querySelectorAll('a[href*="/video/"]')].slice(0, 5);
          return as.map(a => ({href: a.href, target: a.getAttribute('target') || '(なし)'}));
        """)
        print("\n" + "=" * 60)
        print(f"収集できたリンク: {len(links)}件（本番に必要: {self.a.limit}件）")
        print(f"ドキュメントID  : {self.base_doc}")
        print("\n先頭5件の target 属性（'(なし)' ならクリックでSPA遷移になる）:")
        for x in info:
            print(f"  target={x['target']:<8} {x['href']}")
        print("\n※ このモードでは動画を1本も開いていません。")
        print("=" * 60)

    def run(self):
        self.connect()
        self.open_music_page()
        done = self.load_done()
        self.missing = []
        self.not_fetched = []
        if self.a.pool:
            links = self.pool_targets(done)
        elif self.a.videos:
            # 指定されたリストの順に取る
            targets = self.load_targets()
            self.log(f"対象動画リスト: {len(targets)}本（{self.a.videos}）")
            targets = [v for v in targets if v not in done]
            if done:
                self.log(f"  うち取得済み{len(done)}本を除外 → 残り{len(targets)}本")
            targets = targets[self.a.start:self.a.start + self.a.limit]
            found, self.missing = self.match_targets(targets)
            links = [h for _, h in found]
        else:
            # 指定が無ければ楽曲ページのグリッド順
            links = self.collect_links(self.a.start + self.a.limit + len(done))
            links = links[self.a.start:]
            remaining = self.a.limit
            if done:
                before = len(links)
                links = [h for h in links if vid_of(h) not in done]
                # --limit は「目標の総本数」。取得済みぶんを差し引く
                remaining = max(self.a.limit - len(done), 0)
                self.log(f"再開: 取得済み{len(done)}本 / 目標{self.a.limit}本 "
                         f"→ 今回は{remaining}本を取る（候補{before}本）")
            links = links[:remaining]
        if not self.a.pool and len(links) < self.a.limit:
            self.log(f"注意: リンクが{len(links)}件しか集まらなかった（--collect-scrolls を増やす）")
        if self.a.only_open_video:
            # 取る予定の動画への先読みは止めない（フックの skip の説明）
            self.allow = {v for v in (vid_of(h) for h in links) if v}
            self.d.execute_script("window.__capAllow = new Set(arguments[0]);", sorted(self.allow))
        self.log(f"開始: {len(links)}動画 / 間隔{self.a.interval}秒 / "
                 f"上位リストが尽きるまで（最低{self.a.min_comments}件・上限{self.a.cap}件）/ "
                 f"サブリソースブロック={not self.a.no_block}")
        out = open(self.a.out, "a", encoding="utf-8")
        fails = 0
        for i, href in enumerate(links, 1):
            if i > 1:
                # 機械的な等間隔にしない。負荷のピークをならす意味もある
                time.sleep(self.a.interval * random.uniform(1.0, 1.0 + self.a.jitter))
                if self.between_videos:
                    # 待っている CSV ジョブにロックを譲る（譲っていた時間は上限に数えない）
                    self.yielded += self.between_videos() or 0.0
            if self.a.deadline_hours and (time.time() - self.started - self.yielded) > self.a.deadline_hours * 3600:
                self.not_fetched = [vid_of(h) for h in links[i - 1:]]
                self.log(f"時間の上限（{self.a.deadline_hours}時間）に達したので止めます。"
                         f"残り{len(self.not_fetched)}本は取っていない")
                break
            try:
                r = self.collect_one(href)
            except Blocked as e:
                self.log(f"!! {i}件目でブロック検知: {e} → 中断します")
                self.log(f"   ここまでの{i-1}本は {self.a.out} に保存済み。"
                         f"時間を空けて --resume を付けて再実行すれば続きから走ります")
                self.rows.append({"i": i, "video_id": href.rsplit('/', 1)[-1], "status": "blocked"})
                break
            except Exception as e:
                fails += 1
                self.log(f"{i}件目 例外: {type(e).__name__}: {str(e)[:120]}")
                self.rows.append({"i": i, "video_id": href.rsplit('/', 1)[-1], "status": "error"})
                if fails >= 2:
                    self.log("連続失敗のため中断")
                    break
                continue
            fails = 0
            r["i"] = i
            # クリックがフルリロードなら driver.get() と同じことをしているだけで、
            # 仮説Bを検証したことにならない。動画を無駄に消費する前に止める
            if i == 1 and self.a.require_spa and r.get("spa") is False:
                self.rows.append({k: v for k, v in r.items() if k != "comments"})
                self.log("!! 1本目がフルリロードだった（doc が変わった）。"
                         "クリックはSPA遷移になっていない → 実験が成立しないので中断")
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
                break
            self.rows.append({k: v for k, v in r.items() if k != "comments"})
            self.log(
                f"{i}/{len(links)} {r['video_id']}: {r.get('fetched', 0)}件 "
                f"{r['seconds']}秒(表示{r.get('load_seconds')}秒) "
                f"SPA={'はい' if r.get('spa') else 'いいえ(フルリロード)'} "
                f"API{r.get('api_calls')}回 スクロール{r.get('scrolls')}段 "
                f"上位{r.get('top_list')}件 総数{r.get('total')}"
                + (f" 返信{r.get('replies')}件/{r.get('replies_opened')}コメント"
                   if self.a.replies or self.a.reply_policy == "targets" else "")
                + (f" 上限{r.get('cap')}" if self.a.pool else "")
                + (f" 先読みを送らず{r.get('dropped')}" if self.a.only_open_video else "")
                + (f" 待ち中に呼んだ{r.get('early')}" if self.a.prescroll else "")
                + (f" 開き直し{r.get('remounted')}" if self.a.remount_on_stall else ""))
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            out.flush()
            self.write_summary(len(links))
        out.close()
        self.report(len(links))

    # ------------------------------------------------------------------
    def write_summary(self, planned=None):
        """サマリを書き出す。1動画ごとに呼ぶ。

        走行が途中で止まるとペース検証に要る要求単位の記録が失われるため、
        最後にまとめて書かない。
        """
        el = time.time() - self.started
        json.dump({"rows": self.rows, "elapsed_min": round(el / 60, 1),
                   "planned": planned, "base_doc": self.base_doc,
                   "missing": getattr(self, "missing", []),
                   "not_fetched_time": getattr(self, "not_fetched", []),
                   "substitutions": getattr(self, "substitutions", []),
                   "yielded_min": round(self.yielded / 60, 1),
                   "api_calls": self.calls,
                   "requests": self.req_log,
                   "speed_flags": {k: getattr(self.a, k) for k in ("stop_on_no_more", "prescroll", "only_open_video",
                                                                   "remount_on_stall")},
                   "dropped_prefetch": self.drops, "early_triggers": self.early,
                   "prescroll_margin": self.margin, "trigger_dists": self.trigger_dists,
                   "calls_per_min": round(self.calls / (el / 60), 2) if el else None},
                  open(self.a.summary, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    def report(self, planned):
        ok = [r for r in self.rows if r.get("status") == "ok"]
        blocked = [r for r in self.rows if r.get("status") == "blocked"]
        el = time.time() - self.started
        print("\n" + "=" * 60)
        print(f"【仮説B 判定】")
        if blocked:
            print(f"  失敗: {blocked[0]['i']}件目でブロック。")
            print(f"  （対照: driver.get() 方式は4件目でブロック / 間隔60秒）")
        elif len(ok) >= planned >= 20:
            print(f"  成功: {len(ok)}本を連続で取得。3本の壁を越えた")
        elif len(ok) >= planned:
            print(f"  ブロックされずに{len(ok)}本完走。ただし判定には20本必要"
                  f"（--limit {planned} で実行された）")
        else:
            print(f"  判定不能: ブロックはされていないが完走もしていない"
                  f"（成功{len(ok)}/{planned}件）。下の内訳を確認すること")
        print("-" * 60)
        print(f"完走: {len(ok)}/{len(self.rows)}件 / 経過 {el/60:.1f}分")
        n_req = len(self.req_log) or self.calls
        if n_req:
            print(f"コメントAPI: 実要求 {n_req}回 / 実績 {n_req/(el/60):.2f}回/分 "
                  f"(目標 平均{self.a.calls_per_min} / 上限{self.a.max_calls_per_min}回/分)")
            # 直近60秒の窓で見た瞬間最大。上限を破っていないかの検算
            ts = sorted(r["gotAt"] for r in self.req_log if r.get("gotAt") is not None)
            if len(ts) > 1:
                peak, j = 0, 0
                for i in range(len(ts)):
                    while ts[i] - ts[j] > 60000:
                        j += 1
                    peak = max(peak, i - j + 1)
                print(f"   瞬間最大（60秒窓）: {peak}回/分")
        if self.coalesced:
            print(f"重複要求の抑制: {self.coalesced}回ぶん送らずに済ませた")
        if self.req_log:
            # 動画とcursorの**組**で数える。cursorだけで数えると、
            # 別の動画の cursor=20 まで重複に見えてしまう
            cur = collections.Counter(
                (r.get("req_aweme"), r.get("url_cursor")) for r in self.req_log)
            dup = sum(v - 1 for v in cur.values() if v > 1)
            got = sum(len(v) for v in self.pool.values())
            print(f"要求の無駄: 同じcursorの重複 {dup}回 / 全{len(self.req_log)}回"
                  f"（20件×{len(self.req_log)}回={20*len(self.req_log)}件ぶん要求して"
                  f"実データ{got}件）")

        # SPA遷移が本当に起きていたか。ここが「いいえ」だと仮説Bを検証したことにならない
        spa_yes = [r for r in self.rows if r.get("spa") is True]
        spa_no = [r for r in self.rows if r.get("spa") is False]
        print(f"SPA遷移だった: {len(spa_yes)}件 / フルリロードだった: {len(spa_no)}件")
        if spa_no:
            print("  ★ フルリロードが混じっている。クリックが遷移になっておらず、")
            print("     この結果では仮説Bを検証したことにならない")

        if ok:
            secs = [r["seconds"] for r in ok]
            avg = sum(secs) / len(secs)
            auto = sum(1 for r in ok if r.get("auto_comments"))
            print(f"取得コメント: {sum(r['fetched'] for r in ok):,}件")
            print(f"1動画あたり所要: 平均{avg:.1f}秒 (最短{min(secs):.1f} / 最長{max(secs):.1f})")
            print(f"  ※ 対照（driver.get方式）の実測は15.7秒")
            print(f"アイコンのクリック無しでコメントが出た: {auto}/{len(ok)}件")
            print(f"\n【714動画への外挿】1動画 = 所要{avg:.0f}秒 + 待機")
            for gap in (15, 30, 60, 90):
                print(f"   待機{gap:>3}秒 → {714*(avg+gap)/3600:>5.1f}時間")

        cut = [r for r in ok if r.get("top_list") and r.get("fetched")
               and r["fetched"] <= r["top_list"]]
        if ok and self.a.replies:
            ro = sum(r.get("replies_opened") or 0 for r in ok)
            rg = sum(r.get("replies") or 0 for r in ok)
            print(f"返信: {rg:,}件を{ro}コメントから取得"
                  f"（閾値{self.a.reply_min}件以上 / 1コメント上限{self.a.reply_max}件）")
        if ok:
            print(f"上位リストを取り切った: {len(ok)-len(cut)}/{len(ok)}本"
                  + (f" / 途中で打ち切り {len(cut)}本" if cut else ""))
            for r in cut[:5]:
                print(f"    {r['video_id']}: {r['fetched']}件（上位{r['top_list']}件）")

        if getattr(self, "missing", None):
            print(f"\n★ グリッドに出てこず取得できなかった動画: {len(self.missing)}本")
            for v in self.missing[:10]:
                print(f"    {v}")
            if len(self.missing) > 10:
                print(f"    …ほか{len(self.missing)-10}本")

        other = [r for r in self.rows if r.get("status") not in ("ok", "blocked")]
        if other:
            print("\nその他の結果:")
            for r in other:
                print(f"   {r.get('i')}件目 {r.get('video_id')}: "
                      f"{r.get('status')} {r.get('detail', '')}")
        print("=" * 60)
        self.write_summary(planned)


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-url", required=True, help="楽曲ページのURL（ここだけフルロードする）")
    ap.add_argument("--port", default="9222")
    ap.add_argument("--limit", type=int, default=20, help="連続で開く動画の本数。20本で判定する")
    ap.add_argument("--start", type=int, default=0, help="何番目のリンクから始めるか")
    ap.add_argument("--videos", help="対象動画のリスト（CSV/TSV/URL行）。"
                                     "指定するとグリッド順ではなくこの順で取る")
    ap.add_argument("--resume", action="store_true",
                    help="出力ファイルに既にある動画を飛ばして続きから走らせる")
    ap.add_argument("--redo-partial", action="store_true",
                    help="--resume 時、取り切れていない動画（続きありで上限未満）は取り直す")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--cap", type=int, default=300,
                    help="1動画あたりの上限（安全弁）。通常は上位リストが尽きて先に止まる")
    ap.add_argument("--replies", action="store_true",
                    help="返信も取る（ページの「N件の返信を表示」を押す方式）")
    ap.add_argument("--reply-min", type=int, default=10,
                    help="返信がこの件数以上のコメントだけ開く")
    ap.add_argument("--reply-max", type=int, default=10,
                    help="1コメントあたり最大これだけ返信を取る（3件ずつ届く）")
    ap.add_argument("--reply-max-comments", type=int, default=30,
                    help="1動画あたり返信を開くコメント数の上限")
    ap.add_argument("--min-comments", type=int, default=20,
                    help="上位リストが早く尽きても、最低これだけは取る")
    ap.add_argument("--max-steps", type=int, default=200,
                    help="1動画あたりのスクロール段数の上限")
    ap.add_argument("--step-px", type=int, default=300, help="1段で送る量。大きすぎると発火しない")
    ap.add_argument("--step-pause", type=float, default=1.0, help="1段ごとの待ち")
    ap.add_argument("--stall-limit", type=int, default=12,
                    help="底で何段ぶん増えなかったら戻して下り直すか")
    ap.add_argument("--nudge-px", type=int, default=900,
                    help="下り直すときに一度戻す量")
    ap.add_argument("--max-nudges", type=int, default=3,
                    help="下り直しを何回まで試すか。これを使い切ったら終わり")
    ap.add_argument("--calls-per-min", type=float, default=1.8,
                    help="コメントAPIの呼び出しの1分あたりの平均目標")
    ap.add_argument("--max-calls-per-video", type=int, default=25,
                    help="1動画あたりのコメントAPI要求の上限。暴走の歯止め")
    ap.add_argument("--max-calls-per-min", type=float, default=2.0,
                    help="1分あたりの上限。ゆらぎを乗せてもこれを超えない")
    ap.add_argument("--api-gap", type=float, default=0.0,
                    help="次ページが届いた直後に空ける秒数（--calls-per-min とは別に上乗せ）")
    ap.add_argument("--no-remount", dest="remount", action="store_false",
                    help="コメントパネルを開き直さない（開き直すと次ページを要求するようになる）")
    ap.add_argument("--remount-pause", type=float, default=2.5,
                    help="パネルを閉じる/開くときの待ち")
    ap.add_argument("--scroll-log", type=int, default=0,
                    help="スクロールの経過を何段ごとに出すか。0で出さない")
    ap.add_argument("--jitter", type=float, default=0.5,
                    help="動画間の待機に乗せる揺らぎの割合（0.5なら最大1.5倍まで伸びる）")
    ap.add_argument("--collect-scrolls", type=int, default=10,
                    help="楽曲ページでのスクロール回数。必要本数が集まれば途中で止まる")
    ap.add_argument("--auto-wait", type=float, default=8.0,
                    help="アイコンを押さずにコメントが読まれるのを待つ秒数")
    ap.add_argument("--no-block", action="store_true")
    ap.add_argument("--challenge-grace", type=float, default=25.0)
    ap.add_argument("--probe", action="store_true", help="下見のみ。動画は1本も開かない")
    ap.add_argument("--allow-reload", dest="require_spa", action="store_false",
                    help="1本目がフルリロードでも中断せず続行する")
    ap.add_argument("--out", default="spatest.jsonl")
    ap.add_argument("--log", default="spatest.log")
    ap.add_argument("--summary", default="spatest_summary.json")
    # --- 2026-09-30 追加（分析の取得の流れ用）---
    ap.add_argument("--pool", default=None, help="候補プール pool.tsv（グリッドに見えている順に取る。必ず入れる動画を先に）")
    ap.add_argument("--candidates", default=None, help="差し替え候補の属性（records.jsonl）")
    ap.add_argument("--subs-out", default=None, help="差し替えと到達できなかった動画の記録（TSV）")
    ap.add_argument("--subs-cap", type=int, default=40, help="差し替えた動画のコメント上限")
    ap.add_argument("--reply-policy", choices=["threshold", "targets"], default="threshold",
                    help="threshold: --replies の閾値方式（今まで）/ targets: 返信数上位・質問形・投稿者本人だけ開く")
    ap.add_argument("--reply-top", type=int, default=2, help="targets: 返信数の多いコメントを何件開くか")
    ap.add_argument("--reply-questions", type=int, default=2, help="targets: 質問形の親（返信1件以上）を何件まで開くか")
    ap.add_argument("--reply-author", type=int, default=2, help="targets: 投稿者本人のコメントを何件まで開くか")
    ap.add_argument("--reply-more", type=int, default=0, help="targets: 「あとM件表示」を押す回数（0なら最初の3件だけ）")
    # 2026-10-05 速さ（冒頭の説明。既定はどれも切）
    ap.add_argument("--stop-on-no-more", action="store_true",
                    help="この動画の最新の応答が続き無し（has_more=0）なら、最低件数に届かなくても粘らない")
    ap.add_argument("--prescroll", action="store_true", help="次の要求を待つ間に、底の手前まで送っておく")
    ap.add_argument("--prescroll-margin", type=int, default=600,
                    help="--prescroll: 底からこれだけ手前で止める（待ちの間に次を呼んだら自動で広げる）")
    ap.add_argument("--only-open-video", action="store_true",
                    help="開いた動画以外の1ページ目（隣の動画の先読み）を送らない")
    ap.add_argument("--remount-on-stall", action="store_true",
                    help="開き直しは最初にせず、底で止まって続きがあるのに増えないときだけ")
    ap.add_argument("--deadline-hours", type=float, default=0.0,
                    help="取得に使う時間の上限（ロックを譲っていた時間は数えない）。0で無制限")
    return ap


def main():
    a = build_parser().parse_args()

    c = SpaCollector(a)
    try:
        c.probe() if a.probe else c.run()
    except Blocked as e:
        c.log(f"!! 開始前にブロック検知: {e}")
        print("\n前回のブロックから1.5時間空けてから再実行してください。")


if __name__ == "__main__":
    main()
