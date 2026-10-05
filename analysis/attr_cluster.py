"""動画の属性だけで似た動画をまとめる（AI を使わない。AI が界隈を決める前に、コメントを取る動画を選ぶため）。

2026-10-06 ユーザー採用（docs/COMMENT_TARGETS.md）: 必ず入れる動画に「属性のまとまり12個それぞれの再生上位3本」を足す。
作りは試算に使った analysis/comment_exp/select_study.py と同じ（同じデータなら同じまとまりになる）:
  - 属性の語: タグ・TikTok のカテゴリラベル・提案語・投稿地域・言語・フォロワー規模・認証・自己紹介文と表示名・説明文
    （日本語は2文字ずつ、英数字は3文字以上の語）
  - TF-IDF（2本以上の動画に出る語だけ）→ cos 距離の k-means（k-means++ の初期値を5通り試して、まとまりの良いものを使う）
  - AI の界隈との一致は中くらい（NMI 0.36〜0.55。シルエット・きゃわ）。界隈そのものではなく「属性の似た動画の山」
"""
import collections
import math
import re

import numpy as np

K = 12        # まとまりの数
TOP_N = 3     # まとまりごとに取る再生上位の本数
SEED_RUNS = 5

TOK = re.compile(r"[a-z0-9]+|[぀-ヿ一-鿿]{1,}", re.I)
JA = re.compile(r"[぀-ヿ一-鿿]")


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _words(prefix: str, text: str) -> list:
    out = []
    for w in TOK.findall(text):
        w = w.lower()
        if JA.match(w):   # 日本語は2文字ずつ
            out += [prefix + w[i:i + 2] for i in range(max(1, len(w) - 1))]
        elif len(w) > 2:
            out.append(prefix + w)
    return out


def tokens(rec: dict, enr: dict) -> list:
    """1本の動画の属性の語。rec は derived/llm_input/records.jsonl の行、enr は raw/enriched.jsonl の行"""
    a = enr.get("author") or {}
    t = ["#" + str(h).lower() for h in (rec.get("hashtags") or [])]
    for s in (rec.get("tiktok_labels") or []) + (enr.get("diversification_labels") or []):
        t.append("lab:" + str(s).lower())
    for s in rec.get("suggested_words") or []:
        t += ["sw:" + w.lower() for w in TOK.findall(str(s))]
    t.append("loc:" + str(rec.get("location_created") or "?"))
    t.append("lang:" + str(rec.get("text_language") or "?"))
    fol = _num(a.get("follower_count"))
    t.append("fol:" + ("1m" if fol >= 1e6 else "100k" if fol >= 1e5 else "10k" if fol >= 1e4 else "1k" if fol >= 1e3 else "0"))
    t.append("ver:" + str(bool(a.get("verified"))))
    t += _words("bio:", str(a.get("signature") or "") + " " + str(a.get("nickname") or ""))
    t += _words("d:", str(rec.get("desc") or ""))
    return t


def tfidf(docs: dict):
    """docs: {video_id: 語の列}（並びを保つ）→ (video_id の列, 正規化した行列)"""
    df = collections.Counter(w for t in docs.values() for w in set(t))
    vocab = [w for w, n in df.items() if n >= 2]
    ix = {w: i for i, w in enumerate(vocab)}
    n_docs = len(docs)
    X = np.zeros((n_docs, len(vocab)), dtype=np.float32)
    vs = list(docs)
    for i, v in enumerate(vs):
        for w, n in collections.Counter(docs[v]).items():
            if w in ix:
                X[i, ix[w]] = (1 + math.log(n)) * math.log(n_docs / df[w])
    X /= np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-9)
    return vs, X


def kmeans(X, k: int, seed: int = 0, iters: int = 50):
    rng = np.random.default_rng(seed)
    C = [X[rng.integers(len(X))]]
    for _ in range(1, k):   # k-means++（cos 距離）
        d = 1 - np.max(X @ np.array(C).T, axis=1)
        p = np.maximum(d, 0) ** 2
        C.append(X[rng.choice(len(X), p=p / p.sum())] if p.sum() > 0 else X[rng.integers(len(X))])
    C = np.array(C)
    for _ in range(iters):
        a = np.argmax(X @ C.T, axis=1)
        newC = np.array([X[a == j].mean(0) if (a == j).any() else C[j] for j in range(k)])
        newC /= np.maximum(np.linalg.norm(newC, axis=1, keepdims=True), 1e-9)
        if np.allclose(newC, C):
            break
        C = newC
    a = np.argmax(X @ C.T, axis=1)
    return a, float(np.sum(np.max(X @ C.T, axis=1)))


def clusters(records: dict, enriched: dict, ids: list, k: int = K, seed_runs: int = SEED_RUNS) -> dict:
    """ids（records.jsonl の並び）を k 個にまとめる → {video_id: まとまりの番号}。本数が k 以下なら1本ずつ"""
    if len(ids) <= k:
        return {v: i for i, v in enumerate(ids)}
    vs, X = tfidf({v: tokens(records[v], enriched[v]) for v in ids})
    a, _ = max((kmeans(X, k, seed=s) for s in range(seed_runs)), key=lambda t: t[1])
    return {v: int(a[i]) for i, v in enumerate(vs)}


def tops(records: dict, enriched: dict, ids: list, plays: dict, k: int = K, n: int = TOP_N) -> list:
    """まとまりごとの再生上位 n 本（重なりなし。まとまりの番号順・再生順）"""
    cl = clusters(records, enriched, ids, k)
    groups = collections.defaultdict(list)
    for v in ids:
        groups[cl[v]].append(v)
    out = []
    for g in sorted(groups):
        out += sorted(groups[g], key=lambda v: -plays.get(v, 0))[:n]
    return out
