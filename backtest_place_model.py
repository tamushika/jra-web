"""
Web用・オッズなし複勝確率モデルの固定OOS検証。

分割:
  学習 2021-2023 / Platt校正 2024 / 固定テスト 2025 / 追加確認 2026H1

新モデルは backtest_ml.NO_MARKET_FEATURES の20特徴だけを使い、T33公開IFで
各年の前年末までのability統計を適用する。比較対象の現行Webスコアも
backtest_score_compare.py と同じ騎手・枠（複勝率）+能力主成分へ同じ年次
as-of providerを注入する。本番ファイルや画面へは接続しない。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
from backtest_ability import load_runs  # noqa: E402
from backtest_ml import FEATURES, NO_MARKET_FEATURES, feature_matrix  # noqa: E402
from backtest_stats_retrain import build_consistent_feature_dataset  # noqa: E402
from backtest_win5 import load_win5_cfg, score_all_runners  # noqa: E402
from fold_stats import FoldFactorTableProvider, fold_as_of_for  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
MODEL_PATH = os.path.join(BASE_DIR, "web_place_model.json")

DATA_FROM, DATA_TO = "20210101", "20260630"
TRAIN_FROM, TRAIN_TO = "20210101", "20231231"
CALIBRATION_FROM, CALIBRATION_TO = "20240101", "20241231"
TEST_FROM, TEST_TO = "20250101", "20251231"
ADDITIONAL_FROM, ADDITIONAL_TO = "20260101", "20260630"

RELIABILITY_EDGES = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.70, 1.0)
MODEL_LABEL = "新・複勝確率"
CURRENT_LABEL = "現行Web主成分"


def place_cutoff(total_horses):
    """JRA複勝の対象着順。7頭以下は2着、8頭以上は3着まで。"""
    try:
        return 2 if int(total_horses) <= 7 else 3
    except (TypeError, ValueError):
        return 3


def place_target(rank, total_horses):
    try:
        return int(int(rank) <= place_cutoff(total_horses))
    except (TypeError, ValueError):
        return 0


def _sigmoid(values):
    clipped = np.clip(np.asarray(values, dtype=float), -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def fit_platt(scores, targets):
    """2024校正用の1変量Platt scalingを学習する。"""
    from sklearn.linear_model import LogisticRegression

    values = np.asarray(scores, dtype=float).reshape(-1, 1)
    outcomes = np.asarray(targets, dtype=int)
    if len(values) == 0 or len(np.unique(outcomes)) != 2:
        raise ValueError("Platt校正には正例・負例を含むデータが必要です")
    calibrator = LogisticRegression(C=1_000_000.0, max_iter=2000, solver="lbfgs")
    calibrator.fit(values, outcomes)
    return {
        "method": "platt",
        "slope": float(calibrator.coef_[0, 0]),
        "intercept": float(calibrator.intercept_[0]),
    }


def apply_platt(scores, calibration):
    if calibration.get("method") != "platt":
        raise ValueError("未対応の校正方式です")
    logits = (float(calibration["slope"]) * np.asarray(scores, dtype=float)
              + float(calibration["intercept"]))
    return _sigmoid(logits)


def fit_place_model(X, targets, dates,
                    statistics_source="ability_db_yearly_as_of"):
    """2021-23でL2ロジスティック回帰、2024でPlatt校正を学習する。"""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    if "ln_odds" in NO_MARKET_FEATURES:
        raise AssertionError("複勝モデルへ市場特徴を入れてはいけません")
    if set(NO_MARKET_FEATURES) != set(FEATURES) - {"ln_odds"}:
        raise AssertionError("NO_MARKET_FEATURESはln_oddsだけを除外する必要があります")

    matrix = feature_matrix(X, NO_MARKET_FEATURES)
    train = (dates >= TRAIN_FROM) & (dates <= TRAIN_TO)
    calibration = (dates >= CALIBRATION_FROM) & (dates <= CALIBRATION_TO)
    if not train.any() or not calibration.any():
        raise ValueError("学習または校正期間のデータがありません")

    scaler = StandardScaler().fit(matrix[train])
    model = LogisticRegression(C=1.0, max_iter=2000, solver="lbfgs")
    model.fit(scaler.transform(matrix[train]), np.asarray(targets)[train])
    raw_calibration = model.decision_function(scaler.transform(matrix[calibration]))
    platt = fit_platt(raw_calibration, np.asarray(targets)[calibration])

    return {
        "meta": {
            "created_at": datetime.now().strftime("%Y-%m-%d"),
            "purpose": "offline_shadow_evaluation_only",
            "statistics_source": statistics_source,
            "train_period": f"{TRAIN_FROM}-{TRAIN_TO}",
            "calibration_period": f"{CALIBRATION_FROM}-{CALIBRATION_TO}",
            "n_train": int(train.sum()),
            "n_calibration": int(calibration.sum()),
        },
        "objective": "independent_place_logistic_l2",
        "features": list(NO_MARKET_FEATURES),
        "mean": [float(value) for value in scaler.mean_],
        "scale": [float(value) for value in scaler.scale_],
        "coef": [float(value) for value in model.coef_[0]],
        "intercept": float(model.intercept_[0]),
        "calibration": platt,
    }


def predict_place_probability(model, X):
    features = model.get("features")
    if features != NO_MARKET_FEATURES or "ln_odds" in features:
        raise ValueError("保存モデルの非市場特徴量契約が一致しません")
    matrix = feature_matrix(X, features)
    mean = np.asarray(model["mean"], dtype=float)
    scale = np.asarray(model["scale"], dtype=float)
    coef = np.asarray(model["coef"], dtype=float)
    raw_scores = ((matrix - mean) / scale) @ coef + float(model["intercept"])
    return apply_platt(raw_scores, model["calibration"])


def save_model(model, path=MODEL_PATH):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file_obj:
        json.dump(model, file_obj, ensure_ascii=False, indent=2)
        file_obj.write("\n")


def load_model(path=MODEL_PATH):
    with open(path, "r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def load_place_payouts(connection, date_from, date_to):
    """runsを優先し、欠損時はrace_payoutsの複勝払戻で補完する。"""
    payouts = {}
    try:
        rows = connection.execute(
            """SELECT date,place,r,umaban,fukusho_pay FROM runs
               WHERE date >= ? AND date <= ? AND fukusho_pay IS NOT NULL""",
            (date_from, date_to),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for date, place, race_no, horse_no, payout in rows:
        try:
            payouts[(date, place, int(race_no), int(horse_no))] = float(payout)
        except (TypeError, ValueError):
            continue

    try:
        fallback = connection.execute(
            """SELECT date,place,r,combo,pay FROM race_payouts
               WHERE date >= ? AND date <= ? AND bet_type = '複勝'""",
            (date_from, date_to),
        ).fetchall()
    except sqlite3.OperationalError:
        fallback = []
    for date, place, race_no, combo, payout in fallback:
        try:
            key = (date, place, int(race_no), int(str(combo).strip()))
            payouts.setdefault(key, float(payout))
        except (TypeError, ValueError):
            continue
    return payouts


def build_place_feature_dataset(runs, cfg, date_to, db_path=DB_PATH,
                                stats_source="ability"):
    """T33公開IFを通して統計ソースが一貫した20特徴の元datasetを作る。"""
    return build_consistent_feature_dataset(
        runs, cfg, date_to, stats_source=stats_source, db_path=db_path)


def _score_map(races, *, date_from=None, date_to=None):
    score_map = {}
    for key, members in races.items():
        if date_from and key[0] < date_from:
            continue
        if date_to and key[0] > date_to:
            continue
        for score, runner in members:
            try:
                score_map[(key, int(runner.get("umaban")))] = float(score)
            except (TypeError, ValueError):
                continue
    return score_map


def current_web_scores(runs, race_keys, meta, web_cfg, *, db_path=DB_PATH,
                       stats_source="ability"):
    """現行Web主成分を各dataset行へ対応付ける。

    abilityモードは校正・評価各年に前年末snapshotを注入する。currentモードは
    暫定比較を再現するためproduction CSVをそのまま使う。
    """
    if stats_source not in ("ability", "current"):
        raise ValueError("stats_source must be 'ability' or 'current'")
    if stats_source == "current":
        races = score_all_runners(
            runs, CALIBRATION_FROM, web_cfg, rate_key="show_rate",
            factor_table_provider=lambda place, track, distance:
            scoring.load_legacy_factor_table(
                place, track, distance, API_DIR),
        )
        score_map = _score_map(races)
    else:
        score_map = {}
        for year in range(int(CALIBRATION_FROM[:4]), int(DATA_TO[:4]) + 1):
            year_from = f"{year:04d}0101"
            year_to = min(f"{year:04d}1231", DATA_TO)
            as_of = fold_as_of_for(year_from)
            provider = FoldFactorTableProvider(
                db_path, as_of,
                pedigree_cache_path=os.path.join(BASE_DIR, "pedigree_cache.json"),
                legacy_api_dir=API_DIR,
            )
            races = score_all_runners(
                runs, year_from, web_cfg, rate_key="show_rate",
                factor_table_provider=provider,
            )
            score_map.update(_score_map(
                races, date_from=year_from, date_to=year_to))

    values = []
    for key, runner in zip(race_keys, meta):
        try:
            horse_no = int(runner.get("umaban"))
        except (TypeError, ValueError):
            horse_no = -1
        values.append(score_map.get((key, horse_no), math.nan))
    return np.asarray(values, dtype=float)


def build_evaluation_races(model_probabilities, current_probabilities,
                            current_scores, race_keys, meta, payouts,
                            date_from, date_to):
    """全馬の特徴・両スコアが揃うレースだけを同一比較母集団にする。"""
    grouped = defaultdict(list)
    for model_probability, current_probability, current_score, key, runner in zip(
            model_probabilities, current_probabilities, current_scores, race_keys, meta):
        if date_from <= key[0] <= date_to:
            grouped[key].append({
                "model_probability": float(model_probability),
                "current_probability": float(current_probability),
                "current_score": float(current_score),
                "runner": runner,
            })

    races = []
    skipped = defaultdict(int)
    for key in sorted(grouped):
        members = grouped[key]
        field_sizes = []
        for member in members:
            try:
                field_sizes.append(int(member["runner"].get("total_horses") or 0))
            except (TypeError, ValueError):
                pass
        field_size = max(field_sizes, default=0)
        if not field_size or len(members) != field_size:
            skipped["incomplete_features"] += 1
            continue
        if any(not math.isfinite(member["current_score"]) for member in members):
            skipped["missing_current_score"] += 1
            continue

        for member in members:
            runner = member["runner"]
            member["target"] = place_target(runner.get("rank"), field_size)
            try:
                horse_no = int(runner.get("umaban"))
            except (TypeError, ValueError):
                horse_no = -1
            member["horse_no"] = horse_no
            member["payout"] = payouts.get((key[0], key[1], int(key[2]), horse_no))
            try:
                member["popularity"] = int(runner.get("popularity"))
            except (TypeError, ValueError):
                member["popularity"] = None

        model_order = sorted(
            range(len(members)),
            key=lambda index: (-members[index]["model_probability"],
                               members[index]["horse_no"]),
        )
        current_order = sorted(
            range(len(members)),
            key=lambda index: (-members[index]["current_score"],
                               members[index]["horse_no"]),
        )
        races.append({
            "key": key,
            "members": members,
            "model_order": model_order,
            "current_order": current_order,
        })
    return races, dict(skipped)


def evaluate_method(races, order_key, probability_key, ks=(1, 3, 5)):
    positives = sum(member["target"] for race in races for member in race["members"])
    captures = []
    for k in ks:
        captured = sum(
            race["members"][index]["target"]
            for race in races for index in race[order_key][:k]
        )
        captures.append(captured / positives if positives else 0.0)

    probabilities = np.asarray([
        member[probability_key] for race in races for member in race["members"]
    ], dtype=float)
    outcomes = np.asarray([
        member["target"] for race in races for member in race["members"]
    ], dtype=float)
    return {
        "captures": captures,
        "brier": float(np.mean(np.square(outcomes - probabilities))),
        "horses": len(outcomes),
        "positives": int(outcomes.sum()),
    }


def reliability_rows(races, probability_key, edges=RELIABILITY_EDGES):
    values = [
        (member[probability_key], member["target"])
        for race in races for member in race["members"]
    ]
    rows = []
    for lower, upper in zip(edges, edges[1:]):
        selected = [
            value for value in values
            if lower <= value[0] < upper or (upper == 1.0 and value[0] == 1.0)
        ]
        rows.append({
            "lower": lower,
            "upper": upper,
            "n": len(selected),
            "predicted": (sum(value[0] for value in selected) / len(selected)
                          if selected else None),
            "actual": (sum(value[1] for value in selected) / len(selected)
                       if selected else None),
        })
    return rows


def popularity_band(popularity):
    if popularity is None:
        return "人気不明"
    if popularity <= 3:
        return "1-3人気"
    if popularity <= 8:
        return "4-8人気"
    return "9人気以下"


def selected_popularity_report(races, order_key, top_k=5):
    """各レース上位k頭を、評価時だけ人気帯に分けて複勝率・回収率を出す。"""
    stats = defaultdict(lambda: {
        "n": 0, "places": 0, "return": 0.0, "missing_payout": 0,
    })
    for race in races:
        for index in race[order_key][:top_k]:
            member = race["members"][index]
            bucket = stats[popularity_band(member["popularity"])]
            bucket["n"] += 1
            if member["target"]:
                bucket["places"] += 1
                if member["payout"] is None:
                    bucket["missing_payout"] += 1
                else:
                    bucket["return"] += float(member["payout"])
    for values in stats.values():
        values["place_rate"] = values["places"] / values["n"] if values["n"] else 0.0
        values["place_roi"] = values["return"] / values["n"] if values["n"] else 0.0
    return dict(stats)


def print_reliability(label, rows):
    print(f"\n  [{label}] 確率帯 | n      | 予測複勝率 | 実複勝率")
    for row in rows:
        if not row["n"]:
            continue
        print(f"  {100*row['lower']:4.0f}-{100*row['upper']:3.0f}% | "
              f"{row['n']:6d} | {100*row['predicted']:9.2f}% | "
              f"{100*row['actual']:8.2f}%")


def print_period_report(title, races, skipped):
    print(f"\n===== {title}: Web複勝モデル 同一集団比較 =====")
    if not races:
        print("対象レースなし")
        return
    print(f"対象 {len(races)}レース / {sum(len(r['members']) for r in races)}出走"
          f" / 除外 {sum(skipped.values())}レース {skipped}")
    print("手法                     | 上位1捕捉 | 上位3捕捉 | 上位5捕捉 | Brier")
    methods = (
        (CURRENT_LABEL, "current_order", "current_probability"),
        (MODEL_LABEL, "model_order", "model_probability"),
    )
    for label, order_key, probability_key in methods:
        result = evaluate_method(races, order_key, probability_key)
        captures = result["captures"]
        print(f"{label:24s} | {100*captures[0]:9.2f}% | {100*captures[1]:9.2f}% | "
              f"{100*captures[2]:9.2f}% | {result['brier']:.8f}")
        print_reliability(label, reliability_rows(races, probability_key))

    print("\n各手法のレース上位5頭: 人気帯別の複勝率・複勝回収率")
    print("手法                     | 人気帯   | n      | 複勝率  | 複勝回収 | 配当欠損")
    for label, order_key, _probability_key in methods:
        band_rows = selected_popularity_report(races, order_key)
        for band in ("1-3人気", "4-8人気", "9人気以下", "人気不明"):
            values = band_rows.get(band)
            if not values:
                continue
            print(f"{label:24s} | {band:7s} | {values['n']:6d} | "
                  f"{100*values['place_rate']:6.1f}% | {values['place_roi']:7.1f}% | "
                  f"{values['missing_payout']:4d}")


def main():
    parser = argparse.ArgumentParser(description="Web用オッズなし複勝確率モデルの固定OOS")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--model-out", default=MODEL_PATH,
                        help="スクラッチモデルJSONの出力先 (本番ディレクトリは禁止)")
    parser.add_argument("--stats-source", choices=("ability", "current"),
                        default="ability", help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        output_path = validate_model_output_path(args.model_out)
    except ValueError as exc:
        parser.error(str(exc))

    cfg = load_win5_cfg()
    web_cfg = scoring.load_score_weights(API_DIR)
    with sqlite3.connect(args.db) as connection:
        runs = load_runs(connection, DATA_FROM, DATA_TO)
        payouts = load_place_payouts(connection, TEST_FROM, ADDITIONAL_TO)
    print(f"ロード: {len(runs)}行 / 複勝払戻: {len(payouts)}頭")
    if args.stats_source == "ability":
        statistics_label = "ability_db_yearly_as_of"
        print("統計ソース: ability.db 年次as-of (T33公開IF、各行の前年末まで)")
    else:
        statistics_label = "current_db_keiba_production_csv_control"
        print("統計ソース: 現行Web production CSV (暫定対照)")

    X, _winner_y, race_keys, meta = build_place_feature_dataset(
        runs, cfg, DATA_TO, db_path=args.db, stats_source=args.stats_source)
    dates = np.asarray([key[0] for key in race_keys])
    targets = np.asarray([
        place_target(runner.get("rank"), runner.get("total_horses"))
        for runner in meta
    ], dtype=int)
    model = fit_place_model(
        X, targets, dates, statistics_source=statistics_label)
    save_model(model, output_path)
    model = load_model(output_path)
    model_probabilities = predict_place_probability(model, X)

    current_scores = current_web_scores(
        runs, race_keys, meta, web_cfg, db_path=args.db,
        stats_source=args.stats_source)
    calibration = ((dates >= CALIBRATION_FROM) & (dates <= CALIBRATION_TO)
                   & np.isfinite(current_scores))
    current_platt = fit_platt(current_scores[calibration], targets[calibration])
    current_probabilities = np.full(len(current_scores), np.nan, dtype=float)
    finite = np.isfinite(current_scores)
    current_probabilities[finite] = apply_platt(current_scores[finite], current_platt)

    print(f"モデル保存: {output_path} (scratch / 本番未接続)")
    print(f"分割: 学習 {model['meta']['n_train']}走 (2021-23) / "
          f"校正 {model['meta']['n_calibration']}走 (2024)")
    print("特徴量: 20 (ln_odds・人気を不使用)")

    for title, date_from, date_to in (
            ("固定テスト 2025", TEST_FROM, TEST_TO),
            ("追加確認 2026H1", ADDITIONAL_FROM, ADDITIONAL_TO)):
        races, skipped = build_evaluation_races(
            model_probabilities, current_probabilities, current_scores,
            race_keys, meta, payouts, date_from, date_to,
        )
        print_period_report(title, races, skipped)


def validate_model_output_path(path):
    """Allow only the named scratch artifact and never anywhere under api/."""
    output_path = os.path.abspath(os.fspath(path))
    if os.path.basename(output_path).casefold() != "web_place_model.json":
        raise ValueError("出力ファイル名は web_place_model.json に限定されます")
    production_root = os.path.abspath(API_DIR)
    try:
        inside_api = os.path.commonpath(
            (production_root, output_path)) == production_root
    except ValueError:  # Different Windows drives cannot share a common path.
        inside_api = False
    if inside_api:
        raise ValueError("api配下にはスクラッチモデルを出力できません")
    return output_path


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()

