"""SPEC-T79b: 重賞データページで、今回の出走馬を過去傾向の区分に当てはめて
「合致度」を出すための純関数群 (副作用なし。DB・ネットワークには一切アクセスしない)。

表示専用の参考指標であり、予測モデル・EV・仮想購入判断には一切使わない。

依存する既存モジュール:
  - api.graded_names (T79): レース名の正規化・重賞キー突合 (prev_race用)
  - api.past_data_service._kyaku_from_c4 (T75c): 4角位置→脚質ラベル
  - api.index.calculate_kyakushitsu (呼び出し側 analyze_race_url が既に
    horse['kyakushitsu'] として矢印文字列を計算済みなので、ここでは
    その文字列を解釈するだけで再計算はしない)

区分ごとのバケット境界 (人気/馬体重/間隔/前走着順) は jra_graded.py の
_POP_BUCKETS 等と同じ値を意図的に重複定義している (jra_graded → 本モジュール
の一方向依存を保ち、循環importを避けるため)。値を変える場合は両方を直す。
"""
import math
import re
import unicodedata
from collections import Counter

from api.graded_names import match_key, normalize_race_name
from api.past_data_service import _kyaku_from_c4

# ─── バケット定義 (T79 jra_graded.py と同じ境界) ───────────────────────────

_POP_BUCKETS = (
    ("1番人気", lambda p: p == 1),
    ("2番人気", lambda p: p == 2),
    ("3番人気", lambda p: p == 3),
    ("4-6番人気", lambda p: 4 <= p <= 6),
    ("7-9番人気", lambda p: 7 <= p <= 9),
    ("10番人気以下", lambda p: p >= 10),
)

_WEIGHT_BUCKETS = (
    ("〜439", lambda w: w <= 439),
    ("440-479", lambda w: 440 <= w <= 479),
    ("480-519", lambda w: 480 <= w <= 519),
    ("520〜", lambda w: w >= 520),
)

_INTERVAL_BUCKETS = (
    ("中1週以下", lambda d: d <= 14),
    ("中2-3週", lambda d: 15 <= d <= 28),
    ("中4-8週", lambda d: 29 <= d <= 63),
    ("中9週以上", lambda d: d >= 64),
)

_PREV_RANK_BUCKETS = (
    ("1着", lambda r: r == 1),
    ("2-3着", lambda r: 2 <= r <= 3),
    ("4-9着", lambda r: 4 <= r <= 9),
    ("10着以下", lambda r: r >= 10),
)

# kyakushitsu 矢印 (api.index.calculate_kyakushitsu の戻り値) → 脚質ラベル。
# hist[:3] のコーナー通過順から判定できなかった場合のフォールバックに使う。
_KYAKU_ARROW_MAP = {
    "◀◁◁◁": "逃げ",
    "◀◀◁◁": "先行",
    "◁◀◁◁": "先行",
    "◁◀◀◁": "差し",
    "◁◁◀◁": "差し",
    "◁◁◀◀": "追込",
    "◁◁◁◀": "追込",
    "ー": None,
}

# 実データの horse['sex_age'] は "牡5/芦" のように毛色が付くことがあるため、
# 先頭の性別+年齢だけを見る (末尾アンカーなし)。
_SEX_AGE_RE = re.compile(r"^(牡|牝|セ)\s*(\d+)")


def _bucket_label(value, buckets):
    if value is None:
        return None
    for label, pred in buckets:
        try:
            if pred(value):
                return label
        except TypeError:
            continue
    return None


def _to_int(v):
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _clean_str(v):
    """空/'-' を None 扱いにする以外は素通し (前後空白のみ除去)。"""
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s != "-" else None


def _sex_age_label(sex_age):
    """'牡3'/'牝5'/'セ4' 等の horse['sex_age'] を T79 の sex_age ラベル
    (jra_graded._sex_age_label と同じ規則: セは年齢無視、6歳以上は '6+') に変換する。
    """
    s = _clean_str(sex_age)
    if not s:
        return None
    s = unicodedata.normalize("NFKC", s)
    m = _SEX_AGE_RE.match(s)
    if not m:
        return None
    sex, age_s = m.group(1), m.group(2)
    if sex == "セ":
        return "セ"
    age = _to_int(age_s)
    if age is None:
        return None
    if age >= 6:
        return f"{sex}6+"
    return f"{sex}{age}"


def _last_corner_pos(corners):
    """'3-3-2-1' のようなコーナー通過順文字列の最後の数字 (4角相当) を返す。"""
    s = _clean_str(corners)
    if not s:
        return None
    last = s.split("-")[-1].strip()
    return _to_int(last)


def _kyaku_label(horse):
    """SPEC-T79b §2.1: hist[:3] の各走のコーナー通過順から脚質を判定し、
    最頻値 (同数なら直近優先) を返す。判定できる走が1つも無ければ、
    horse['kyakushitsu'] の矢印表記にフォールバックする。"""
    hist = horse.get("hist") or []
    votes = []  # (recency_idx, label) recency_idx=0が最新
    for i, h in enumerate(hist[:3]):
        pos = _last_corner_pos((h or {}).get("corners"))
        if pos is None:
            continue
        label = _kyaku_from_c4(pos)
        if label and label != "?":
            votes.append((i, label))
    if votes:
        counts = Counter(label for _, label in votes)
        max_count = max(counts.values())
        top_labels = {label for label, c in counts.items() if c == max_count}
        for i, label in votes:  # 出現順=直近優先で最初に見つかった最多ラベルを採用
            if label in top_labels:
                return label
    return _KYAKU_ARROW_MAP.get(horse.get("kyakushitsu"))


def _resolve_prev_race_label(hist0, known_keys=None, sponsors=None, aliases=None):
    """SPEC-T79b §2.1 prev_race: hist[0].race_name を正規化し、既知の重賞キーに
    解決できればそのキー (T79キャッシュ側 prev_race の集計ラベルと同じ形式)、
    できなければ正規化名 (base) を返す。"""
    if not hist0:
        return None
    raw_name = _clean_str(hist0.get("race_name"))
    if not raw_name:
        return None
    base, _grade = normalize_race_name(raw_name)
    if not base:
        return None
    if known_keys:
        place = _clean_str(hist0.get("place"))
        track_type = _clean_str(hist0.get("track_type"))
        distance = hist0.get("distance")
        key, _grade2, _how = match_key(
            raw_name, known_keys, place=place, track_type=track_type,
            distance=distance, sponsors=sponsors, aliases=aliases)
        if key:
            return key
    return base


def derive_entry_attrs(horse, ev_horse=None, race_date=None, *,
                       known_keys=None, sponsors=None, aliases=None):
    """SPEC-T79b §2.1: 出走馬1頭分の属性を、T79の過去傾向集計と同じラベル体系に
    変換する。取消馬かどうかの判定・除外は呼び出し側の責務 (ここでは行わない)。

    `race_date` は将来の拡張用に受け取るのみで、現バージョンでは未使用。

    戻り値は factor -> label (解決できなければ None) の dict:
      popularity, waku, kyaku, sex_age, affi, prev_race, prev_rank,
      sire, jockey, weight, interval
    """
    # popularity: EV監視の実際の人気 (あれば) → 無ければ analyze_race_url が
    # オッズ昇順で既に振っている horse['pop']。
    pop_val = None
    if ev_horse is not None:
        pop_val = _to_int(ev_horse.get("pop"))
    if pop_val is None:
        pop_val = _to_int(horse.get("pop"))
    popularity = _bucket_label(pop_val, _POP_BUCKETS)

    waku_val = _to_int(horse.get("w_num"))
    waku = str(waku_val) if waku_val is not None and 1 <= waku_val <= 8 else None

    kyaku = _kyaku_label(horse)

    sex_age = _sex_age_label(horse.get("sex_age"))

    affi = horse.get("affi") if horse.get("affi") in ("美浦", "栗東") else None

    hist = horse.get("hist") or []
    hist0 = hist[0] if hist else None
    prev_race = _resolve_prev_race_label(hist0, known_keys=known_keys,
                                         sponsors=sponsors, aliases=aliases)
    prev_rank_val = _to_int((hist0 or {}).get("rank")) if hist0 else None
    prev_rank = _bucket_label(prev_rank_val, _PREV_RANK_BUCKETS)

    sire = _clean_str(horse.get("sire"))
    jockey = _clean_str(horse.get("jock"))

    weight_val = _to_float(horse.get("current_weight"))
    weight = _bucket_label(weight_val, _WEIGHT_BUCKETS)

    interval_val = horse.get("interval_days")
    interval_val = interval_val if isinstance(interval_val, (int, float)) else _to_int(interval_val)
    interval = _bucket_label(interval_val, _INTERVAL_BUCKETS)

    return {
        "popularity": popularity, "waku": waku, "kyaku": kyaku, "sex_age": sex_age,
        "affi": affi, "prev_race": prev_race, "prev_rank": prev_rank,
        "sire": sire, "jockey": jockey, "weight": weight, "interval": interval,
    }


# ─── §2.2: 合致スコア ───────────────────────────────────────────────────────

_SHRINK_K = 10
_LIFT_HIT_THRESHOLD = math.log(1.25)
_LIFT_MISS_THRESHOLD = math.log(0.7)
_MIN_N_FOR_DISPLAY = 8

# score_entry が見る11区分 (SPEC §2.2)。aggregates の辞書キー名と factor 名が
# 異なるものだけ _FACTOR_TO_AGG_KEY に書く (prev_rank -> prev_rank_band)。
_SCORE_FACTORS = (
    "popularity", "waku", "kyaku", "sex_age", "affi", "prev_race",
    "prev_rank", "sire", "jockey", "weight", "interval",
)
_FACTOR_TO_AGG_KEY = {"prev_rank": "prev_rank_band"}


def _normalize_jockey_label(name):
    """騎手名の表記ゆれ (全角/半角・スペース) を吸収した比較用キー。"""
    s = _clean_str(name)
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s)


def _find_agg_row(rows, label, factor):
    if label is None:
        return None
    if factor == "jockey":
        target = _normalize_jockey_label(label)
        for row in rows:
            if _normalize_jockey_label(row.get("label")) == target:
                return row
        return None
    for row in rows:
        if row.get("label") == label:
            return row
    return None


def score_entry(attrs, aggregates, overall_fuku_rate):
    """SPEC-T79b §2.2: attrs (derive_entry_attrsの戻り値) と、T79履歴の
    aggregates / overall_fuku_rate から合致スコアを計算する。

    戻り値: {score, score_ex_pop, hits, misses, detail}
      - score: 人気を含む11区分の lift 合計
      - score_ex_pop: popularity を除いた10区分の lift 合計 (画面の既定表示)
      - hits/misses: 表示用の目立つ区分 (n>=8 かつ lift が閾値超/未満)
      - detail: 全区分の {factor, label, n, fuku_rate, lift}
    """
    score = 0.0
    score_ex_pop = 0.0
    hits, misses, detail = [], [], []

    for factor in _SCORE_FACTORS:
        label = attrs.get(factor)
        if label is None:
            continue
        agg_key = _FACTOR_TO_AGG_KEY.get(factor, factor)
        rows = aggregates.get(agg_key) or []
        row = _find_agg_row(rows, label, factor)
        if row is None:
            continue  # 該当行なし (sire/jockey/prev_raceの「データ少」含む) -> 0扱い
        n = row.get("n") or 0
        fuku_rate = row.get("fuku_rate")
        if not n or fuku_rate is None or not overall_fuku_rate:
            continue
        shrunk = (fuku_rate * n + overall_fuku_rate * _SHRINK_K) / (n + _SHRINK_K)
        lift = math.log(shrunk / overall_fuku_rate)

        score += lift
        if factor != "popularity":
            score_ex_pop += lift
        detail.append({"factor": factor, "label": label, "n": n,
                       "fuku_rate": fuku_rate, "lift": round(lift, 4)})

        if n >= _MIN_N_FOR_DISPLAY:
            entry = {
                "factor": factor, "label": label, "n": n, "fuku_rate": fuku_rate,
                "c123": f"{row.get('c1', 0)}-{row.get('c2', 0)}-{row.get('c3', 0)}-{row.get('out', 0)}",
            }
            if lift >= _LIFT_HIT_THRESHOLD:
                hits.append(entry)
            elif lift <= _LIFT_MISS_THRESHOLD:
                misses.append(entry)

    return {
        "score": round(score, 4), "score_ex_pop": round(score_ex_pop, 4),
        "hits": hits, "misses": misses, "detail": detail,
    }
