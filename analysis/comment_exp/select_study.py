"""
コメントを取る動画の選び方の検討（2026-10-05 ユーザー「最終レポートで使っているコメント・着目している動画と、界隈分類の前にできる
クラスタリングとで、どれくらい強い相関を作れるか」「クラスタの中で再生数やいいね数で弱小動画を落とすのもあり」）。AI も TikTok も使わない。

  .venv/bin/python analysis/comment_exp/grading/../select_study.py

正解（当てたいもの。元の量の版3本＝本番・写し1・写し2のうち2本以上で一致したもの）:
  引用  レポートがコメントを引用した動画
  熟読  AI が comments:<seq> で全部読んだ動画
  言及  レポート本文で seq を名指しした動画（ラベルの付いた約290本まで広いので、今のプールの偏りが小さい）
候補: 楽曲ページの一覧のうち属性が取れた動画（pool.py の alive と同じ）
"""
import collections
import csv
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

CID = re.compile(r"cid[ 　:：]*(\d{15,20})")
SEQ = re.compile(r"seq[ 　]*(\d+)")
SONGS = [("シルエット", "a20260930-2342-0035", ["x-sil-b1", "x-sil-b2"]),
         ("きゃわ", "a20261004-0837-4e69", ["x-kyw-b1", "x-kyw-b2"])]


def jl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def load(aid, bases):
    d = common.APP_DATA / "analyses" / aid
    recs = {str(r["video_id"]): r for r in jl(d / "derived/llm_input/records.jsonl")}
    enr = {str(r["video_id"]): r for r in jl(d / "raw/enriched.jsonl")}
    seq2v = {int(r["seq"]): v for v, r in recs.items()}
    with open(d / "outputs/labels.tsv", encoding="utf-8") as f:
        labs = {seq2v[int(r["seq"])]: r["community"] for r in csv.DictReader(f, delimiter="\t") if int(r["seq"]) in seq2v}
    with open(d / "derived/pool.tsv", encoding="utf-8") as f:
        pool = {r["video_id"]: r for r in csv.DictReader(f, delimiter="\t")}
    raw = jl(d / "raw/comments.jsonl")
    cid2v = {}
    for r in raw:
        if r.get("status") != "ok":
            continue
        for c in r.get("comments") or []:
            cid2v[str(c["cid"])] = str(r["video_id"])
            for rp in c.get("reply_comment") or []:
                cid2v[str(rp["cid"])] = str(r["video_id"])
        for rp in r.get("reply_comments") or []:
            cid2v[str(rp["cid"])] = str(r["video_id"])
    fetched = {str(r["video_id"]) for r in raw if r.get("status") == "ok"}
    truth = {"引用": collections.Counter(), "熟読": collections.Counter(), "言及": collections.Counter()}
    for vd in [d] + [common.HOME / "analyses" / b for b in bases]:
        rep = (vd / "outputs/REPORT.md").read_text(encoding="utf-8")
        rep = rep[:rep.find("付録")] if "付録" in rep else rep
        truth["引用"].update({cid2v[c] for c in set(CID.findall(rep)) if c in cid2v})
        truth["言及"].update({seq2v[int(s)] for s in set(SEQ.findall(rep)) if int(s) in seq2v})
        read = set()
        for e in jl(vd / "state/task_log.jsonl"):
            n = str(e.get("name", ""))
            if e.get("event") == "read" and n.startswith("comments:") and n[9:].isdigit() and int(n[9:]) in seq2v:
                read.add(seq2v[int(n[9:])])
        truth["熟読"].update(read)
    stable = {k: {v for v, n in c.items() if n >= 2} for k, c in truth.items()}
    alive = [v for v in recs if (enr.get(v) or {}).get("author")]
    return dict(recs=recs, enr=enr, labs=labs, pool=pool, fetched=fetched, truth=stable, alive=alive)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def feats(D):
    """数値の属性（フィルタ・順位づけ用）"""
    out = {}
    for v in D["alive"]:
        r, e = D["recs"][v], D["enr"][v]
        st = e.get("stats") or {}
        a = e.get("author") or {}
        out[v] = {"plays": num(r.get("plays")), "likes": num(r.get("likes")), "comments": num(st.get("commentCount") or r.get("comments")),
                  "shares": num(r.get("shares")), "collects": num(st.get("collectCount")), "date": r.get("date") or "",
                  "followers": num(a.get("follower_count")), "verified": bool(a.get("verified"))}
    return out


TOK = re.compile(r"[a-z0-9]+|[぀-ヿ一-鿿]{1,}", re.I)


def tokens(D, v):
    r, e = D["recs"][v], D["enr"][v]
    a = e.get("author") or {}
    t = []
    t += ["#" + str(h).lower() for h in (r.get("hashtags") or [])]
    for s in (r.get("tiktok_labels") or []) + (e.get("diversification_labels") or []):
        t.append("lab:" + str(s).lower())
    for s in r.get("suggested_words") or []:
        t += ["sw:" + w.lower() for w in TOK.findall(str(s))]
    t.append("loc:" + str(r.get("location_created") or "?"))
    t.append("lang:" + str(r.get("text_language") or "?"))
    fol = num(a.get("follower_count"))
    t.append("fol:" + ("1m" if fol >= 1e6 else "100k" if fol >= 1e5 else "10k" if fol >= 1e4 else "1k" if fol >= 1e3 else "0"))
    t.append("ver:" + str(bool(a.get("verified"))))
    for w in TOK.findall(str(a.get("signature") or "") + " " + str(a.get("nickname") or "")):
        w = w.lower()
        if re.match(r"[぀-ヿ一-鿿]", w):   # 日本語は2文字ずつ
            t += ["bio:" + w[i:i + 2] for i in range(max(1, len(w) - 1))]
        elif len(w) > 2:
            t.append("bio:" + w)
    for w in TOK.findall(str(r.get("desc") or "")):
        w = w.lower()
        if re.match(r"[぀-ヿ一-鿿]", w):
            t += ["d:" + w[i:i + 2] for i in range(max(1, len(w) - 1))]
        elif len(w) > 2:
            t.append("d:" + w)
    return t


def tfidf(D):
    docs = {v: tokens(D, v) for v in D["alive"]}
    df = collections.Counter(w for t in docs.values() for w in set(t))
    vocab = [w for w, n in df.items() if n >= 2]
    ix = {w: i for i, w in enumerate(vocab)}
    N = len(docs)
    X = np.zeros((N, len(vocab)), dtype=np.float32)
    vs = list(docs)
    for i, v in enumerate(vs):
        for w, n in collections.Counter(docs[v]).items():
            if w in ix:
                X[i, ix[w]] = (1 + math.log(n)) * math.log(N / df[w])
    X /= np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-9)
    return vs, X


def kmeans(X, k, seed=0, iters=50):
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
    score = float(np.sum(np.max(X @ C.T, axis=1)))
    return a, score


def nmi(x, y):
    n = len(x)
    cx, cy, cxy = collections.Counter(x), collections.Counter(y), collections.Counter(zip(x, y))
    mi = sum(c / n * math.log((c / n) / ((cx[a] / n) * (cy[b] / n))) for (a, b), c in cxy.items())
    hx = -sum(c / n * math.log(c / n) for c in cx.values())
    hy = -sum(c / n * math.log(c / n) for c in cy.values())
    return 2 * mi / (hx + hy) if hx + hy else 0.0


def purity_rev(clu, lab):
    """界隈ごとに、一番多いクラスタに入る割合（界隈が何個のクラスタに散るか）"""
    by = collections.defaultdict(collections.Counter)
    for c, l in zip(clu, lab):
        by[l][c] += 1
    return sum(max(cn.values()) for cn in by.values()) / len(lab)


def recall(sel, T):
    return len(sel & T) / len(T) if T else float("nan")


def main():
    for name, aid, bases in SONGS:
        D = load(aid, bases)
        F = feats(D)
        T = D["truth"]
        must = {v for v, p in D["pool"].items() if p["priority"] == "1"}
        poolset = set(D["pool"])
        print(f"\n######## {name}: 候補 {len(D['alive'])}本・今のプール {len(poolset)}本（必ず {len(must)}）・"
              f"正解 引用 {len(T['引用'])}本・熟読 {len(T['熟読'])}本・言及 {len(T['言及'])}本")
        # 1. 弱小動画は正解に入っているか
        print("\n[1] 正解の動画の再生数（候補全体と比べる）")
        for lab, S in [("候補全体", set(D["alive"])), ("引用", T["引用"]), ("熟読", T["熟読"]), ("言及", T["言及"])]:
            ps = sorted(F[v]["plays"] for v in S if v in F)
            if not ps:
                continue
            q = lambda p: ps[min(len(ps) - 1, int(p * len(ps)))]
            print(f"  {lab:<6} {len(ps):>4}本  再生の中央値 {q(.5):>10,.0f}  下位10% {q(.1):>9,.0f}  "
                  f"1万未満 {sum(1 for p in ps if p < 1e4)/len(ps):>4.0%}  10万未満 {sum(1 for p in ps if p < 1e5)/len(ps):>4.0%}")
        # 2. クラスタリングと界隈の一致
        vs, X = tfidf(D)
        lab_v = [v for v in vs if v in D["labs"] and D["labs"][v] != "unknown"]
        print(f"\n[2] 属性のクラスタリングと AI の界隈（ラベル {len(lab_v)}本・界隈 {len(set(D['labs'][v] for v in lab_v))}個）の一致")
        best = {}
        for k in (8, 12, 16, 24):
            a, sc = max((kmeans(X, k, seed=s) for s in range(5)), key=lambda t: t[1])
            idx = {v: i for i, v in enumerate(vs)}
            clu = [int(a[idx[v]]) for v in lab_v]
            labs = [D["labs"][v] for v in lab_v]
            print(f"  k={k:<3} NMI {nmi(clu, labs):.2f}  界隈がまとまる割合 {purity_rev(clu, labs):.0%}")
            best[k] = {v: int(a[i]) for i, v in enumerate(vs)}
        # 3. 選び方ごとの当たり
        print("\n[3] 選び方ごとに、正解の何割を拾えるか（本数 K）")
        print(f"  {'選び方':<44}{'K':>5}  引用  熟読  言及")

        def show(label, sel):
            print(f"  {label:<44}{len(sel):>5}  {recall(sel, T['引用']):>4.0%}  {recall(sel, T['熟読']):>4.0%}  {recall(sel, T['言及']):>4.0%}")

        show("今のプール", poolset)
        show("必ず取る動画だけ", must)
        by = lambda key: sorted(F, key=lambda v: -F[v][key])
        for K in (100, 130, len(poolset)):
            show(f"必ず＋コメント数の多い順で K 本まで", must | set([v for v in by("comments") if v not in must][:max(0, K - len(must))]))
        for K in (100, 130, len(poolset)):
            show(f"必ず＋再生の多い順で K 本まで", must | set([v for v in by("plays") if v not in must][:max(0, K - len(must))]))
        for k in (12, 16):
            cl = best[k]
            groups = collections.defaultdict(list)
            for v in F:
                groups[cl[v]].append(v)
            for n in (2, 3, 5):
                sel = set(must)
                for g in groups.values():
                    sel |= set(sorted(g, key=lambda v: -F[v]["plays"])[:n])
                    sel |= set(sorted(g, key=lambda v: -F[v]["comments"])[:n])
                show(f"必ず＋クラスタ{k}個×（再生上位{n}・コメント上位{n}）", sel)
            for pct in (0.5, 0.7, 0.8):   # ユーザー案: クラスタの中で再生の弱い動画を落とす
                sel = set()
                for g in groups.values():
                    ps = sorted(F[v]["plays"] for v in g)
                    th = ps[min(len(ps) - 1, int(pct * len(ps)))]
                    sel |= {v for v in g if F[v]["plays"] >= th}
                show(f"クラスタ{k}個の中で再生上位{100-int(pct*100)}%（必ず無し）", sel)
                show(f"  同上＋必ず取る動画", sel | must)
        for th in (1e4, 5e4, 1e5):
            sel = {v for v in F if F[v]["plays"] >= th}
            show(f"再生 {th:,.0f} 以上（全部）", sel)


if __name__ == "__main__" and len(sys.argv) == 1:
    main()


def keep_cluster_top(name, aid, bases, k=12, n=3, seed_runs=5):
    """実験用: 必ず取る動画＋クラスタ（k 個）ごとに再生上位 n 本。今のプールの中からしか選べない（取っていない動画にはコメントが無い）"""
    D = load(aid, bases)
    F = feats(D)
    vs, X = tfidf(D)
    a, _ = max((kmeans(X, k, seed=s) for s in range(seed_runs)), key=lambda t: t[1])
    cl = {v: int(a[i]) for i, v in enumerate(vs)}
    must = {v for v, p in D["pool"].items() if p["priority"] == "1"}
    inpool = [v for v in D["pool"] if v in F and v not in must]
    groups = collections.defaultdict(list)
    for v in inpool:
        groups[cl[v]].append(v)
    keep = set(must)
    for g in groups.values():
        keep |= set(sorted(g, key=lambda v: -F[v]["plays"])[:n])
    # 本物のルール（候補全体から選ぶ）で選ばれる動画のうち、今のプールにある割合（実験の偏りの目安）
    allg = collections.defaultdict(list)
    for v in F:
        if v not in must:
            allg[cl[v]].append(v)
    real = set()
    for g in allg.values():
        real |= set(sorted(g, key=lambda v: -F[v]["plays"])[:n])
    out = common.EXP / f"keep_{aid}_cluster{k}top{n}.txt"
    out.write_text("\n".join(sorted(keep)) + "\n", encoding="utf-8")
    T = D["truth"]
    print(f"{name}: 残す {len(keep)}本（必ず {len(must)}＋週ごとから {len(keep)-len(must)}）／今のプール {len(D['pool'])}本。"
          f"本物のルールで選ぶ週ごとの {len(real)}本のうち、今のプールにある {len(real & set(D['pool']))}本。"
          f" 正解を拾う割合 引用 {recall(keep, T['引用']):.0%}・熟読 {recall(keep, T['熟読']):.0%}・言及 {recall(keep, T['言及']):.0%} → {out}")
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "keep":
    for s in SONGS:
        keep_cluster_top(*s)
