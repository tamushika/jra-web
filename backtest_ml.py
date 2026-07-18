"""
重み付けの機械学習化の検証
============================
現行スコアの各成分 + 追加素性を特徴量に、勝ち馬予測のロジスティック回帰を学習し、
手調整の現行スコアとアウトオブサンプル (時系列分割) で勝ち馬カバレッジを比較する。

  学習: 2021-2024 / 検証: 2025 (学習に一切使わない)

特徴量はすべてライブで計算可能なもの (バックテスト再現可能な範囲) に限定:
  騎手/枠バイアス点・タイム指数(馬場差込み)・クラス実績・上がり質・市場(ln odds)・
  前走着順・出走間隔・年齢・性別・斤量・前走馬体重・キャリア数(直近窓)・道悪適性

【使い方】
  python backtest_ml.py            # 時系列分割で検証 (学習2021-24 / 検証2025)
  python backtest_ml.py --write    # 検証後、全期間(2021-2025)で再学習して
                                   # api/data_files/common/win5_ml_model.json に出力
"""
import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
from backtest_ability import load_runs, RECENCY, TIME_DIFF_CLIP, CLASS_RANK  # noqa: E402
from backtest_win5 import (load_win5_cfg, load_upset_map, score_all_runners,
                           measure_coverage, parse_final_odds,
                           popularity_races)  # noqa: E402
from backtest_score import JockeyMatcher  # noqa: E402
from past_data_service import calculate_waku  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")

TRAIN_FROM, TRAIN_TO = "20210101", "20241231"
TEST_FROM, TEST_TO = "20250101", "20251231"
ADDITIONAL_TEST_FROM, ADDITIONAL_TEST_TO = "20260101", "20260630"

FEATURES = ["j_pts", "f_pts", "tfeat", "cfeat", "agari_flag", "ln_odds",
            "prev_rank", "ln_interval", "age", "is_male", "kinryo", "weight",
            "n_prior", "wet_match",
            # 2026-07 追加パック
            "dist_pts",    # 距離変更バイアス点 (db-keiba)
            "surf_pts",    # コース替わりバイアス点 (db-keiba)
            "affi_pts",    # 所属バイアス点 (db-keiba)
            "pace_fit",    # 型PCI偏差 × メンバー予測ペース (backtest_pace C検証定義)
            "course_fit",  # 直近4走に同場同芝ダ同距離での3着以内あり
            "grade_pts",   # 好走条件判定 ◎3/〇2/△1 (血統以外ルール)
            # 血統 (horse_pedigree バックフィル完了後に有効化。カバレッジ不足時は全0
            # = 定数列 → StandardScaler が scale=1 で無害化し、係数もほぼ0になる)
            "sire_pts"]    # 種牡馬×コースバイアス点 (db-keiba father_w)

# M2は市場情報だけを外した診断モデル。本番モデルのFEATURESは変更しない。
NO_MARKET_FEATURES = [name for name in FEATURES if name != "ln_odds"]

# T13: レース内の相対比較を conditional logit に明示的に渡す診断特徴量。
# 本番モデルの FEATURES は変更せず、--relative 相当の呼び出しでだけ末尾に加える。
RELATIVE_FEATURES = [
    "tfeat_rank",
    "tfeat_gap_med",
    "tfeat_gap_best",
    "j_pts_dev",
    "f_pts_dev",
    "sire_pts_dev",
    "kinryo_dev",
    "n_prior_dev",
]

_RELATIVE_AVAILABILITY_BITS = {
    name: 1 << index
    for index, name in enumerate(
        ("tfeat", "j_pts", "f_pts", "sire_pts", "kinryo", "n_prior")
    )
}


def active_feature_names(relative=False, pack_groups=None):
    """今回のデータセットに含める特徴量名を、列順どおりに返す。"""
    return (list(FEATURES) + (list(RELATIVE_FEATURES) if relative else [])
            + pack_feature_names(pack_groups))


# T41: 低コスト特徴パック (A3馬体重変動/A7ローテ/A8斤量差分/X3タイム指数状態量)。
# T13 の RELATIVE_FEATURES と同じ隔離パターン。本番 FEATURES は不変。
# pack_groups が空/None のときは build_dataset の出力に一切影響しない
# (SPEC-T41 §2/§5.4: パックOFFで既存出力が決定論SHA一致すること)。
A3_FEATURES = [
    "weight_delta_prev",                  # 当日体重-前走体重 (前走なしは0)
    "weight_delta_rate",                  # 上記/前走体重
    "weight_no_prior_flag",               # 前走なし (初出走) flag
    "weight_today_missing_flag",          # 当日体重欠損 (前走値fallback使用) flag
    "weight_dev_expanding",               # 過去走のみのexpanding中央値からのMAD正規化偏差
    "weight_dev_expanding_missing_flag",  # 過去2走未満 flag
    "weight_delta_x_layoff",              # weight_delta_prev × ln(休養日数)
]
A7_FEATURES = [
    "starts_28d",                    # 過去28日の出走数
    "starts_56d",                    # 過去56日の出走数
    "run_after_layoff",              # 休養(90日以上)明け何戦目か (上限5クリップ、休養未経験は0)
    "class_delta",                   # 前走とのクラス差 (昇級+/降級-、初出走0)
    "dist_delta_log",                # ln(今回距離/前走距離) (初出走0)
    "surface_change",                # 芝⇔ダ替わり flag (初出走0)
    "venue_change_transport_proxy",  # 前走と別競馬場 flag (滞在競馬は区別できないためproxy呼称)
    "jockey_change",                 # 前走と騎手が異なる flag
]
A8_FEATURES = [
    "kinryo_delta_prev",         # 今回斤量-前走斤量 (前走なしは0)
    "kinryo_per_weight",         # 斤量/当日馬体重 (体重欠損時は前走体重fallback)
    "kinryo_delta_x_handicap",   # kinryo_delta_prev × ハンデ戦flag
]
X3_FEATURES = [
    "tfeat_rwmean",              # recency加重平均 (半減期2走、直近8走)
    "tfeat_std",                 # 同ウィンドウの標準偏差
    "tfeat_std_missing_flag",    # 有効走数2走未満 flag
    "tfeat_slope2",              # 直近2走(有効値)の傾き
    "tfeat_gap_from_max",        # tfeat_rwmean - 現行tfeat(直近4走max)
    "tfeat_post_layoff_delta",   # 前走が休養明け初戦だった場合のその走の指数変化
]
PACK_GROUPS = {"A3": A3_FEATURES, "A7": A7_FEATURES, "A8": A8_FEATURES, "X3": X3_FEATURES}
PACK_GROUP_ORDER = ("A3", "A7", "A8", "X3")

# race_name (TARGET式圧縮名) のハンデ戦マーカー: 全角Ｈが「名前の直後・
# 修飾の直前」に付く (例: "小倉大賞ＨG3", "仁川ＳＨ(L)", "早春ＳＨ･3勝",
# "いわきＨ1000", 末尾 "しらかばＨ")。局名等の名前の一部のＨ
# ("ＮＨＫマG1", "ＨＢＣ賞1000", "ＵＨＢ杯") はハンデではない。
# ability.db全件でＨ直後の文字はマーカー位置 (数字/･/G+数字/括弧/末尾) と
# 名前連続 (Ｂ/Ｔ/Ｋ) に完全二分されることを確認済み (T41レビュー、誤検知1,990走を除外)。
HANDICAP_RACE_NAME_MARKER_RE = re.compile(r"Ｈ(?=$|[GＧ][1-3１-３]|[0-9０-９]|･|\(|（)")
X3_WINDOW = 8
X3_HALFLIFE_STARTS = 2.0


def pack_feature_names(pack_groups):
    """要求されたパックグループの特徴量名を、固定順 (A3→A7→A8→X3) で返す。"""
    if not pack_groups:
        return []
    selected = [g for g in PACK_GROUP_ORDER if g in pack_groups]
    names = []
    for group in selected:
        names.extend(PACK_GROUPS[group])
    return names


def _time_index_value(pr, use_variant):
    """単一走のタイム指数 (標準タイム+馬場差-実タイム、クリップ済み)。

    現行tfeat/cfeatが使う値と同一の定義 (build_dataset内のtbest計算と同じ
    scoring.std_time/track_variant呼び出し)。X3はこの値の分布・推移だけを見る。
    """
    if not pr.get("time_sec") or not pr.get("condition"):
        return None
    base = scoring.std_time(pr["place"], pr["track_type"], pr["distance"],
                            pr["race_class"], pr["condition"])
    if base is None:
        return None
    tv = scoring.track_variant(pr["date"], pr["place"],
                               pr["track_type"]) if use_variant else 0.0
    return max(-TIME_DIFF_CLIP, min(TIME_DIFF_CLIP, base + tv - pr["time_sec"]))


def _days_between(later, earlier):
    try:
        d1 = datetime.strptime(later, "%Y%m%d")
        d0 = datetime.strptime(earlier, "%Y%m%d")
    except (TypeError, ValueError):
        return None
    return (d1 - d0).days


def _horse_layoff_states(lst):
    """休養(90日以上)明け何戦目かを、その馬のキャリア列(時系列昇順)全体について
    1回だけ計算する。値は各インデックス位置の「その走時点の状態」(0=休養未経験
    または休養区間対象外、1..5=休養明けから数えた出走数・上限5クリップ)。
    どちらの側も当該走およびそれより未来の情報は使わない。
    """
    states = [0] * len(lst)
    state = 0
    for j in range(1, len(lst)):
        gap = _days_between(lst[j]["date"], lst[j - 1]["date"])
        if gap is not None and gap >= 90:
            state = 1
        elif state > 0:
            state = min(state + 1, 5)
        states[j] = state
    return states


def _pack_feature_values(lst, i, groups, *, use_variant, layoff_states, tfeat_current):
    """行iのパック特徴量を計算する。lst[:i] (当該走より前) のみを参照する。"""
    cur = lst[i]
    full_prior = lst[:i]
    prev = full_prior[-1] if full_prior else None
    prev_prev = full_prior[-2] if len(full_prior) >= 2 else None
    values = {}

    if "A3" in groups:
        cur_weight = cur.get("weight")
        prev_weight = prev.get("weight") if prev else None
        no_prior = prev is None or prev_weight is None
        today_missing = cur_weight is None
        effective_cur_weight = cur_weight if cur_weight is not None else prev_weight
        if no_prior or effective_cur_weight is None:
            delta_prev = 0.0
            delta_rate = 0.0
        else:
            delta_prev = float(effective_cur_weight) - float(prev_weight)
            delta_rate = delta_prev / prev_weight if prev_weight else 0.0
        past_weights = [p.get("weight") for p in full_prior if p.get("weight") is not None]
        if len(past_weights) < 2 or effective_cur_weight is None:
            dev_expanding = 0.0
            dev_missing = 1.0
        else:
            median = statistics.median(past_weights)
            mad = statistics.median([abs(w - median) for w in past_weights])
            dev_expanding = ((float(effective_cur_weight) - median) / mad) if mad > 0 else 0.0
            dev_missing = 0.0
        if not no_prior:
            gap = _days_between(cur["date"], prev["date"])
            layoff_ln = math.log(max(gap, 1)) if gap is not None else 0.0
        else:
            layoff_ln = 0.0
        values["weight_delta_prev"] = delta_prev
        values["weight_delta_rate"] = delta_rate
        values["weight_no_prior_flag"] = 1.0 if no_prior else 0.0
        values["weight_today_missing_flag"] = 1.0 if today_missing else 0.0
        values["weight_dev_expanding"] = dev_expanding
        values["weight_dev_expanding_missing_flag"] = dev_missing
        values["weight_delta_x_layoff"] = delta_prev * layoff_ln

    if "A7" in groups:
        starts_28 = starts_56 = 0
        for p in full_prior:
            gap = _days_between(cur["date"], p["date"])
            if gap is None or gap <= 0:
                continue
            if gap <= 28:
                starts_28 += 1
            if gap <= 56:
                starts_56 += 1
        if prev is not None:
            class_delta = float(CLASS_RANK.get(cur.get("race_class"), 0)
                                - CLASS_RANK.get(prev.get("race_class"), 0))
        else:
            class_delta = 0.0
        if prev is not None and cur.get("distance") and prev.get("distance"):
            dist_delta_log = math.log(cur["distance"] / prev["distance"])
        else:
            dist_delta_log = 0.0
        surface_change = 1.0 if (
            prev is not None and prev.get("track_type") != cur.get("track_type")) else 0.0
        venue_change = 1.0 if (
            prev is not None and prev.get("place") != cur.get("place")) else 0.0
        jockey_change = 1.0 if (
            prev is not None and prev.get("jockey") != cur.get("jockey")) else 0.0
        values["starts_28d"] = float(starts_28)
        values["starts_56d"] = float(starts_56)
        values["run_after_layoff"] = float(layoff_states[i])
        values["class_delta"] = class_delta
        values["dist_delta_log"] = dist_delta_log
        values["surface_change"] = surface_change
        values["venue_change_transport_proxy"] = venue_change
        values["jockey_change"] = jockey_change

    if "A8" in groups:
        cur_weight = cur.get("weight")
        prev_weight = prev.get("weight") if prev else None
        effective_weight = cur_weight if cur_weight is not None else prev_weight
        cur_kinryo = cur.get("kinryo")
        prev_kinryo = prev.get("kinryo") if prev else None
        if prev is not None and cur_kinryo is not None and prev_kinryo is not None:
            kinryo_delta_prev = float(cur_kinryo) - float(prev_kinryo)
        else:
            kinryo_delta_prev = 0.0
        if cur_kinryo is not None and effective_weight:
            kinryo_per_weight = float(cur_kinryo) / float(effective_weight)
        else:
            kinryo_per_weight = 0.0
        is_handicap = 1.0 if HANDICAP_RACE_NAME_MARKER_RE.search(
            str(cur.get("race_name") or "")) else 0.0
        values["kinryo_delta_prev"] = kinryo_delta_prev
        values["kinryo_per_weight"] = kinryo_per_weight
        values["kinryo_delta_x_handicap"] = kinryo_delta_prev * is_handicap

    if "X3" in groups:
        window = full_prior[-X3_WINDOW:][::-1]  # recency順 (0=直近)
        indexed = [(j, _time_index_value(p, use_variant)) for j, p in enumerate(window)]
        valid = [(j, v) for j, v in indexed if v is not None]
        if valid:
            weights = [2.0 ** (-j / X3_HALFLIFE_STARTS) for j, _ in valid]
            wsum = sum(weights)
            rwmean = sum(w * v for (_j, v), w in zip(valid, weights)) / wsum
        else:
            rwmean = 0.0
        if len(valid) >= 2:
            tstd = statistics.pstdev([v for _j, v in valid])
            std_missing = 0.0
        else:
            tstd = 0.0
            std_missing = 1.0
        ordered = sorted(valid, key=lambda item: item[0])
        if len(ordered) >= 2:
            slope2 = ordered[0][1] - ordered[1][1]
        else:
            slope2 = 0.0
        gap_from_max = rwmean - tfeat_current
        post_layoff_delta = 0.0
        if prev is not None and layoff_states[i - 1] == 1 and prev_prev is not None:
            v_prev = _time_index_value(prev, use_variant)
            v_prev_prev = _time_index_value(prev_prev, use_variant)
            if v_prev is not None and v_prev_prev is not None:
                post_layoff_delta = v_prev - v_prev_prev
        values["tfeat_rwmean"] = rwmean
        values["tfeat_std"] = tstd
        values["tfeat_std_missing_flag"] = std_missing
        values["tfeat_slope2"] = slope2
        values["tfeat_gap_from_max"] = gap_from_max
        values["tfeat_post_layoff_delta"] = post_layoff_delta

    return values


PEDIGREE_MIN_COVERAGE = 0.70  # 評価出走の血統判明率がこれ未満なら sire_pts は無効のまま

WET = {"稍", "重", "不"}
PCI_TRACK_MEAN = {"芝": 51.96, "ダート": 45.83}  # backtest_pace.TRACK_MEAN と同値
MODEL_PATH = os.path.join(API_DIR, "data_files", "common", "win5_ml_model.json")


def _pci_dev(run):
    if run.get("pci") is None or run["track_type"] not in PCI_TRACK_MEAN:
        return None
    return run["pci"] - PCI_TRACK_MEAN[run["track_type"]]


def _append_relative_features(X, race_keys, availability):
    """絶対特徴量行列の末尾へ、同一レース内で計算した相対特徴量を加える。

    ``availability`` は各行について元特徴量が実際に取得できたかを保持する。
    欠損値を既存モデル用の既定値（0、斤量55など）と区別し、相対値は必ず0に
    落とす。平均・中央値・順位は取得できた馬だけで計算する。
    """
    # The yearly T33 build can hold tens of thousands of Python row lists at
    # once.  For that list input, first compact to float32 and release the row
    # objects before allocating the 29-column result.  The default 21-column
    # path never enters here, and ndarray callers retain their original dtype.
    if isinstance(X, list):
        base = np.empty((len(X), len(FEATURES)), dtype=np.float32)
        for row_index, row in enumerate(X):
            base[row_index] = row
            X[row_index] = None
        X.clear()
    else:
        base = np.asarray(X, dtype=float)
    if base.ndim == 1 and base.size == 0:
        base = np.empty((0, len(FEATURES)), dtype=float)
    if base.ndim != 2 or base.shape[1] != len(FEATURES):
        raise ValueError(f"相対特徴量の入力列数が不正です: {base.shape}")
    if len(base) != len(race_keys) or len(base) != len(availability):
        raise ValueError("特徴量・race_keys・欠損maskの行数が一致しません")

    extended = np.zeros(
        (len(base), len(FEATURES) + len(RELATIVE_FEATURES)), dtype=base.dtype)
    extended[:, :len(FEATURES)] = base
    relative = extended[:, len(FEATURES):]
    race_rows = defaultdict(list)
    for row_index, race_key in enumerate(race_keys):
        race_rows[race_key].append(row_index)

    source_columns = {
        "tfeat": FEATURES.index("tfeat"),
        "j_pts": FEATURES.index("j_pts"),
        "f_pts": FEATURES.index("f_pts"),
        "sire_pts": FEATURES.index("sire_pts"),
        "kinryo": FEATURES.index("kinryo"),
        "n_prior": FEATURES.index("n_prior"),
    }

    for rows in race_rows.values():
        # tfeat順位・中央値差・最良値差。大きい値を上位とし、同値は平均順位。
        t_rows = [i for i in rows if _relative_value_available(
            availability[i], "tfeat")]
        if t_rows:
            values = np.asarray([base[i, source_columns["tfeat"]] for i in t_rows])
            median = float(np.median(values))
            best = float(np.max(values))
            field_count = len(rows)
            for i, value in zip(t_rows, values):
                if field_count == 1:
                    rank_score = 1.0
                else:
                    greater = int(np.count_nonzero(values > value))
                    ties = int(np.count_nonzero(values == value))
                    average_rank = greater + (ties + 1) / 2.0
                    # SPEC-T13の「頭数で正規化」は取得可能頭数ではなく
                    # レース全頭数を分母にする。欠損馬自身は引き続き0。
                    rank_score = (
                        (field_count - average_rank) / (field_count - 1))
                relative[i, 0] = rank_score
                relative[i, 1] = float(value) - median
                relative[i, 2] = float(value) - best

        for source, target_col in (("j_pts", 3), ("f_pts", 4),
                                   ("sire_pts", 5), ("kinryo", 6),
                                   ("n_prior", 7)):
            available_rows = [i for i in rows if _relative_value_available(
                availability[i], source)]
            if not available_rows:
                continue
            mean = float(np.mean([base[i, source_columns[source]] for i in available_rows]))
            for i in available_rows:
                relative[i, target_col] = base[i, source_columns[source]] - mean

    return extended


def _relative_value_available(mask, source):
    if isinstance(mask, (int, np.integer)):
        return bool(mask & _RELATIVE_AVAILABILITY_BITS[source])
    return bool(mask.get(source, False))


def build_dataset(runs, date_from, cfg, relative=False, pack_groups=None):
    """評価出走ごとに特徴量ベクトルを構築 (score_all_runners と同じ部品を使用)

    ``pack_groups`` は T41 低コスト特徴パック ({"A3","A7","A8","X3"} の部分集合)。
    None/空のときは一切のコード分岐に入らず、既存の出力と完全に同一になる。
    """
    from backtest_criteria import (load_filtered_criteria, load_mawari,
                                   build_h, attach_agari_rank)
    import analysis

    if pack_groups:
        unknown = set(pack_groups) - set(PACK_GROUP_ORDER)
        if unknown:
            raise ValueError(f"未知のpack_groups: {sorted(unknown)}")
        active_pack_groups = frozenset(pack_groups)
    else:
        active_pack_groups = frozenset()

    params = cfg["params"]
    weights = cfg["weights"]
    ab = params["ability"]
    pos_factor = {int(k): v for k, v in ab["pos_factor"].items()}
    use_variant = ab.get("track_variant", False)

    criteria_map = load_filtered_criteria()
    mawari_map = load_mawari()
    attach_agari_rank(runs)

    # 血統 (horse_pedigree)。カバレッジが閾値未満なら sire_pts は0のまま (安全に退化)
    pedigree = {}
    try:
        import pedigree_store
        pedigree = pedigree_store.load_all()
    except Exception as e:
        print(f"[INFO] 血統読み込みスキップ: {e}")
    eval_horses = {r["horse"] for r in runs if r["horse"] and r["date"] >= date_from}
    ped_cov = (sum(1 for h in eval_horses if h in pedigree) / len(eval_horses)) if eval_horses else 0.0
    use_pedigree = ped_cov >= PEDIGREE_MIN_COVERAGE
    print(f"血統カバレッジ: {100*ped_cov:.1f}% ({len(pedigree)}頭登録) → "
          f"sire_pts {'有効' if use_pedigree else f'無効 (閾値{100*PEDIGREE_MIN_COVERAGE:.0f}%未満)'}")

    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    # ── 1パス目: レースごとの予測ペース (出走馬の直近4走PCI偏差平均の平均) ──
    race_pre = defaultdict(list)
    for horse, lst in by_horse.items():
        for i, cur in enumerate(lst):
            if cur["date"] < date_from or cur["rank"] is None:
                continue
            devs = [d for d in (_pci_dev(p) for p in lst[max(0, i - 4):i]) if d is not None]
            if devs:
                race_pre[(cur["date"], cur["place"], cur["r"])].append(sum(devs) / len(devs))
    pred_pace = {k: sum(v) / len(v) for k, v in race_pre.items() if len(v) >= 5}

    ftables = {}

    def get_ftable(place, track, dist):
        key = (place, track, dist)
        if key not in ftables:
            ftables[key] = scoring.load_factor_table(place, track, dist, API_DIR)
        return ftables[key]

    jmatcher = JockeyMatcher()
    X, y, race_keys, meta = [], [], [], []
    # Six availability flags fit in one byte per runner; a bytearray avoids
    # retaining tens of thousands of Python dict/int objects in yearly folds.
    relative_availability = bytearray()
    X_pack = [] if active_pack_groups else None
    pack_names = pack_feature_names(active_pack_groups) if active_pack_groups else None

    for horse, lst in by_horse.items():
        layoff_states = _horse_layoff_states(lst) if active_pack_groups else None
        for i, cur in enumerate(lst):
            if cur["date"] < date_from or cur["rank"] is None:
                continue
            prior = lst[max(0, i - 4):i][::-1]
            if not prior:
                continue

            f = dict.fromkeys(FEATURES, 0.0)
            available = 0

            # バイアス (騎手/枠/距離変更/コース替わり/所属) — db-keiba統計点を特徴量に
            table = get_ftable(cur["place"], cur["track_type"], cur["distance"])
            if table:
                baseline = table["baseline"].get("win_rate")
                if baseline is not None:
                    def bias_pts(fmap, key, w=1.0):
                        row = (fmap or {}).get(key) if key else None
                        if row is None:
                            return None
                        p, _ = scoring._factor_points(row, baseline, params, w, "win_rate")
                        return p

                    # ability snapshotではability.db側の短縮名に合わせて、ライブ
                    # _ml_featuresと同じ双方向fallbackを使う。従来CSVは既存の
                    # JockeyMatcherを維持し、snapshot未配置時の挙動を変えない。
                    if (table.get("_meta") or {}).get("source") == "ability.db:runs":
                        row = scoring._match_entity(
                            table.get("jockey_w"), cur["jockey"])
                    else:
                        row = jmatcher.match(
                            table.get("jockey_w") or {}, cur["jockey"])
                    if row is not None:
                        p, _ = scoring._factor_points(row, baseline, params,
                                                      weights.get("jockey_w", 0), "win_rate")
                        if p is not None:
                            f["j_pts"] = p
                            available |= _RELATIVE_AVAILABILITY_BITS["j_pts"]
                    waku = calculate_waku(cur["umaban"], cur["total_horses"])
                    if waku:
                        p = bias_pts(table.get("frame"), f"{waku}枠", weights.get("frame", 0))
                        if p is not None:
                            f["f_pts"] = p
                            available |= _RELATIVE_AVAILABILITY_BITS["f_pts"]
                    # 距離変更 (前走距離との実差分 → db-keibaの7段階ラベル)
                    d_label = scoring._distance_label(cur["distance"], prior[0]["distance"])
                    p = bias_pts(table.get("distance"), scoring._norm(d_label) if d_label else None)
                    if p is not None:
                        f["dist_pts"] = p
                    # コース替わり (前走芝ダ → 今回芝ダ)
                    s_label = (f"{'芝' if prior[0]['track_type'] == '芝' else 'ダ'}"
                               f"→{'芝' if cur['track_type'] == '芝' else 'ダ'}")
                    p = bias_pts(table.get("surface"), scoring._norm(s_label))
                    if p is not None:
                        f["surf_pts"] = p
                    # 所属 (美浦/栗東)
                    affi = "美浦" if "美" in str(cur.get("affi") or "") else \
                           ("栗東" if "栗" in str(cur.get("affi") or "") else None)
                    p = bias_pts(table.get("stable_trainer"), scoring._norm(affi) if affi else None)
                    if p is not None:
                        f["affi_pts"] = p
                    # 種牡馬 (father_w)。血統カバレッジが閾値以上のときのみ
                    if use_pedigree:
                        ped = pedigree.get(horse)
                        if ped and ped.get("sire"):
                            row = scoring._match_entity(table.get("father_w"), ped["sire"])
                            if row is not None:
                                p, _ = scoring._factor_points(row, baseline, params, 1.0, "win_rate")
                                if p is not None:
                                    f["sire_pts"] = p
                                    available |= _RELATIVE_AVAILABILITY_BITS["sire_pts"]

            # 能力系
            tbest, cbest, amin = None, None, None
            for j, pr in enumerate(prior):
                rw = RECENCY[j] if j < len(RECENCY) else RECENCY[-1]
                if pr["time_sec"] and pr["condition"]:
                    base = scoring.std_time(pr["place"], pr["track_type"], pr["distance"],
                                            pr["race_class"], pr["condition"])
                    if base is not None:
                        tv = scoring.track_variant(pr["date"], pr["place"],
                                                   pr["track_type"]) if use_variant else 0.0
                        diff = max(-TIME_DIFF_CLIP, min(TIME_DIFF_CLIP, base + tv - pr["time_sec"]))
                        v = diff * rw
                        tbest = v if tbest is None else max(tbest, v)
                pf = pos_factor.get(pr["rank"], 0.0)
                if pf:
                    v = CLASS_RANK.get(pr["race_class"], 0) * pf * rw
                    cbest = v if cbest is None else max(cbest, v)
                if pr.get("agari_ratio") is not None:
                    amin = pr["agari_ratio"] if amin is None else min(amin, pr["agari_ratio"])
            f["tfeat"] = tbest if tbest is not None else 0.0
            if tbest is not None:
                available |= _RELATIVE_AVAILABILITY_BITS["tfeat"]
            f["cfeat"] = cbest if cbest is not None else 0.0
            f["agari_flag"] = 1.0 if (amin is not None and amin <= ab["agari_best_ratio"]) else 0.0

            # 市場
            odds = parse_final_odds(cur.get("win_pay"), cur["rank"])
            f["ln_odds"] = math.log(odds) if odds and odds > 1.0 else math.log(30.0)

            # 追加素性
            f["prev_rank"] = min(prior[0]["rank"], 18) if prior[0]["rank"] else 10
            try:
                d1 = datetime.strptime(cur["date"], "%Y%m%d")
                d0 = datetime.strptime(prior[0]["date"], "%Y%m%d")
                f["ln_interval"] = math.log(max((d1 - d0).days, 1))
            except ValueError:
                f["ln_interval"] = math.log(30)
            f["age"] = cur.get("age") or 4
            f["is_male"] = 1.0 if cur.get("sex") in ("牡", "セ") else 0.0
            f["kinryo"] = cur.get("kinryo") or 55.0
            if cur.get("kinryo") is not None:
                available |= _RELATIVE_AVAILABILITY_BITS["kinryo"]
            f["weight"] = cur.get("weight") or 470
            f["n_prior"] = len(prior)
            available |= _RELATIVE_AVAILABILITY_BITS["n_prior"]

            # 道悪適性 (当日道悪 × 相対適性。backtest_wetで検証済みの定義)
            if cur.get("condition") in WET:
                wet_ranks = [p["rank"] for p in prior if p["condition"] in WET and p["rank"]]
                dry_ranks = [p["rank"] for p in prior if p["condition"] == "良" and p["rank"]]
                if wet_ranks and dry_ranks:
                    gap = min(wet_ranks) - min(dry_ranks)
                    if gap <= -2:
                        f["wet_match"] = 1.0
                    elif gap >= 2:
                        f["wet_match"] = -1.0

            # ペース適性: 型PCI偏差 × メンバー予測ペース (backtest_pace C定義)
            pv = pred_pace.get((cur["date"], cur["place"], cur["r"]))
            devs = [d for d in (_pci_dev(p) for p in prior) if d is not None]
            if pv is not None and devs:
                f["pace_fit"] = (sum(devs) / len(devs)) * pv

            # 同コース実績: 直近4走に同場・同芝ダ・同距離で3着以内
            f["course_fit"] = 1.0 if any(
                p["place"] == cur["place"] and p["track_type"] == cur["track_type"]
                and p["distance"] == cur["distance"] and p["rank"] and p["rank"] <= 3
                for p in prior) else 0.0

            # 好走条件判定 (criteria.csv 血統以外ルール): ◎3 / 〇2 / △1
            crits = criteria_map.get(cur["place"])
            if crits:
                h_c = build_h(cur, prior[0])
                if use_pedigree:
                    ped = pedigree.get(horse)
                    if ped:  # 血統ルール評価の準備 (現状criteria_mapは血統以外のみ)
                        h_c["sire"] = ped.get("sire") or "-"
                        h_c["bms"] = ped.get("bms") or "-"
                r_ctx = {"type": cur["track_type"], "dist": cur["distance"],
                         "venue": cur["place"], "total_horses": cur["total_horses"],
                         "class": cur["race_class"]}
                grade, _det, _mt = analysis.evaluate_ultra(h_c, r_ctx, crits, {}, mawari_map)
                f["grade_pts"] = {"◎": 3.0, "〇": 2.0, "△": 1.0}.get(grade, 0.0)

            X.append([f[k] for k in FEATURES])
            y.append(1 if cur["rank"] == 1 else 0)
            race_keys.append((cur["date"], cur["place"], cur["r"]))
            meta.append(cur)
            if relative:
                relative_availability.append(available)
            if active_pack_groups:
                pack_values = _pack_feature_values(
                    lst, i, active_pack_groups, use_variant=use_variant,
                    layoff_states=layoff_states, tfeat_current=f["tfeat"])
                X_pack.append([pack_values[name] for name in pack_names])
    if relative:
        X = _append_relative_features(X, race_keys, relative_availability)
    if active_pack_groups:
        base = X if isinstance(X, np.ndarray) else np.asarray(X, dtype=float)
        X = np.hstack([base, np.asarray(X_pack, dtype=float)])
    return np.array(X), np.array(y), race_keys, meta


def coverage_from_scores(scores, race_keys, meta, upset_map):
    races = defaultdict(list)
    for s, key, m in zip(scores, race_keys, meta):
        races[key].append((float(s), m))
    return measure_coverage(races, upset_map)


def fit_conditional_logit(Xz, y, race_keys, l2=1.0, max_iter=300, offset=None):
    """
    conditional logit (レース内softmax / Plackett-Luce top-1) を L-BFGS で学習。
    通常のロジスティック回帰と違い「同レースの他馬との相対比較」を直接最適化する。
    softmax(w·x) がそのままレース内勝率になる (確率キャリブレーション込み)。
    同着は勝ち馬ごとに1イベントとして扱う。勝ち馬が特徴量構築から漏れたレース
    (勝ち馬が初出走等) は尤度に寄与しない。
    """
    from scipy.optimize import minimize

    rid_map = {}
    rid = np.empty(len(race_keys), dtype=np.int64)
    for i, k in enumerate(race_keys):
        rid[i] = rid_map.setdefault(k, len(rid_map))
    order = np.argsort(rid, kind="stable")
    Xs, ys, rs = Xz[order], np.asarray(y)[order].astype(float), rid[order]
    if offset is None:
        offset_values = np.zeros(len(race_keys), dtype=float)
    else:
        offset_values = np.asarray(offset, dtype=float)
        if offset_values.ndim != 1 or len(offset_values) != len(race_keys):
            raise ValueError("offset must be a one-dimensional array matching race_keys")
    offsets = offset_values[order]
    starts = np.flatnonzero(np.r_[True, rs[1:] != rs[:-1]])
    seg = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(rs)]))
    nwin = np.add.reduceat(ys, starts)          # レースごとの勝ち馬数 (通常1, 同着2)
    xwin_sum = Xs[ys == 1].sum(axis=0)

    def negll(w):
        s = offsets + Xs @ w
        smax = np.maximum.reduceat(s, starts)
        e = np.exp(s - smax[seg])
        z = np.add.reduceat(e, starts)
        lse = smax + np.log(z)
        ll = float(s[ys == 1].sum() - (nwin * lse).sum()) - 0.5 * l2 * float(w @ w)
        p = e / z[seg]
        grad = Xs.T @ (p * nwin[seg]) - xwin_sum + l2 * w
        return -ll, grad

    res = minimize(negll, np.zeros(Xs.shape[1]), jac=True,
                   method="L-BFGS-B", options={"maxiter": max_iter})
    if not res.success:
        print(f"  [WARN] conditional logit 収束警告: {res.message}")
    return res.x


def fit_temperature(scores, y, race_keys, grid=None):
    """softmax温度 τ をキャリブレーション用データの条件付き対数尤度最大化で決定。
    τ>1 = モデルが過信気味 (確率を平滑化)。"""
    rid_map = {}
    rid = np.empty(len(race_keys), dtype=np.int64)
    for i, k in enumerate(race_keys):
        rid[i] = rid_map.setdefault(k, len(rid_map))
    order = np.argsort(rid, kind="stable")
    s, ys, rs = np.asarray(scores)[order], np.asarray(y)[order].astype(float), rid[order]
    starts = np.flatnonzero(np.r_[True, rs[1:] != rs[:-1]])
    seg = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(rs)]))
    nwin = np.add.reduceat(ys, starts)

    def nll(tau):
        st = s / tau
        smax = np.maximum.reduceat(st, starts)
        lse = smax + np.log(np.add.reduceat(np.exp(st - smax[seg]), starts))
        return -(st[ys == 1].sum() - (nwin * lse).sum())

    grid = grid if grid is not None else np.arange(0.70, 1.61, 0.02)
    best = min(grid, key=nll)
    return float(round(best, 2))


def _group_by_race(Xz, y, race_keys):
    """レース順に整列した (X, y, グループサイズ配列) を返す (LightGBM group用)"""
    rid_map = {}
    rid = np.empty(len(race_keys), dtype=np.int64)
    for i, k in enumerate(race_keys):
        rid[i] = rid_map.setdefault(k, len(rid_map))
    order = np.argsort(rid, kind="stable")
    rs = rid[order]
    starts = np.flatnonzero(np.r_[True, rs[1:] != rs[:-1]])
    sizes = np.diff(np.r_[starts, len(rs)])
    return Xz[order], np.asarray(y)[order], sizes


def fit_lgbm_rank(Xz_tr, y_tr, keys_tr, Xz_va, y_va, keys_va, seed=7):
    """LambdaRank (レース内ランキング学習)。検証セットで早期停止し、
    (booster, best_iteration) を返す。"""
    import lightgbm as lgb
    Xs, ys, gs = _group_by_race(Xz_tr, y_tr, keys_tr)
    Xv, yv, gv = _group_by_race(Xz_va, y_va, keys_va)
    params = {
        "objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [1, 3],
        "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 100,
        "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 1,
        "lambdarank_truncation_level": 8, "verbosity": -1, "seed": seed,
    }
    ds_tr = lgb.Dataset(Xs, label=ys, group=gs)
    ds_va = lgb.Dataset(Xv, label=yv, group=gv, reference=ds_tr)
    booster = lgb.train(params, ds_tr, num_boost_round=800, valid_sets=[ds_va],
                        callbacks=[lgb.early_stopping(50, verbose=False)])
    return booster, booster.best_iteration


def build_prob_races(scores, race_keys, meta, temp=1.0):
    """スコア → レースごとに softmax(スコア/τ) 勝率を付与。
    戻り値: {key: [(score, prob, meta), ...] スコア降順}"""
    races = defaultdict(list)
    for s, k, m in zip(scores, race_keys, meta):
        races[k].append((float(s), m))
    out = {}
    for k, mem in races.items():
        mem.sort(key=lambda x: -x[0])
        mx = mem[0][0]
        es = [math.exp((s - mx) / temp) for s, _ in mem]
        z = sum(es)
        out[k] = [(s, e / z, m) for (s, m), e in zip(mem, es)]
    return out


def feature_matrix(X, feature_names):
    """FEATURES順の行列から、指定した特徴量だけを同じ順序で取り出す。"""
    matrix = np.asarray(X)
    source_names = active_feature_names(
        relative=(matrix.ndim == 2 and matrix.shape[1] == len(FEATURES) + len(RELATIVE_FEATURES))
    )
    unknown = [name for name in feature_names if name not in source_names]
    if unknown:
        raise ValueError(f"未知の特徴量: {unknown}")
    indices = [source_names.index(name) for name in feature_names]
    return matrix[:, indices]


def _softmax(scores, temperature=1.0):
    if temperature <= 0:
        raise ValueError("temperature must be positive")
    values = np.asarray(scores, dtype=float) / temperature
    values -= values.max()
    exp_values = np.exp(values)
    return exp_values / exp_values.sum()


def _rank_positions(order):
    positions = [0] * len(order)
    for rank, member_index in enumerate(order, 1):
        positions[member_index] = rank
    return positions


def _spearman_orders(left_order, right_order):
    """同一メンバーの2順位についてSpearman相関を返す。"""
    left = np.asarray(_rank_positions(left_order), dtype=float)
    right = np.asarray(_rank_positions(right_order), dtype=float)
    left -= left.mean()
    right -= right.mean()
    denom = math.sqrt(float(left @ left) * float(right @ right))
    return float(left @ right) / denom if denom else None


def build_model_comparison_races(current_scores, m2_scores, race_keys, meta,
                                  date_from, date_to, current_temp=1.0,
                                  m2_temp=1.0, min_r=9):
    """M0/現行CL/M2を完全に同じレース・出走馬で比較できる形へまとめる。

    市場確率に必要な全馬の確定オッズ、モデルに必要な全馬の特徴量が揃わない
    レースは比較対象から除く。順位評価のM0はJRA人気順、確率評価のM0は
    レース内で正規化した1/oddsを使う。
    """
    grouped = defaultdict(list)
    for current, m2, key, runner in zip(current_scores, m2_scores, race_keys, meta):
        if date_from <= key[0] <= date_to:
            grouped[key].append((float(current), float(m2), runner))

    skipped = defaultdict(int)
    races = []
    for key in sorted(grouped):
        _date, _place, race_no = key
        members = grouped[key]
        if race_no is None or race_no < min_r or len(members) < 8:
            skipped["filter"] += 1
            continue

        field_sizes = [int(m.get("total_horses") or 0) for _, _, m in members]
        field_size = max(field_sizes, default=0)
        if field_size and len(members) != field_size:
            skipped["incomplete_features"] += 1
            continue

        winner_index = next(
            (index for index, (_, _, runner) in enumerate(members)
             if runner.get("rank") == 1), None)
        if winner_index is None:
            skipped["no_winner"] += 1
            continue

        odds = [parse_final_odds(runner.get("win_pay"), runner.get("rank"))
                for _, _, runner in members]
        if any(value is None or value <= 1.0 for value in odds):
            skipped["missing_odds"] += 1
            continue

        inverse_odds = np.asarray([1.0 / value for value in odds], dtype=float)
        market_probabilities = inverse_odds / inverse_odds.sum()
        current_values = [value for value, _, _ in members]
        m2_values = [value for _, value, _ in members]

        def market_sort_key(index):
            runner = members[index][2]
            popularity = runner.get("popularity")
            try:
                popularity = int(popularity)
            except (TypeError, ValueError):
                popularity = 999
            return popularity, odds[index], int(runner.get("umaban") or 99)

        market_order = sorted(range(len(members)), key=market_sort_key)
        current_order = sorted(range(len(members)),
                               key=lambda index: (-current_values[index], index))
        m2_order = sorted(range(len(members)),
                          key=lambda index: (-m2_values[index], index))
        races.append({
            "key": key,
            "winner_index": winner_index,
            "market_order": market_order,
            "current_order": current_order,
            "m2_order": m2_order,
            "market_probabilities": market_probabilities,
            "current_probabilities": _softmax(current_values, current_temp),
            "m2_probabilities": _softmax(m2_values, m2_temp),
        })
    return races, dict(skipped)


def evaluate_model_comparison(races, kmax=4):
    """同一レース集合のtop-k、レースLogLoss、馬単位Brierを集計する。"""
    if not races:
        return None
    definitions = (
        ("M0 人気順", "market_order", "market_probabilities"),
        ("現行CL (オッズ込み)", "current_order", "current_probabilities"),
        ("M2 (オッズなし)", "m2_order", "m2_probabilities"),
    )
    metrics = {}
    for label, order_key, probability_key in definitions:
        topk_hits = [0] * kmax
        log_loss = 0.0
        brier = 0.0
        horse_count = 0
        for race in races:
            winner_index = race["winner_index"]
            winner_position = race[order_key].index(winner_index) + 1
            for k in range(kmax):
                if winner_position <= k + 1:
                    topk_hits[k] += 1
            probabilities = np.asarray(race[probability_key], dtype=float)
            log_loss -= math.log(max(1e-15, float(probabilities[winner_index])))
            outcomes = np.zeros(len(probabilities), dtype=float)
            outcomes[winner_index] = 1.0
            brier += float(np.square(outcomes - probabilities).sum())
            horse_count += len(probabilities)
        metrics[label] = {
            "coverage": [value / len(races) for value in topk_hits],
            "log_loss": log_loss / len(races),
            "brier": brier / horse_count,
        }

    correlations = [
        _spearman_orders(race["m2_order"], race["market_order"])
        for race in races
    ]
    correlations = [value for value in correlations if value is not None]
    return {
        "races": len(races),
        "horses": sum(len(race["market_order"]) for race in races),
        "models": metrics,
        "m2_market_spearman": (
            sum(correlations) / len(correlations) if correlations else None),
        "spearman_races": len(correlations),
    }


def fit_cl_diagnostic_model(X, y, race_keys, dates, feature_names):
    """2021-24学習・2024温度調整の固定分割でCL診断モデルを作る。"""
    from sklearn.preprocessing import StandardScaler

    matrix = feature_matrix(X, feature_names)
    train = dates <= TRAIN_TO
    scaler = StandardScaler().fit(matrix[train])
    standardized_train = scaler.transform(matrix[train])
    train_y = y[train]
    train_keys = [key for key, selected in zip(race_keys, train) if selected]
    weights = fit_conditional_logit(standardized_train, train_y, train_keys)

    inner_train = dates[train] <= "20231231"
    inner_weights = fit_conditional_logit(
        standardized_train[inner_train], train_y[inner_train],
        [key for key, selected in zip(train_keys, inner_train) if selected])
    calibration = ~inner_train
    temperature = fit_temperature(
        standardized_train[calibration] @ inner_weights,
        train_y[calibration],
        [key for key, selected in zip(train_keys, calibration) if selected])
    return {
        "features": list(feature_names),
        "scaler": scaler,
        "weights": weights,
        "temperature": temperature,
    }


def predict_cl_diagnostic_model(model, X):
    matrix = feature_matrix(X, model["features"])
    return model["scaler"].transform(matrix) @ model["weights"]


def _print_no_market_period(title, races, skipped):
    result = evaluate_model_comparison(races)
    print(f"\n===== {title} (9R以降・8頭以上・全馬オッズ/特徴量あり) =====")
    if result is None:
        print("対象レースなし")
        return
    print(f"同一比較集団: {result['races']}レース / {result['horses']}出走"
          f" / 除外 {sum(skipped.values())}レース {skipped}")
    print(f"{'model':24s} | k=1   k=2   k=3   k=4")
    for label, values in result["models"].items():
        coverage = "  ".join(f"{100.0*value:4.1f}" for value in values["coverage"])
        print(f"{label:24s} | {coverage}")
    print("\nmodel                    | Race LogLoss | Brier (馬単位)")
    for label, values in result["models"].items():
        print(f"{label:24s} | {values['log_loss']:12.6f} | {values['brier']:.8f}")
    rho = result["m2_market_spearman"]
    print(f"\nM2順位×市場人気順位 Spearman (レース内平均): "
          f"{rho:.4f} (n={result['spearman_races']})")


def run_no_market_diagnostic(X, y, race_keys, meta, dates):
    """本番へ保存せず、現行CLとM2を固定分割で比較・報告する。"""
    if "ln_odds" in NO_MARKET_FEATURES or len(NO_MARKET_FEATURES) != len(FEATURES) - 1:
        raise AssertionError("M2特徴量はln_oddsだけを除外する必要があります")

    print(f"特徴量: 現行 {len(FEATURES)}次元 / M2 {len(NO_MARKET_FEATURES)}次元 "
          "(除外: ln_odds)")
    current_model = fit_cl_diagnostic_model(X, y, race_keys, dates, FEATURES)
    m2_model = fit_cl_diagnostic_model(X, y, race_keys, dates, NO_MARKET_FEATURES)
    current_scores = predict_cl_diagnostic_model(current_model, X)
    m2_scores = predict_cl_diagnostic_model(m2_model, X)
    print("温度キャリブレーション (21-23学習→24調整): "
          f"現行 τ={current_model['temperature']:.2f} / M2 τ={m2_model['temperature']:.2f}")
    print("M2係数 (標準化後、|係数|順):")
    for name, coefficient in sorted(
            zip(NO_MARKET_FEATURES, m2_model["weights"]), key=lambda item: -abs(item[1])):
        print(f"  {name:12s}: {coefficient:+.3f}")

    for title, date_from, date_to in (
            ("固定テスト 2025年", TEST_FROM, TEST_TO),
            ("追加確認 2026H1", ADDITIONAL_TEST_FROM, ADDITIONAL_TEST_TO)):
        races, skipped = build_model_comparison_races(
            current_scores, m2_scores, race_keys, meta, date_from, date_to,
            current_temp=current_model["temperature"],
            m2_temp=m2_model["temperature"])
        _print_no_market_period(title, races, skipped)


def report_calibration(prob_races, kmax=4, min_r=9):
    """予測カバレッジ (上位k頭の勝率和の平均) と実測カバレッジを比較"""
    pred_sum = [0.0] * kmax
    act_cnt = [0] * kmax
    n = 0
    for (date, place, r), mem in prob_races.items():
        if r is None or r < min_r or len(mem) < 8:
            continue
        wpos = next((i + 1 for i, (_, _, m) in enumerate(mem) if m["rank"] == 1), None)
        if wpos is None:
            continue
        n += 1
        cum = 0.0
        for k in range(kmax):
            if k < len(mem):
                cum += mem[k][1]
            pred_sum[k] += min(cum, 1.0)
            if wpos <= k + 1:
                act_cnt[k] += 1
    if not n:
        return
    print(f"\n===== 確率キャリブレーション (検証セット, n={n}) =====")
    print("  k | 予測カバレッジ | 実測カバレッジ")
    for k in range(kmax):
        print(f"  {k+1} | {100.0*pred_sum[k]/n:12.1f}% | {100.0*act_cnt[k]/n:12.1f}%")


def simulate_win5_compare(prob_races, upset_map, coverage, budgets, min_r=9):
    """土日・R降順5レース近似のWIN5で、荒れランク平均配分 vs レース固有確率配分を比較。
    軸固定は rank版=スコア1-2位差最大 / prob版=最大勝率レース を1点固定。"""
    from backtest_win5 import upset_rank_of

    by_day = defaultdict(list)
    for (date, place, r), mem in prob_races.items():
        if r is None or r < min_r or len(mem) < 8:
            continue
        try:
            if datetime.strptime(date, "%Y%m%d").weekday() not in (5, 6):
                continue
        except ValueError:
            continue
        by_day[date].append((r, place, mem))

    strategies = ["rank通常", "rank軸", "prob通常", "prob軸"]
    results = {b: {s: {"hits": 0, "pts": 0} for s in strategies} for b in budgets}
    day_records = {b: [] for b in budgets}  # prob通常の日別記録 (見送り基準分析用)
    n_days = 0

    for date, cands in sorted(by_day.items()):
        if len(cands) < 5:
            continue
        cands.sort(key=lambda x: (-x[0], x[1]))
        sel = cands[:5]
        winner_pos, ranks, prob_lists, margins, top1 = [], [], [], [], []
        winner_odds = []
        ok = True
        for r, place, mem in sel:
            wpos = next((i + 1 for i, (_, _, m) in enumerate(mem) if m["rank"] == 1), None)
            if wpos is None:
                ok = False
                break
            winner_pos.append(wpos)
            m0 = mem[0][2]
            ranks.append(upset_rank_of(upset_map, place, m0["track_type"], m0["distance"]))
            prob_lists.append([p for _, p, _ in mem])
            margins.append(mem[0][0] - mem[1][0] if len(mem) > 1 else 0.0)
            top1.append(mem[0][1])
            wmeta = next(m for _, _, m in mem if m["rank"] == 1)
            odds = parse_final_odds(wmeta.get("win_pay"), 1)
            winner_odds.append(odds)
        if not ok:
            continue
        n_days += 1
        axis_rank = {max(range(5), key=lambda i: margins[i])}
        axis_prob = {max(range(5), key=lambda i: top1[i])}
        odds_proxy = None
        if all(winner_odds):
            odds_proxy = 1.0
            for o in winner_odds:
                odds_proxy *= o

        for b in budgets:
            picks_p, est_p = scoring.allocate_picks_prob(prob_lists, b)
            allocs = {
                "rank通常": scoring.allocate_picks(ranks, coverage, b)[0],
                "rank軸": scoring.allocate_picks(ranks, coverage, b, fixed=axis_rank)[0],
                "prob通常": picks_p,
                "prob軸": scoring.allocate_picks_prob(prob_lists, b, fixed=axis_prob)[0],
            }
            for name, picks in allocs.items():
                results[b][name]["pts"] += scoring._pts_of(picks)
                if all(wp <= k for wp, k in zip(winner_pos, picks)):
                    results[b][name]["hits"] += 1
            day_records[b].append({
                "date": date, "est": est_p, "pts": scoring._pts_of(picks_p),
                "hit": all(wp <= k for wp, k in zip(winner_pos, picks_p)),
                "odds_proxy": odds_proxy,
            })

    print(f"\n===== WIN5配分比較 (検証セット, 土日R降順5R近似, {n_days}日) =====")
    print("点数  | " + " | ".join(f"{s:14s}" for s in strategies))
    for b in budgets:
        cells = []
        for s in strategies:
            rr = results[b][s]
            cells.append(f"{100.0*rr['hits']/n_days:5.1f}% /{rr['pts']/n_days:6.1f}点"
                         if n_days else "n/a")
        print(f"  {b:4d}| " + " | ".join(cells))
    return results, n_days, day_records


def report_skip_analysis(day_records, budgets):
    """見送り基準: 推定的中率が閾値未満の日をスキップした場合の成績。
    回収プロキシ = 的中日の勝ち馬単勝オッズ積 (WIN5配当の近似) × 100円 / 投入点数"""
    for b in budgets:
        recs = day_records.get(b, [])
        if not recs:
            continue
        ests = sorted(r["est"] for r in recs)
        print(f"\n===== 見送り基準スイープ ({b}点, 全{len(recs)}日, "
              f"推定的中率の分布 中央値{100*ests[len(ests)//2]:.1f}% / "
              f"上位25%点{100*ests[int(len(ests)*0.75)]:.1f}%) =====")
        print("  閾値   | 購入日数 | 的中  | 的中率  | 回収プロキシ")
        for th in (0.0, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.12):
            buy = [r for r in recs if r["est"] >= th]
            if not buy:
                continue
            hits = [r for r in buy if r["hit"]]
            cost = sum(r["pts"] for r in buy) * 100.0
            ret = sum((r["odds_proxy"] or 0.0) * 100.0 for r in hits)
            roi = 100.0 * ret / cost if cost else 0.0
            print(f"  {100*th:5.1f}% | {len(buy):7d} | {len(hits):4d} | "
                  f"{100.0*len(hits)/len(buy):5.1f}% | {roi:6.1f}%")


def parse_cli_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="全期間で再学習してモデルJSONを出力")
    ap.add_argument("--lgbm", action="store_true",
                    help="LightGBM LambdaRank も学習して比較 (要 lightgbm)")
    ap.add_argument("--no-market", action="store_true",
                    help="ln_oddsを除外したM2を現行CL・市場人気と比較")
    ap.add_argument("--m3", action="store_true",
                    help="ability年次as-of統計で市場offset+残差M3を評価")
    ap.add_argument("--m4b", action="store_true",
                    help="ability年次as-of統計で市場の限定補正M4-Bを評価")
    ap.add_argument("--relative", action="store_true",
                    help="ability年次as-of統計でT13レース内相対特徴を評価")
    ap.add_argument("--stats-snapshot", action="store_true",
                    help="ability.db年次as-of統計でT33凍結評価を実行")
    ap.add_argument("--rules-file", action="append", default=None, metavar="PATH",
                    help="--stats-snapshot評価に記録するmined rules CSV (複数可)")
    args = ap.parse_args(argv)
    if args.no_market and args.write:
        ap.error("--no-market と --write は同時に指定できません (M2は本番保存禁止)")
    if args.m3 and args.write:
        ap.error("--m3 と --write は同時に指定できません (M3は評価専用)")
    if args.m4b and args.write:
        ap.error("--m4b と --write は同時に指定できません (M4-Bは評価専用)")
    if args.relative and args.write:
        ap.error("--relative と --write は同時に指定できません (T13は評価専用)")
    if args.stats_snapshot and args.write:
        ap.error("--stats-snapshot と --write は同時に指定できません (T33は評価専用)")
    if args.rules_file and not args.stats_snapshot:
        ap.error("--rules-file は --stats-snapshot と併用してください")
    if args.stats_snapshot and (args.no_market or args.lgbm or args.m4b):
        ap.error("--stats-snapshot は --no-market/--lgbm/--m4b と同時に指定できません")
    if args.m3 and (args.stats_snapshot or args.no_market or args.lgbm or args.m4b):
        ap.error("--m3 は --stats-snapshot/--no-market/--lgbm/--m4b と同時に指定できません")
    if args.relative and (args.m3 or args.m4b or args.stats_snapshot
                          or args.no_market or args.lgbm):
        ap.error("--relative は --m3/--m4b/--stats-snapshot/--no-market/--lgbm と同時に指定できません")
    if args.m4b and (args.relative or args.stats_snapshot
                     or args.no_market or args.lgbm):
        ap.error("--m4b は --relative/--stats-snapshot/--no-market/--lgbm と同時に指定できません")
    return args


def main():
    args = parse_cli_args()
    if args.relative:
        from backtest_relative_diagnostics import main as relative_diagnostic_main
        # 親CLIの --relative を子parserへ再送しない。
        relative_diagnostic_main([])
        return
    if args.m4b:
        from backtest_m4b_diagnostics import main as m4b_diagnostic_main
        # 同様に親CLIの --m4b を評価専用の子parserへ再送しない。
        m4b_diagnostic_main([])
        return
    if args.m3:
        from backtest_market_diagnostics import main as m3_diagnostic_main
        m3_diagnostic_main()
        return
    if args.stats_snapshot:
        # T11/T13との衝突を避け、年次as-of構築と確率評価は独立ハーネスへ委譲。
        from backtest_stats_retrain import main as stats_retrain_main
        delegated = ["--stats-source", "all"]
        for path in args.rules_file or []:
            delegated.extend(["--rules-file", path])
        stats_retrain_main(delegated)
        return

    # 特徴量一致テストは学習器を使わないため、重い任意依存は実行時だけ読み込む。
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    cfg = load_win5_cfg()
    upset_map = load_upset_map()

    conn = sqlite3.connect(DB_PATH)
    data_to = ADDITIONAL_TEST_TO if args.no_market else TEST_TO
    runs = load_runs(conn, TRAIN_FROM, data_to)
    conn.close()
    print(f"ロード: {len(runs)}行")

    X, y, race_keys, meta = build_dataset(runs, TRAIN_FROM, cfg)
    dates = np.array([k[0] for k in race_keys])
    if args.no_market:
        train = dates <= TRAIN_TO
        fixed_test = (dates >= TEST_FROM) & (dates <= TEST_TO)
        additional_test = ((dates >= ADDITIONAL_TEST_FROM)
                           & (dates <= ADDITIONAL_TEST_TO))
        print(f"固定分割: 学習2021-24 {train.sum()}出走 / "
              f"固定テスト2025 {fixed_test.sum()}出走 / "
              f"追加確認2026H1 {additional_test.sum()}出走")
        run_no_market_diagnostic(X, y, race_keys, meta, dates)
        return

    tr = dates <= TRAIN_TO
    te = dates >= TEST_FROM
    print(f"特徴量: {len(FEATURES)}次元 / 学習 {tr.sum()}出走 / 検証 {te.sum()}出走")

    scaler = StandardScaler().fit(X[tr])
    Xz_tr = scaler.transform(X[tr])
    Xz_te = scaler.transform(X[te])
    tr_keys = [k for k, t in zip(race_keys, tr) if t]
    te_keys = [k for k, t in zip(race_keys, te) if t]
    te_meta = [m for m, t in zip(meta, te) if t]

    model = LogisticRegression(max_iter=2000, C=1.0)
    model.fit(Xz_tr, y[tr])
    w_cl = fit_conditional_logit(Xz_tr, y[tr], tr_keys)

    print("\n===== 学習された係数 (標準化後、|LR|順): LR vs conditional logit =====")
    coefs = sorted(zip(FEATURES, model.coef_[0], w_cl), key=lambda x: -abs(x[1]))
    for name, c_lr, c_cl in coefs:
        print(f"  {name:12s}: LR {c_lr:+.3f} / CL {c_cl:+.3f}")

    # ── 検証セットでのカバレッジ比較 ──
    proba = model.decision_function(Xz_te)
    scores_cl = Xz_te @ w_cl
    cov_ml, n_ml = coverage_from_scores(proba, te_keys, te_meta, upset_map)
    cov_cl, n_cl = coverage_from_scores(scores_cl, te_keys, te_meta, upset_map)

    # 現行の手調整スコア (同じ検証期間。履歴なし馬の扱いが僅かに異なる点は誤差)
    races_cur = score_all_runners(runs, TEST_FROM, cfg)
    cov_cur, n_cur = measure_coverage(races_cur, upset_map)

    # 人気順ベースライン (同一 runs / TEST_FROM フィルタ = 同一レース集団)
    races_ninki = popularity_races(runs, TEST_FROM)
    cov_ninki, n_ninki = measure_coverage(races_ninki, upset_map)

    print(f"\n===== 検証セット (2025年) の勝ち馬カバレッジ =====")
    print(f"{'model':26s} | k=1   k=2   k=3   k=4   (n)")
    for label, cov, n in (("人気順ベースライン", cov_ninki, n_ninki),
                          ("現行 (手調整)", cov_cur, n_cur),
                          ("ロジスティック回帰", cov_ml, n_ml),
                          ("conditional logit", cov_cl, n_cl)):
        d = cov.get("default", [])
        row = "  ".join(f"{c*100:4.1f}" for c in d[:4]) if d else "n/a"
        print(f"{label:26s} | {row}  ({n.get('default_all', 0)})")

    # ── 温度キャリブレーション (内部分割: 学習21-23 → 温度調整24。検証25は使わない) ──
    inner_tr = dates[tr] <= "20231231"
    w_inner = fit_conditional_logit(Xz_tr[inner_tr], y[tr][inner_tr],
                                    [k for k, t in zip(tr_keys, inner_tr) if t])
    cal = ~inner_tr
    temp = fit_temperature(Xz_tr[cal] @ w_inner, y[tr][cal],
                           [k for k, t in zip(tr_keys, cal) if t])
    print(f"\n温度キャリブレーション (21-23学習→24調整): τ = {temp:.2f} "
          f"({'過信を平滑化' if temp > 1 else '据え置き' if temp == 1 else '先鋭化'})")

    # ── 確率キャリブレーション + WIN5配分比較 (CLのsoftmax勝率, τ適用) ──
    prob_races = build_prob_races(scores_cl, te_keys, te_meta, temp=temp)
    report_calibration(prob_races)
    _res, _nd, day_records = simulate_win5_compare(
        prob_races, upset_map, cov_cl, budgets=[50, 100, 150, 200])
    report_skip_analysis(day_records, budgets=[100, 150, 200])

    # ── LightGBM LambdaRank (オプション比較) ──
    if args.lgbm:
        inner_keys = [k for k, t in zip(tr_keys, inner_tr) if t]
        cal_keys = [k for k, t in zip(tr_keys, cal) if t]
        booster, best_it = fit_lgbm_rank(
            Xz_tr[inner_tr], y[tr][inner_tr], inner_keys,
            Xz_tr[cal], y[tr][cal], cal_keys)
        print(f"\nLightGBM LambdaRank: best_iteration = {best_it}")
        scores_gb = booster.predict(Xz_te, num_iteration=best_it)
        cov_gb, n_gb = coverage_from_scores(scores_gb, te_keys, te_meta, upset_map)
        d_cl = cov_cl.get("default", [0] * 4)
        d_gb = cov_gb.get("default", [0] * 4)
        print("model                      | k=1   k=2   k=3   k=4")
        print(f"{'conditional logit':26s} | " + "  ".join(f"{c*100:4.1f}" for c in d_cl[:4]))
        print(f"{'LightGBM LambdaRank':26s} | " + "  ".join(f"{c*100:4.1f}" for c in d_gb[:4]))
        gain = sorted(zip(FEATURES, booster.feature_importance("gain")), key=lambda x: -x[1])
        print("特徴量重要度(gain上位8):", ", ".join(f"{n}={g:.0f}" for n, g in gain[:8]))
        # 温度 (rankスコアは非確率のためキャリブレーション必須)
        temp_gb = fit_temperature(booster.predict(Xz_tr[cal], num_iteration=best_it),
                                  y[tr][cal], cal_keys,
                                  grid=np.arange(0.2, 3.01, 0.05))
        print(f"LGBM 温度: τ = {temp_gb:.2f}")
        prob_races_gb = build_prob_races(scores_gb, te_keys, te_meta, temp=temp_gb)
        report_calibration(prob_races_gb)
        simulate_win5_compare(prob_races_gb, upset_map, cov_gb, budgets=[50, 100, 150, 200])

    # CL はキャリブレーション済み勝率を提供し、レース固有配分 (WIN5的中率+3pt級) の
    # 前提になるため、LR がカバレッジで 1pt 以上明確に勝る場合のみ LR を採用する
    mean_lr = sum(cov_ml.get("default", [0] * 4)[:4]) / 4
    mean_cl = sum(cov_cl.get("default", [0] * 4)[:4]) / 4
    best_obj = "logistic_regression" if mean_lr - mean_cl > 0.01 else "conditional_logit"
    print(f"\nOOS平均カバレッジ(k1-4): LR {mean_lr*100:.1f}% / CL {mean_cl*100:.1f}% → 採用候補: {best_obj}"
          f" (CLは勝率配分の前提のため1pt差までCL優先)")

    # ── 本番モデル出力 (全期間で再学習) ──
    if args.write:
        scaler_f = StandardScaler().fit(X)
        Xz_f = scaler_f.transform(X)
        if best_obj == "conditional_logit":
            coef_f = fit_conditional_logit(Xz_f, y, race_keys)
        else:
            model_f = LogisticRegression(max_iter=2000, C=1.0)
            model_f.fit(Xz_f, y)
            coef_f = model_f.coef_[0]
        out = {
            "meta": {
                "trained_at": datetime.now().strftime("%Y-%m-%d"),
                "train_period": f"{TRAIN_FROM}-{TEST_TO}",
                "n_samples": int(len(y)),
                "oos_note": f"時系列検証(学習21-24/検証25): CL k1-4 = "
                            f"{[round(c*100,1) for c in cov_cl.get('default', [])[:4]]} / LR "
                            f"{[round(c*100,1) for c in cov_ml.get('default', [])[:4]]} / "
                            f"手調整 {[round(c*100,1) for c in cov_cur.get('default', [])[:4]]}",
                "missing_fill": {"ln_odds": "ln(30)", "prev_rank": 10, "ln_interval": "ln(30)",
                                 "age": 4, "kinryo": 55.0, "weight": 470},
            },
            "objective": best_obj,
            "features": FEATURES,
            "mean": [round(float(v), 6) for v in scaler_f.mean_],
            "sd": [round(float(v), 6) for v in scaler_f.scale_],
            "coef": [round(float(v), 6) for v in coef_f],
            "display_scale": 10.0,
            "prob_temperature": temp,
        }
        with open(MODEL_PATH, "w", encoding="utf-8") as fp:
            json.dump(out, fp, ensure_ascii=False, indent=1)
        print(f"\n[OK] 本番モデル ({best_obj}, 全期間再学習, n={len(y)}) -> {MODEL_PATH}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
