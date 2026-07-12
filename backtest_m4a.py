"""
M4-A: 市場確率の単調校正バックテスト
========================================

確定単勝オッズから作る市場確率の順位を変えず、確率値だけを校正する。
学習 (2021-2023)、調整 (2024)、固定テスト (2025)、追加確認 (2026H1)
は混在させない。本スクリプトは校正器と評価成果物を作るだけで、本番コードには接続しない。
"""

import argparse
import bisect
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from backtest_ability import load_runs
from backtest_win5 import parse_final_odds


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "ability.db")
OUTPUT_PATH = os.path.join(
    BASE_DIR, "api", "data_files", "common", "m4a_calibration.json"
)

DATA_FROM = "20210101"
DATA_TO = "20260630"
TRAIN_FROM = "20210101"
TRAIN_TO = "20231231"
TUNE_FROM = "20240101"
TUNE_TO = "20241231"
TEST_FROM = "20250101"
TEST_TO = "20251231"
CHECK_FROM = "20260101"
CHECK_TO = "20260630"

MIN_BIN_SIZE = 2_000
MAX_BINS = 20
RANK_EPSILON = 1e-8
PROBABILITY_FLOOR = 1e-12
RELIABILITY_EDGES = (0.0, 0.02, 0.05, 0.10, 0.20, 0.35, 1.0)

VARIANTS = ("V0", "V1", "V2", "V3")
VARIANT_LABELS = {
    "V0": "全体一律",
    "V1": "芝/ダート",
    "V2": "芝/ダート×頭数帯",
    "V3": "芝/ダート×クラス帯",
}


@dataclass(frozen=True)
class MarketRace:
    """1レース分の市場確率と結果。"""

    key: str
    race_date: str
    surface: str
    race_class: str
    probabilities: tuple
    winner_index: int

    @property
    def field_size(self):
        return len(self.probabilities)


@dataclass
class CalibrationMap:
    """log(市場確率)から校正スコアへの区分線形な単調写像。"""

    bin_edges: list
    x_points: list
    corrected_scores: list
    corrected_probabilities: list
    counts: list
    raw_probability_means: list
    observed_win_rates: list
    rank_epsilon: float = RANK_EPSILON

    def __post_init__(self):
        lengths = {
            len(self.x_points),
            len(self.corrected_scores),
            len(self.corrected_probabilities),
            len(self.counts),
            len(self.raw_probability_means),
            len(self.observed_win_rates),
        }
        if len(lengths) != 1 or not self.x_points:
            raise ValueError("校正マップの配列長が不正です")
        if any(a >= b for a, b in zip(self.x_points, self.x_points[1:])):
            raise ValueError("x_points は厳密な昇順である必要があります")
        if any(a > b + 1e-15 for a, b in zip(
                self.corrected_scores, self.corrected_scores[1:])):
            raise ValueError("校正スコアが単調増加ではありません")

    def score(self, log_probability):
        """区分線形補間した校正スコアを返す。極小傾斜で順位を厳密に保つ。"""
        x = float(log_probability)
        if len(self.x_points) == 1:
            base = self.corrected_scores[0]
        elif x <= self.x_points[0]:
            base = self.corrected_scores[0]
        elif x >= self.x_points[-1]:
            base = self.corrected_scores[-1]
        else:
            right = bisect.bisect_right(self.x_points, x)
            left = right - 1
            x0, x1 = self.x_points[left], self.x_points[right]
            y0, y1 = self.corrected_scores[left], self.corrected_scores[right]
            ratio = (x - x0) / (x1 - x0)
            base = y0 + ratio * (y1 - y0)
        return base + self.rank_epsilon * x

    def to_dict(self):
        return {
            "bin_edges": self.bin_edges,
            "x_points": self.x_points,
            "corrected_scores": self.corrected_scores,
            "corrected_probabilities": self.corrected_probabilities,
            "counts": self.counts,
            "raw_probability_means": self.raw_probability_means,
            "observed_win_rates": self.observed_win_rates,
            "rank_epsilon": self.rank_epsilon,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            bin_edges=list(data["bin_edges"]),
            x_points=list(data["x_points"]),
            corrected_scores=list(data["corrected_scores"]),
            corrected_probabilities=list(data["corrected_probabilities"]),
            counts=list(data["counts"]),
            raw_probability_means=list(data["raw_probability_means"]),
            observed_win_rates=list(data["observed_win_rates"]),
            rank_epsilon=float(data.get("rank_epsilon", RANK_EPSILON)),
        )


@dataclass
class CalibrationModel:
    """選択したセグメント粒度と、その校正マップ一式。"""

    variant: str
    global_map: CalibrationMap
    segment_maps: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.variant not in VARIANTS:
            raise ValueError(f"未知のバリアントです: {self.variant}")

    def map_for(self, race):
        key = segment_key(self.variant, race)
        return self.segment_maps.get(key, self.global_map)

    def to_dict(self):
        return {
            "schema_version": 1,
            "model": "M4-A",
            "method": "binned_empirical_rate_pava",
            "variant": self.variant,
            "variant_label": VARIANT_LABELS[self.variant],
            "rank_preserving": True,
            "global_map": self.global_map.to_dict(),
            "segment_maps": {
                key: value.to_dict()
                for key, value in sorted(self.segment_maps.items())
            },
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            variant=data["variant"],
            global_map=CalibrationMap.from_dict(data["global_map"]),
            segment_maps={
                key: CalibrationMap.from_dict(value)
                for key, value in data.get("segment_maps", {}).items()
            },
            metadata=dict(data.get("metadata", {})),
        )


def _quantile_slices(rows, min_bin_size, max_bins):
    """同じxを分断せず、原則min_bin_size以上になる量子化ビンを返す。"""
    n = len(rows)
    n_bins = min(max_bins, max(1, n // min_bin_size))
    if n_bins == 1:
        return [rows]

    cuts = []
    previous = 0
    for k in range(1, n_bins):
        cut = round(k * n / n_bins)
        cut = max(cut, previous + min_bin_size)
        while cut < n and rows[cut - 1][0] == rows[cut][0]:
            cut += 1
        if n - cut < min_bin_size:
            break
        cuts.append(cut)
        previous = cut

    slices = []
    start = 0
    for cut in cuts:
        slices.append(rows[start:cut])
        start = cut
    slices.append(rows[start:])
    return slices


def _pava(values, weights):
    """重み付きPAVAで非減少列へ射影する。"""
    blocks = []
    for index, (value, weight) in enumerate(zip(values, weights)):
        blocks.append({
            "start": index,
            "end": index,
            "weight": float(weight),
            "sum": float(value) * float(weight),
        })
        while len(blocks) >= 2:
            left, right = blocks[-2], blocks[-1]
            left_value = left["sum"] / left["weight"]
            right_value = right["sum"] / right["weight"]
            if left_value <= right_value:
                break
            blocks[-2:] = [{
                "start": left["start"],
                "end": right["end"],
                "weight": left["weight"] + right["weight"],
                "sum": left["sum"] + right["sum"],
            }]

    fitted = [0.0] * len(values)
    for block in blocks:
        value = block["sum"] / block["weight"]
        for index in range(block["start"], block["end"] + 1):
            fitted[index] = value
    return fitted


def fit_calibration_map(probabilities, outcomes, min_bin_size=MIN_BIN_SIZE,
                        max_bins=MAX_BINS):
    """市場確率と勝敗から、ビン別実測勝率を使う単調校正マップを学習する。"""
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("probabilities/outcomes の長さが不正です")
    if min_bin_size < 1 or max_bins < 1:
        raise ValueError("min_bin_size/max_bins は1以上である必要があります")

    rows = []
    for probability, outcome in zip(probabilities, outcomes):
        probability = float(probability)
        if not 0.0 < probability <= 1.0:
            raise ValueError(f"確率が範囲外です: {probability}")
        rows.append((math.log(probability), probability, int(bool(outcome))))
    rows.sort(key=lambda row: row[0])

    bins = _quantile_slices(rows, min_bin_size, max_bins)
    x_points = []
    raw_means = []
    observed_rates = []
    shrunk_rates = []
    effective_weights = []
    counts = []
    bin_edges = [bins[0][0][0]]

    for index, chunk in enumerate(bins):
        count = len(chunk)
        wins = sum(row[2] for row in chunk)
        x_mean = sum(row[0] for row in chunk) / count
        raw_mean = sum(row[1] for row in chunk) / count
        observed = wins / count

        # 小ビンは市場確率側へ収縮し、少数標本の極端値を避ける。
        prior_count = max(0, min_bin_size - count)
        shrunk = (wins + prior_count * raw_mean) / (count + prior_count)

        x_points.append(x_mean)
        raw_means.append(raw_mean)
        observed_rates.append(observed)
        shrunk_rates.append(shrunk)
        effective_weights.append(count + prior_count)
        counts.append(count)
        if index:
            bin_edges.append((bins[index - 1][-1][0] + chunk[0][0]) / 2.0)
    bin_edges.append(bins[-1][-1][0])

    monotone_rates = _pava(shrunk_rates, effective_weights)
    monotone_rates = [min(1.0 - PROBABILITY_FLOOR,
                          max(PROBABILITY_FLOOR, value))
                      for value in monotone_rates]
    corrected_scores = [math.log(value) for value in monotone_rates]

    return CalibrationMap(
        bin_edges=bin_edges,
        x_points=x_points,
        corrected_scores=corrected_scores,
        corrected_probabilities=monotone_rates,
        counts=counts,
        raw_probability_means=raw_means,
        observed_win_rates=observed_rates,
    )


def _stable_probability_order(probabilities):
    return sorted(range(len(probabilities)),
                  key=lambda index: (-probabilities[index], index))


def assert_same_rank(raw_probabilities, calibrated_probabilities):
    """人気順位が校正前後で同一であることを保証する。"""
    raw_order = _stable_probability_order(raw_probabilities)
    calibrated_order = _stable_probability_order(calibrated_probabilities)
    assert raw_order == calibrated_order, (
        f"校正により人気順位が変化しました: raw={raw_order}, calibrated={calibrated_order}"
    )


def calibrate_probabilities(probabilities, calibration_map):
    """校正スコアをレース内softmaxし、順位と確率和を検証する。"""
    if not probabilities:
        raise ValueError("空のレースは校正できません")
    scores = [calibration_map.score(math.log(float(probability)))
              for probability in probabilities]
    maximum = max(scores)
    exponentials = [math.exp(score - maximum) for score in scores]
    denominator = sum(exponentials)
    calibrated = [value / denominator for value in exponentials]
    assert math.isclose(sum(calibrated), 1.0, rel_tol=0.0, abs_tol=1e-12)
    assert_same_rank(probabilities, calibrated)
    return calibrated


def field_band(field_size):
    if field_size <= 10:
        return "<=10"
    if field_size <= 14:
        return "11-14"
    return ">=15"


def class_band(race_class):
    if race_class in ("新馬", "未勝利"):
        return "debut_maiden"
    if race_class in ("1勝クラス", "2勝クラス", "3勝クラス"):
        return "condition"
    if race_class in ("オープン", "重賞"):
        return "open_graded"
    return "other"


def segment_key(variant, race):
    if variant == "V0":
        return "all"
    if variant == "V1":
        return race.surface
    if variant == "V2":
        return f"{race.surface}|field:{field_band(race.field_size)}"
    if variant == "V3":
        return f"{race.surface}|class:{class_band(race.race_class)}"
    raise ValueError(f"未知のバリアントです: {variant}")


def _flatten_races(races):
    probabilities = []
    outcomes = []
    for race in races:
        probabilities.extend(race.probabilities)
        outcomes.extend(
            1 if index == race.winner_index else 0
            for index in range(race.field_size)
        )
    return probabilities, outcomes


def fit_variant(races, variant, min_bin_size=MIN_BIN_SIZE, max_bins=MAX_BINS):
    """学習期間だけを使って指定バリアントの校正器を作る。"""
    probabilities, outcomes = _flatten_races(races)
    global_map = fit_calibration_map(
        probabilities, outcomes, min_bin_size=min_bin_size, max_bins=max_bins
    )

    grouped = defaultdict(list)
    if variant != "V0":
        for race in races:
            grouped[segment_key(variant, race)].append(race)

    segment_maps = {}
    segment_counts = {}
    for key, segment_races in grouped.items():
        segment_probabilities, segment_outcomes = _flatten_races(segment_races)
        segment_counts[key] = len(segment_probabilities)
        if len(segment_probabilities) < min_bin_size:
            continue
        segment_maps[key] = fit_calibration_map(
            segment_probabilities,
            segment_outcomes,
            min_bin_size=min_bin_size,
            max_bins=max_bins,
        )

    return CalibrationModel(
        variant=variant,
        global_map=global_map,
        segment_maps=segment_maps,
        metadata={"segment_sample_counts": dict(sorted(segment_counts.items()))},
    )


def _reliability_rows(probabilities, outcomes):
    rows = []
    for lower, upper in zip(RELIABILITY_EDGES, RELIABILITY_EDGES[1:]):
        selected = [
            (probability, outcome)
            for probability, outcome in zip(probabilities, outcomes)
            if lower <= probability < upper
            or (upper == 1.0 and probability == 1.0)
        ]
        count = len(selected)
        rows.append({
            "lower": lower,
            "upper": upper,
            "count": count,
            "predicted_mean": (
                sum(value[0] for value in selected) / count if count else None
            ),
            "observed_rate": (
                sum(value[1] for value in selected) / count if count else None
            ),
        })
    return rows


def evaluate(races, model=None):
    """レースLog Loss、馬単位Brier、信頼性表を計算する。"""
    if not races:
        raise ValueError("評価対象レースがありません")
    log_loss_sum = 0.0
    brier_sum = 0.0
    all_probabilities = []
    all_outcomes = []

    for race in races:
        probabilities = list(race.probabilities)
        if model is not None:
            probabilities = calibrate_probabilities(
                probabilities, model.map_for(race)
            )
        log_loss_sum -= math.log(max(PROBABILITY_FLOOR,
                                     probabilities[race.winner_index]))
        for index, probability in enumerate(probabilities):
            outcome = 1 if index == race.winner_index else 0
            brier_sum += (outcome - probability) ** 2
            all_probabilities.append(probability)
            all_outcomes.append(outcome)

    return {
        "races": len(races),
        "horses": len(all_probabilities),
        "log_loss": log_loss_sum / len(races),
        "brier": brier_sum / len(all_probabilities),
        "reliability": _reliability_rows(all_probabilities, all_outcomes),
    }


def save_calibration_model(model, path):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file_obj:
        json.dump(model.to_dict(), file_obj, ensure_ascii=False, indent=2)
        file_obj.write("\n")


def load_calibration_model(path):
    with open(path, "r", encoding="utf-8") as file_obj:
        return CalibrationModel.from_dict(json.load(file_obj))


def load_market_races(db_path=DB_PATH, date_from=DATA_FROM, date_to=DATA_TO):
    """ability.dbから、全馬の確定オッズが揃う8頭以上の平地レースを作る。"""
    with sqlite3.connect(db_path) as connection:
        runs = load_runs(connection, date_from, date_to)

    grouped = defaultdict(list)
    for run in runs:
        if date_from <= run["date"] <= date_to:
            grouped[(run["date"], run["place"], run["r"])].append(run)

    races = []
    skipped = defaultdict(int)
    for (race_date, place, race_number), members in sorted(grouped.items()):
        surfaces = {str(member.get("track_type") or "").strip() for member in members}
        if len(surfaces) != 1 or next(iter(surfaces)) not in ("芝", "ダート"):
            skipped["not_flat"] += 1
            continue
        if len(members) < 8:
            skipped["under_8"] += 1
            continue
        winner_indices = [
            index for index, member in enumerate(members) if member.get("rank") == 1
        ]
        if len(winner_indices) != 1:
            skipped["winner_not_unique"] += 1
            continue

        odds = [
            parse_final_odds(member.get("win_pay"), member.get("rank"))
            for member in members
        ]
        if any(value is None or value <= 0.0 for value in odds):
            skipped["missing_odds"] += 1
            continue
        inverse_odds = [1.0 / value for value in odds]
        total_inverse = sum(inverse_odds)
        probabilities = tuple(value / total_inverse for value in inverse_odds)
        assert math.isclose(sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-12)

        first = members[0]
        races.append(MarketRace(
            key=f"{race_date}-{place}-{race_number}",
            race_date=race_date,
            surface=next(iter(surfaces)),
            race_class=str(first.get("race_class") or ""),
            probabilities=probabilities,
            winner_index=winner_indices[0],
        ))
    return races, dict(sorted(skipped.items()))


def split_races(races):
    periods = {
        "train_2021_2023": [],
        "tune_2024": [],
        "test_2025": [],
        "check_2026H1": [],
    }
    for race in races:
        if TRAIN_FROM <= race.race_date <= TRAIN_TO:
            periods["train_2021_2023"].append(race)
        elif TUNE_FROM <= race.race_date <= TUNE_TO:
            periods["tune_2024"].append(race)
        elif TEST_FROM <= race.race_date <= TEST_TO:
            periods["test_2025"].append(race)
        elif CHECK_FROM <= race.race_date <= CHECK_TO:
            periods["check_2026H1"].append(race)
    if any(not value for value in periods.values()):
        empty = [key for key, value in periods.items() if not value]
        raise RuntimeError(f"空の期間があります: {', '.join(empty)}")
    return periods


def _print_metric_table(period_metrics):
    print("\n===== 期間×手法 指標比較 =====")
    print("期間             | 手法       | レース | 出走頭 | Log Loss | Brier     | ΔLogLoss  | ΔBrier")
    for period_name, metrics in period_metrics.items():
        raw = metrics["raw"]
        for method in ("raw", "calibrated"):
            row = metrics[method]
            print(
                f"{period_name:16s} | {method:10s} | {row['races']:6d} | "
                f"{row['horses']:6d} | {row['log_loss']:.6f} | {row['brier']:.8f} | "
                f"{row['log_loss'] - raw['log_loss']:+.6f} | "
                f"{row['brier'] - raw['brier']:+.8f}"
            )


def _format_bin(lower, upper):
    return f"[{100 * lower:>2.0f},{100 * upper:>3.0f}{']' if upper == 1.0 else ')'}%"


def _print_reliability(period_name, raw, calibrated):
    print(f"\n===== 信頼性テーブル: {period_name} =====")
    print("確率帯       | 手法       | 予測平均 | 実勝率   | 件数")
    for raw_row, calibrated_row in zip(raw["reliability"],
                                       calibrated["reliability"]):
        for method, row in (("raw", raw_row), ("calibrated", calibrated_row)):
            predicted = "-" if row["predicted_mean"] is None else f"{100 * row['predicted_mean']:7.3f}%"
            observed = "-" if row["observed_rate"] is None else f"{100 * row['observed_rate']:7.3f}%"
            print(
                f"{_format_bin(row['lower'], row['upper']):12s} | {method:10s} | "
                f"{predicted:>8s} | {observed:>8s} | {row['count']:6d}"
            )


def run_backtest(db_path=DB_PATH, output_path=OUTPUT_PATH):
    print(f"データ: {db_path}")
    races, skipped = load_market_races(db_path)
    periods = split_races(races)
    print(f"対象: {len(races):,}レース / {sum(r.field_size for r in races):,}出走")
    print(f"除外: {skipped}")
    for key, value in periods.items():
        print(f"  {key}: {len(value):,}レース / "
              f"{sum(race.field_size for race in value):,}出走")

    train_races = periods["train_2021_2023"]
    tune_races = periods["tune_2024"]
    models = {}
    tune_results = {}
    raw_tune = evaluate(tune_races)

    print("\n===== 2024調整データによるバリアント選択 =====")
    print("候補 | セグメント                  | 2024 Log Loss | raw差")
    for variant in VARIANTS:
        model = fit_variant(train_races, variant)
        result = evaluate(tune_races, model)
        models[variant] = model
        tune_results[variant] = result
        print(
            f"{variant:4s} | {VARIANT_LABELS[variant]:28s} | "
            f"{result['log_loss']:.6f} | {result['log_loss'] - raw_tune['log_loss']:+.6f}"
        )

    # 同値なら複雑度の低いV0→V3を優先する。
    chosen_variant = min(VARIANTS, key=lambda item: tune_results[item]["log_loss"])
    chosen_model = models[chosen_variant]
    print(
        f"選択: {chosen_variant} ({VARIANT_LABELS[chosen_variant]}) — "
        f"2024 Log Loss最小 {tune_results[chosen_variant]['log_loss']:.6f} "
        "（同値時は低複雑度を優先）"
    )

    period_metrics = {}
    for period_name, period_races in periods.items():
        raw = raw_tune if period_name == "tune_2024" else evaluate(period_races)
        calibrated = (
            tune_results[chosen_variant]
            if period_name == "tune_2024"
            else evaluate(period_races, chosen_model)
        )
        period_metrics[period_name] = {"raw": raw, "calibrated": calibrated}

    _print_metric_table(period_metrics)
    for period_name, metrics in period_metrics.items():
        _print_reliability(period_name, metrics["raw"], metrics["calibrated"])

    chosen_model.metadata.update({
        "created_on": date.today().isoformat(),
        "data_period": {"from": "2021-01-01", "to": "2026-06-30"},
        "training_period": {"from": "2021-01-01", "to": "2023-12-31"},
        "tuning_period": {"from": "2024-01-01", "to": "2024-12-31"},
        "fixed_test_period": {"from": "2025-01-01", "to": "2025-12-31"},
        "additional_check_period": {"from": "2026-01-01", "to": "2026-06-30"},
        "selection_metric": "2024 race log loss",
        "selection_results": {
            variant: {
                "log_loss": tune_results[variant]["log_loss"],
                "brier": tune_results[variant]["brier"],
            }
            for variant in VARIANTS
        },
        "min_bin_size": MIN_BIN_SIZE,
        "max_bins": MAX_BINS,
        "reliability_edges": list(RELIABILITY_EDGES),
        "evaluation": {
            period_name: {
                method: {
                    "races": result["races"],
                    "horses": result["horses"],
                    "log_loss": result["log_loss"],
                    "brier": result["brier"],
                    "reliability": result["reliability"],
                }
                for method, result in metrics.items()
            }
            for period_name, metrics in period_metrics.items()
        },
        "production_connected": False,
    })
    save_calibration_model(chosen_model, output_path)
    print(f"\n校正マップ保存: {output_path}")
    print("本番コードには接続していません。採否は上位モデルのレビュー対象です。")
    return chosen_model, period_metrics


def main():
    parser = argparse.ArgumentParser(description="M4-A 市場確率の単調校正バックテスト")
    parser.add_argument("--db", default=DB_PATH, help="ability.db のパス")
    parser.add_argument("--output", default=OUTPUT_PATH, help="校正JSONの保存先")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run_backtest(args.db, args.output)


if __name__ == "__main__":
    main()
