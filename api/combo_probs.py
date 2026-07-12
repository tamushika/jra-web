"""
組み合わせ馬券の確率エンジン (Harville式 + 減衰λ)
====================================================
conditional logit のレース内勝率からワイド (2頭がともに3着内) の
的中確率を導出する。backtest_combo.py で検証済み:

  - 減衰 λ=0.7 (2021-24年の実現2着に対する最尤推定)
  - overlay = モデル確率 / 市場確率 (単勝オッズ由来のHarville近似)
  - ワイド overlay>=1.6 の全組み合わせ購入で
    2025年 回収110.1% (6,678点) / 2026年OOS 110.0% (1,075点)
  - 馬連は2026年OOSで再現せず不採用 / 1レース1点方式も劣後 (102.7%/90.6%)
"""
from itertools import combinations, permutations
from collections import defaultdict

LAMBDA = 0.7  # backtest_combo.py の最尤推定値。再推定したらここを更新


def wide_probs(probs, lam=LAMBDA):
    """勝率リスト → {(i,j): 2頭がともに3着内に入る確率} (i<j)。
    1着は素の確率 p、2着以降の条件付きは減衰確率 s∝p^λ (Harville補正)。"""
    z = sum(probs)
    if z <= 0 or len(probs) < 3:
        return {}
    p = [x / z for x in probs]
    sz = sum(x ** lam for x in p)
    s = [x ** lam / sz for x in p]
    n = len(p)

    wide = defaultdict(float)
    for a, b, c in combinations(range(n), 3):
        pset = 0.0
        for x, y, w in permutations((a, b, c)):
            d1 = 1 - s[x]
            d2 = d1 - s[y]
            if d1 > 0 and d2 > 0:
                pset += p[x] * s[y] / d1 * s[w] / d2
        wide[(a, b)] += pset
        wide[(a, c)] += pset
        wide[(b, c)] += pset
    return dict(wide)


def wide_candidates(win_probs, odds, overlay_min=1.6, min_prob=0.02, top_n=5):
    """モデル勝率と単勝オッズから、overlay >= overlay_min のワイド候補を返す。
    戻り値: [(i, j, overlay, model_prob)] overlay降順・最大top_n件。
    win_probs/odds の要素が None の馬は除外して計算する。"""
    idx = [k for k, (p, o) in enumerate(zip(win_probs, odds))
           if p is not None and o is not None and o > 1.0]
    if len(idx) < 3:
        return []
    probs = [win_probs[k] for k in idx]
    inv = [1.0 / odds[k] for k in idx]
    z = sum(inv)
    mkt = [v / z for v in inv]

    w_model = wide_probs(probs)
    w_mkt = wide_probs(mkt)
    out = []
    for (a, b), mp in w_model.items():
        if mp < min_prob:
            continue
        mk = w_mkt.get((a, b), 0.0)
        if mk <= 0:
            continue
        overlay = mp / mk
        if overlay >= overlay_min:
            out.append((idx[a], idx[b], round(overlay, 2), round(mp, 4)))
    out.sort(key=lambda x: -x[2])
    return out[:top_n]
