"""T65: 条件セル監査 — 特殊条件下の市場効率の系統検証 (反証フィルタ).

SPEC: docs/codex/SPEC-T65-condition-cell-audit.md
登録: T39台帳 `T65-condition-cell-audit-v1` (セル・family・Δ_min・ゲート凍結)。

事前期待は「通過family 0 = 反証の成功」。d = market勝馬LL − model勝馬LL (正=モデル優位)。
モデルはT62と同一のbacktest CL機構 (2024評価=2021-23学習、2025/2026H1評価=2021-24 refit)。

母集団に関する凍結解釈 (実行前に確定・データ閲覧後の変更禁止):
台帳の明示列挙「平地・8頭以上・全馬オッズあり」を採用する。T62歴史層の9R以降制約は
含めない — C6/C7 (未勝利・新馬) は午前レース中心で、9R制約は討議で合意した
セル実測件数 (C6=306R/年等) と矛盾するため。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backtest_ability import load_runs  # noqa: E402
from backtest_t62_race_selection import (  # noqa: E402
    ABILITY_DB_SHA256, DATA_TO, EPS, TRAIN_FROM,
    _fit_base, _mask, _runner_sort_key, _score_base, _selected, _softmax,
    build_strict_feature_dataset, canonical_sha256, fit_temperature,
    rolling_origin_contract, sha256_file, validate_rolling_origin_contract,
)
from backtest_win5 import load_win5_cfg, parse_final_odds  # noqa: E402
from eval.ledger import load_ledger  # noqa: E402

EXPERIMENT_ID = "T65-condition-cell-audit-v1"
DEFAULT_DB = ROOT / "ability.db"
DEFAULT_LEDGER = ROOT / "eval" / "experiments.jsonl"
DEFAULT_SPEC = ROOT / "docs" / "codex" / "SPEC-T65-condition-cell-audit.md"
DEFAULT_OUTPUT = ROOT / "outputs" / "t65_result.json"
DELTA_MIN = 0.0042805
ALPHA = 0.05  # 片側・maxT補正後 (実行前固定)
RESAMPLES = 2_000
SEED = 6501
LOCAL_PLACES = frozenset({"札幌", "函館", "福島", "新潟", "小倉"})
WET = frozenset({"重", "不"})
CLASSES_123 = frozenset({"1勝クラス", "2勝クラス", "3勝クラス"})
FAMILIES = {
    "道悪": ("C1", "C2"),
    "経験浅": ("C6", "C7"),
    "ローカル/価格形成": ("C3", "C4", "C5"),
}
PERIODS = ("2024", "2025", "2026H1")


class T65Error(RuntimeError):
    pass


def require_registration(
    *, ledger_path: Path = DEFAULT_LEDGER, db_path: Path = DEFAULT_DB,
    spec_path: Path = DEFAULT_SPEC,
) -> dict:
    rows = [row for row in load_ledger(ledger_path)
            if row["experiment_id"] == EXPERIMENT_ID]
    if len(rows) != 1:
        raise T65Error(f"missing or duplicate ledger row: {EXPERIMENT_ID}")
    row = rows[0]
    failures = []
    grid = row["search_grid"]
    if abs(float(grid.get("delta_min", -1)) - DELTA_MIN) > 1e-12:
        failures.append("delta_min")
    families = {key: tuple(value) for key, value in grid["families"].items()}
    if families != FAMILIES:
        failures.append("families")
    if set(grid["cells"]) != {"C1", "C2", "C3", "C4", "C5", "C6", "C7"}:
        failures.append("cells")
    hashes = row.get("data_hashes", {})
    if hashes.get("ability_db_sha256") != ABILITY_DB_SHA256:
        failures.append("registered_ability_sha")
    if sha256_file(db_path) != ABILITY_DB_SHA256:
        failures.append("ability_db_changed")
    if hashes.get("spec_sha256") != hashlib.sha256(
        spec_path.read_bytes()
    ).hexdigest():
        failures.append("spec_changed")
    if failures:
        raise T65Error("T65 frozen contract mismatch: " + ", ".join(failures))
    return row


# ── セル判定 (凍結・SPEC §2) ────────────────────────────────────────────────

def classify_cells(meta: dict) -> tuple[str, ...]:
    month = int(str(meta["date"])[4:6])
    track = meta["track_type"]
    wet = meta["condition"] in WET
    dist = meta["distance"]
    place = meta["place"]
    cls = meta["race_class"]
    cells = []
    if track == "芝" and wet and 1400 <= dist <= 1800:
        cells.append("C1")
    if track == "ダート" and wet and 1400 <= dist <= 1800:
        cells.append("C2")
    if place in LOCAL_PLACES and cls == "2勝クラス":
        cells.append("C3")
    if cls in CLASSES_123 and meta["field_size"] >= 14 and meta["is_handicap"]:
        cells.append("C4")
    if month in (12, 1, 2) and meta["is_fillies_only"] and cls in CLASSES_123:
        cells.append("C5")
    if month in (1, 2, 3) and cls == "未勝利" and meta["max_age"] == 3:
        cells.append("C6")
    if (month in (6, 7, 8, 9) and place in LOCAL_PLACES
            and cls in ("新馬", "未勝利") and meta["max_age"] == 2):
        cells.append("C7")
    return tuple(cells)


def build_observations(scores, labels, keys, meta_rows, temperature):
    """T62のbuild_race_observationsと同じ選抜・同じd定義。9R制約のみ外し、
    セル判定用のレース属性を付す (docstring冒頭の凍結解釈を参照)。"""
    grouped = defaultdict(list)
    for score, label, key, row in zip(scores, labels, keys, meta_rows):
        grouped[key].append((float(score), int(label), row))
    observations = []
    for key in sorted(grouped):
        members = sorted(grouped[key], key=lambda item: _runner_sort_key(item[2]))
        if len(members) < 8:
            continue
        field_size = max(
            (int(row.get("total_horses") or 0) for _, _, row in members), default=0)
        if not field_size or len(members) != field_size:
            continue
        first = members[0][2]
        if str(first.get("track_type") or "") not in ("芝", "ダート"):
            continue
        winner_indices = [i for i, (_s, label, _r) in enumerate(members) if label == 1]
        if not winner_indices:
            continue
        odds = [parse_final_odds(row.get("win_pay"), row.get("rank"))
                for _, _, row in members]
        if any(v is None or not math.isfinite(float(v)) or float(v) <= 1.0
               for v in odds):
            continue
        odds_array = np.asarray(odds, dtype=float)
        market = 1.0 / odds_array
        market /= market.sum()
        model = _softmax([score for score, _, _ in members], temperature)
        model_loss = float(np.mean(
            [-math.log(max(EPS, float(model[i]))) for i in winner_indices]))
        market_loss = float(np.mean(
            [-math.log(max(EPS, float(market[i]))) for i in winner_indices]))
        try:
            winner_popularity = min(
                int(members[i][2].get("popularity")) for i in winner_indices)
        except (TypeError, ValueError):
            winner_popularity = 999

        def market_order_key(index):
            try:
                popularity = int(members[index][2].get("popularity"))
            except (TypeError, ValueError):
                popularity = 999
            return (popularity, float(odds_array[index]),
                    _runner_sort_key(members[index][2])[0])

        ages = [int(row.get("age") or 0) for _, _, row in members]
        race_name = str(first.get("race_name") or "")
        meta = {
            "date": str(key[0]), "place": str(key[1]), "race_no": int(key[2]),
            "track_type": str(first.get("track_type") or ""),
            "condition": str(first.get("condition") or ""),
            "distance": int(first.get("distance") or 0),
            "race_class": str(first.get("race_class") or ""),
            "race_name": race_name,
            "field_size": field_size,
            "is_handicap": "Ｈ" in race_name,
            "is_fillies_only": "牝" in race_name,
            "max_age": max(ages) if ages else 0,
        }
        observations.append({
            "key": key, "date": meta["date"],
            "d": market_loss - model_loss,  # 正=モデル優位 (凍結定義)
            "model_win_in_topk": tuple(
                any(i in winner_indices for i in sorted(
                    range(len(members)),
                    key=lambda j: (-float(model[j]), _runner_sort_key(members[j][2])[0]),
                )[:k]) for k in (1, 2, 3, 4)),
            "market_win_in_topk": tuple(
                any(i in winner_indices for i in sorted(
                    range(len(members)), key=market_order_key)[:k])
                for k in (1, 2, 3, 4)),
            "winner_popularity": winner_popularity,
            "cells": classify_cells(meta),
            "meta": meta,
        })
    return observations


def generate_observations(db_path: Path):
    contract = rolling_origin_contract()
    validate_rolling_origin_contract(contract)
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
        runs = load_runs(conn, TRAIN_FROM, DATA_TO)
        cursor = conn.execute(
            """SELECT date, place, rank, track_type, distance, race_class,
                      condition, time_sec FROM runs
               WHERE date BETWEEN '20160101' AND ?""", (DATA_TO,))
        columns = [item[0] for item in cursor.description]
        standard_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    cfg = load_win5_cfg()
    features, labels, keys, meta, _audit = build_strict_feature_dataset(
        runs, standard_rows, cfg, db_path)
    dates = np.asarray([key[0] for key in keys])
    observations = {}
    previous_oos = None
    for index, fold in enumerate(contract):
        model = _fit_base(
            features, labels, keys, dates, fold["train_from"], fold["train_to"])
        target = _mask(dates, fold["target_from"], fold["target_to"])
        if not target.any():
            raise T65Error(f"{fold['target']}: empty target period")
        temperature = 1.0 if index == 0 else fit_temperature(
            previous_oos["scores"], previous_oos["labels"], previous_oos["keys"])
        target_scores = _score_base(model, features, target)
        target_labels = labels[target]
        target_keys = _selected(keys, target)
        target_meta = _selected(meta, target)
        if fold["target"] in ("2024", "2025_2026H1"):
            observations[fold["target"]] = build_observations(
                target_scores, target_labels, target_keys, target_meta, temperature)
        previous_oos = {"scores": target_scores, "labels": target_labels,
                        "keys": target_keys}
    pooled = []
    for obs in observations.get("2024", []):
        pooled.append({**obs, "period": "2024"})
    for obs in observations.get("2025_2026H1", []):
        pooled.append({**obs,
                       "period": "2025" if obs["date"] < "20260101" else "2026H1"})
    return pooled


# ── 統計 (凍結ゲート・SPEC §3) ───────────────────────────────────────────────

def _family_masks(observations):
    masks = {}
    for family, cells in FAMILIES.items():
        member = np.asarray(
            [bool(set(obs["cells"]) & set(cells)) for obs in observations])
        masks[family] = member
    return masks


def _stats_for(d_values, member):
    inside = d_values[member]
    outside = d_values[~member]
    if not len(inside) or not len(outside):
        return None, None
    mean_in = float(inside.mean())
    return mean_in, mean_in - float(outside.mean())


def gate_statistics(observations, *, resamples=RESAMPLES, seed=SEED):
    """6片側仮説 (3family×{E[d|F]>0, θ_F>Δ_min}) を同一開催日blockドロー上で
    同時再計算し、centered bootstrap maxTで補正する。"""
    d_values = np.asarray([obs["d"] for obs in observations])
    dates = np.asarray([obs["date"] for obs in observations])
    masks = _family_masks(observations)
    hypotheses = []   # (family, kind, observed, null)
    for family, member in masks.items():
        mean_in, theta = _stats_for(d_values, member)
        if mean_in is None:
            raise T65Error(f"{family}: empty family or complement")
        hypotheses.append([family, "mean", mean_in, 0.0])
        hypotheses.append([family, "theta", theta, DELTA_MIN])
    unique_days = np.unique(dates)
    day_indices = {day: np.flatnonzero(dates == day) for day in unique_days}
    rng = np.random.default_rng(SEED if seed is None else seed)
    draws = np.empty((resamples, len(hypotheses)))
    for b in range(resamples):
        sampled_days = rng.choice(unique_days, size=len(unique_days), replace=True)
        idx = np.concatenate([day_indices[day] for day in sampled_days])
        d_b = d_values[idx]
        col = 0
        for family, member in masks.items():
            member_b = member[idx]
            mean_in, theta = _stats_for(d_b, member_b)
            draws[b, col] = np.nan if mean_in is None else mean_in
            draws[b, col + 1] = np.nan if theta is None else theta
            col += 2
    draws = np.where(np.isnan(draws), -np.inf, draws)
    se = np.asarray([
        max(1e-12, float(np.std(draws[np.isfinite(draws[:, j]), j], ddof=1)))
        for j in range(len(hypotheses))])
    observed = np.asarray([h[2] for h in hypotheses])
    nulls = np.asarray([h[3] for h in hypotheses])
    t_observed = (observed - nulls) / se
    centered = (draws - observed) / se     # 帰無分布の近似 (centered bootstrap)
    max_t = centered.max(axis=1)
    p_maxt = [(float(np.mean(max_t >= t)), ) [0] for t in t_observed]
    # Holm (感度分析): 個別片側p → Holm補正
    p_raw = [float(np.mean(centered[:, j] >= t_observed[j]))
             for j in range(len(hypotheses))]
    order = np.argsort([-t for t in t_observed])  # 小さいp順 = 大きいt順
    p_holm = [None] * len(hypotheses)
    running = 0.0
    for rank, j in enumerate(order):
        adjusted = min(1.0, (len(hypotheses) - rank) * p_raw[j])
        running = max(running, adjusted)
        p_holm[j] = running
    results = {}
    for col, (family, kind, observed_value, null_value) in enumerate(hypotheses):
        results.setdefault(family, {})[kind] = {
            "observed": observed_value, "null": null_value,
            "se_bootstrap": float(se[col]), "t": float(t_observed[col]),
            "p_maxt": p_maxt[col], "p_holm": float(p_holm[col]),
        }
    for family in results:
        results[family]["pass_dual_maxt"] = bool(
            results[family]["mean"]["p_maxt"] < ALPHA
            and results[family]["theta"]["p_maxt"] < ALPHA)
    return results


def per_period_and_diagnostics(observations):
    d_values = np.asarray([obs["d"] for obs in observations])
    periods = np.asarray([obs["period"] for obs in observations])
    masks = _family_masks(observations)
    out = {"families": {}, "cells": {}, "overlap": {}}
    for family, member in masks.items():
        rows = {}
        for period in PERIODS:
            sel = member & (periods == period)
            rows[period] = {
                "races": int(sel.sum()),
                "mean_d": float(d_values[sel].mean()) if sel.any() else None,
            }
        signs = [rows[p]["mean_d"] for p in PERIODS if rows[p]["mean_d"] is not None]
        pooled_sel = member
        model_topk = np.asarray(
            [obs["model_win_in_topk"] for obs in observations])[pooled_sel]
        market_topk = np.asarray(
            [obs["market_win_in_topk"] for obs in observations])[pooled_sel]
        bands = {}
        for label, low, high in (("1-3", 1, 3), ("4-8", 4, 8), ("9+", 9, 999)):
            sel_band = pooled_sel & np.asarray(
                [low <= obs["winner_popularity"] <= high for obs in observations])
            bands[label] = {
                "races": int(sel_band.sum()),
                "mean_d": float(d_values[sel_band].mean()) if sel_band.any() else None,
            }
        out["families"][family] = {
            "per_period": rows,
            "same_sign_3periods": bool(
                len(signs) == len(PERIODS)
                and (all(v > 0 for v in signs) or all(v < 0 for v in signs))),
            "topk_floor": {
                f"k{k}": {
                    "model": float(model_topk[:, k - 1].mean()),
                    "market": float(market_topk[:, k - 1].mean()),
                    "ok": bool(model_topk[:, k - 1].mean()
                               >= market_topk[:, k - 1].mean()),
                } for k in (1, 2, 3, 4)
            },
            "popularity_bands": bands,
        }
    cell_names = ("C1", "C2", "C3", "C4", "C5", "C6", "C7")
    cell_masks = {c: np.asarray([c in obs["cells"] for obs in observations])
                  for c in cell_names}
    theta_by_cell = {}
    for cell, member in cell_masks.items():
        rows = {}
        for period in PERIODS:
            sel = member & (periods == period)
            mean_in, theta = (None, None)
            if sel.any() and (~member & (periods == period)).any():
                inside = d_values[sel]
                outside = d_values[~member & (periods == period)]
                mean_in = float(inside.mean())
                theta = mean_in - float(outside.mean())
            rows[period] = {"races": int(sel.sum()), "mean_d": mean_in,
                            "theta": theta}
        pooled_mean, pooled_theta = _stats_for(d_values, member) \
            if member.any() and (~member).any() else (None, None)
        out["cells"][cell] = {"per_period": rows, "pooled_mean_d": pooled_mean,
                              "pooled_theta": pooled_theta}
        theta_by_cell[cell] = [rows[p]["theta"] for p in PERIODS
                               if rows[p]["theta"] is not None]
    for a in cell_names:
        for b in cell_names:
            if a < b:
                shared = int((cell_masks[a] & cell_masks[b]).sum())
                if shared:
                    out["overlap"][f"{a}&{b}"] = shared
    # 階層縮約 (順位付け専用・ゲート外)
    flat = [(cell, theta) for cell, thetas in theta_by_cell.items()
            for theta in thetas]
    if flat:
        values = np.asarray([theta for _, theta in flat])
        grand = float(values.mean())
        tau2 = max(0.0, float(values.var(ddof=1)) - float(values.var(ddof=1)) / 3)
        shrunk = {}
        for cell, thetas in theta_by_cell.items():
            if not thetas:
                continue
            cell_mean = float(np.mean(thetas))
            weight = tau2 / (tau2 + max(1e-12, float(np.var(thetas)) / len(thetas)))
            shrunk[cell] = grand + weight * (cell_mean - grand)
        out["shrinkage_ranking"] = sorted(
            shrunk.items(), key=lambda item: -item[1])
    # C4ドリフト監査 (SPEC §2)
    handicap_2026 = sum(
        1 for obs in observations
        if obs["period"] == "2026H1" and obs["meta"]["is_handicap"])
    out["c4_2026H1_handicap_races"] = handicap_2026
    out["c4_2026H1_note"] = (
        "判定=race_name内の全角Ｈマーカー。2026年ソースドリフトにより0件の場合、"
        "C4の2026H1は欠測 (代替定義は発明しない)")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    registration = require_registration(db_path=args.db)
    observations = generate_observations(args.db)
    gate = gate_statistics(observations)
    diagnostics = per_period_and_diagnostics(observations)
    passing = [family for family, row in gate.items()
               if row["pass_dual_maxt"]
               and diagnostics["families"][family]["same_sign_3periods"]
               and all(v["ok"] for v in
                       diagnostics["families"][family]["topk_floor"].values())]
    payload = {
        "experiment_id": EXPERIMENT_ID,
        "registered_at_utc": registration["registered_at_utc"],
        "population_note": (
            "台帳の明示列挙 (平地・8頭以上・全馬オッズあり) を凍結解釈として採用。"
            "T62歴史層の9R以降制約は含めない (C6/C7の討議実測件数と矛盾するため)。"
            "この解釈は実行前に確定した"),
        "expectation_note": (
            "negative priorに基づき全滅が事前期待。通過family 0 は実験の失敗ではなく"
            "反証の成功"),
        "alpha_one_sided_maxt": ALPHA,
        "delta_min": DELTA_MIN,
        "n_observations": len(observations),
        "gate": gate,
        "diagnostics": diagnostics,
        "passing_families": passing,
        "seed": SEED, "resamples": RESAMPLES,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1,
                   allow_nan=False) + "\n", encoding="utf-8")
    print(args.output)
    print("passing families:", passing or "なし (事前期待どおりなら反証成功)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
