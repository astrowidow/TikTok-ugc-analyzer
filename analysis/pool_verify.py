"""候補プールの規則が E2E の代表をどれだけ覆うかを測る（2026-09-30 の検証をそのまま残したもの）。

docs/BLUEPRINT.md 第3章の表はこのスクリプトの出力。予算 240 → プール 223本 → 代表65本中53、代表セル38中34。
本番の規則（analysis/pool.py など）を作ったら、同じ数字が出るかをこれで確かめる。

シルエット固有の部分（汎用化が要る）:
  - 本人・レーベルの判定が username の 'kana' / 'sony'
  - 公式企画のタグが 'みんなでシルエット'
  - phase() の日付（E2E の段階の区切り）は検証用。規則そのものは段階を使わない

使い方: リポジトリ直下で python3 analysis/pool_verify.py
"""
import collections
import csv
import json
import math
import random

BASE = 'output/trial_silhouette'

V = [json.loads(l) for l in open(f'{BASE}/videos.jsonl')]
E = {}
for l in open(f'{BASE}/enriched.jsonl'):
    e = json.loads(l)
    E[e['video_id']] = e
L = {r['seq']: r for r in csv.DictReader(open(f'{BASE}/clean/labels.tsv'), delimiter='\t')}
R = list(csv.DictReader(open(f'{BASE}/reps.tsv'), delimiter='\t'))
byseq = {str(v['seq']): v for v in V}


def phase(d):
    return 'P1' if d <= '2025-08-20' else 'P2' if d <= '2025-10-19' else 'P3' if d <= '2025-11-23' else 'P4'


# 属性が取れた（＝削除・写真でない）動画だけが対象
alive = [v for v in V if (E.get(v['video_id']) or {}).get('author')]


def build(budget, seed=7):
    pool = set()

    def add(vs):
        for v in vs:
            pool.add(str(v['seq']))

    # 必ず入れる: 最初期20・再生上位20・認証の最初期10と上位15・本人とレーベル・公式企画のタグ
    add(sorted(alive, key=lambda v: (v['date'], v['seq']))[:20])
    add(sorted(alive, key=lambda v: -v['plays'])[:20])
    ver = [v for v in alive if E[v['video_id']]['author'].get('verified')]
    add(sorted(ver, key=lambda v: v['date'])[:10])
    add(sorted(ver, key=lambda v: -v['plays'])[:15])
    add([v for v in alive if 'kana' in v['username'].lower() or 'sony' in v['username'].lower()])
    add([v for v in alive if 'みんなでシルエット' in (v['desc'] or '')])

    # 残りを週ごとに、週の本数の平方根に比例して配る
    weeks = collections.defaultdict(list)
    for v in alive:
        weeks[v['week']].append(v)
    rem = max(0, budget - len(pool))
    wsum = sum(math.sqrt(len(x)) for x in weeks.values())
    random.seed(seed)
    alloc = {w: max(1, round(rem * math.sqrt(len(x)) / wsum)) for w, x in weeks.items()}
    for w, vs in sorted(weeks.items()):
        cand = [v for v in vs if str(v['seq']) not in pool]
        k = alloc[w]
        chosen = []
        # 週の中: 投稿地域ごと・TikTok のカテゴリラベルごとに最大再生を1本ずつ
        for keyf in (lambda v: E[v['video_id']].get('location_created') or '?',
                     lambda v: (E[v['video_id']].get('diversification_labels') or ['?'])[0]):
            groups = collections.defaultdict(list)
            for v in cand:
                groups[keyf(v)].append(v)
            for g, gv in groups.items():
                best = max(gv, key=lambda v: v['plays'])
                if best not in chosen and len(chosen) < k:
                    chosen.append(best)
        # 残りは上位半分＋無作為半分
        rest = [v for v in cand if v not in chosen]
        top = sorted(rest, key=lambda v: -v['plays'])[:max(0, math.ceil((k - len(chosen)) / 2))]
        chosen += top
        rest = [v for v in rest if v not in top]
        chosen += random.sample(rest, min(len(rest), max(0, k - len(chosen))))
        add(chosen)
    return pool


if __name__ == '__main__':
    cells = collections.Counter((r['community'], r['phase']) for r in R)
    print('予算  プール  取得時間  代表65本中  代表セル38中')
    for B in (100, 140, 180, 240):
        pool = build(B)
        reps_in = sum(1 for r in R if r['seq'] in pool)
        cov = sum(1 for (c, p) in cells
                  if any(s in pool and L[s]['community'] == c and phase(byseq[s]['date']) == p for s in L))
        print(f'{B:4d}  {len(pool):4d}   {len(pool) * 3 / 60:4.1f}h     {reps_in:3d}        {cov:3d}')
