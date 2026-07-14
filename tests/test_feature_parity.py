"""Offline contract test for training/live WIN5 ML feature parity."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

import backtest_ml
from backtest_ml import NO_MARKET_FEATURES
import analysis
import scoring
from backtest_ability import load_runs
from backtest_criteria import load_mawari
from backtest_win5 import load_win5_cfg, parse_final_odds
from past_data_service import calculate_waku
from factor_snapshot import (build_snapshot_payload, reset_snapshot_cache,
                             write_factor_snapshot)
from fold_stats import FoldFactorTableProvider, fold_as_of_for


ROOT = Path(__file__).parents[1]
ABILITY_DB = ROOT / "ability.db"
MODEL_PATH = ROOT / "api" / "data_files" / "common" / "win5_ml_model.json"
N_RACES = 20

# T6/T7 completion removed pace_fit/weight from this list.  T8 unified the training/live
# grade_pts definition (both now use the pedigree-excluded rule set), removing it here.
# Final-vs-live odds remain intentionally distinct (backtest uses settled odds).
EXPECTED_MISMATCHES = {"ln_odds"}


def _latest_complete_races(conn, limit=N_RACES):
    return [tuple(row) for row in conn.execute("""
        SELECT date,place,r FROM runs
        WHERE rank IS NOT NULL AND track_type IN ('芝','ダート')
        GROUP BY date,place,r
        HAVING count(*) >= 8 AND sum(rank IS NOT NULL)=count(*)
        ORDER BY date DESC,place,r DESC LIMIT ?
    """, (limit,))]


def _time_text(seconds):
    if seconds is None:
        return "-"
    seconds = float(seconds)
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"{minutes}:{rest:04.1f}" if minutes else f"{rest:.1f}"


def _live_history(run):
    surface = "芝" if run["track_type"] == "芝" else "ダ"
    date = datetime.strptime(run["date"], "%Y%m%d")
    raw = (f"{date.year}年{date.month}月{date.day}日 {run['place']} "
           f"{run.get('race_name') or ''} {run.get('rank') or '-'} 着 "
           f"{run.get('total_horses') or '-'} 頭")
    return {
        "raw": raw,
        "place": run["place"],
        "course": f"{run['distance']}{surface}",
        "track_type": run["track_type"],
        "distance": run["distance"],
        "race_name": run.get("race_name") or "",
        "race_class": run.get("race_class") or "",
        "condition": run.get("condition") or "",
        "rank": str(run.get("rank") or "-"),
        "total": str(run.get("total_horses") or "-"),
        "corners": str(run.get("c4") or "-"),
        "run_time": _time_text(run.get("time_sec")),
        "time_sec": run.get("time_sec"),
        "agari_rank": run.get("agari_rank_str", "-"),
        "agari_ratio": run.get("agari_ratio"),
        "agari_3f": run.get("agari"),
        "weight": str(run.get("weight") or ""),
        "pci": run.get("pci"),
    }


def _live_horse(cur, prior, pedigree, criteria, sire_lineage, mawari):
    try:
        interval_days = (datetime.strptime(cur["date"], "%Y%m%d")
                         - datetime.strptime(prior[0]["date"], "%Y%m%d")).days
    except (IndexError, ValueError):
        interval_days = None
    ped = pedigree.get(cur["horse"], {})
    affi = "美浦" if "美" in str(cur.get("affi") or "") else (
        "栗東" if "栗" in str(cur.get("affi") or "") else "")
    h = {
        "name": cur["horse"], "num": cur["umaban"],
        "w_num": calculate_waku(cur["umaban"], cur["total_horses"]),
        "jock": cur.get("jockey") or "", "kg": str(cur.get("kinryo") or ""),
        "sex_age": f"{cur.get('sex') or ''}{cur.get('age') or ''}",
        "affi": affi, "sire": ped.get("sire") or "-", "bms": ped.get("bms") or "-",
        "odds": parse_final_odds(cur.get("win_pay"), cur.get("rank")) or 30.0,
        "interval_days": interval_days,
        "iv": "-", "hist": [_live_history(run) for run in prior],
        "current_weight": cur.get("weight"), "weight_source": "jra_live",
    }
    rc = {"type": cur["track_type"], "dist": cur["distance"], "venue": cur["place"],
          "total_horses": cur["total_horses"], "race_class": cur.get("race_class") or "",
          "class": cur.get("race_class") or "", "baba_cond": cur.get("condition") or ""}
    grade, _details, matches = analysis.evaluate_ultra(
        h, rc, criteria.get(cur["place"], []), sire_lineage, mawari)
    h["grade"] = grade
    h["ultra_matches"] = matches
    return h, rc


def _print_report(features, mismatches, counts):
    out = sys.__stdout__
    print("\n===== 学習/ライブ 特徴量不一致率 (ability.db・直近20R) =====", file=out)
    print("特徴量       | 不一致馬 | 比率     | 扱い", file=out)
    for name in features:
        n = counts[name]
        rate = 100.0 * mismatches[name] / n if n else 0.0
        status = "EXPECTED" if name in EXPECTED_MISMATCHES else "MUST MATCH"
        print(f"{name:12s} | {mismatches[name]:7d}/{n:<4d} | {rate:7.2f}% | {status}", file=out)


@pytest.mark.parametrize("factor_source", ["legacy_fallback", "ability_snapshot"])
def test_training_and_live_feature_parity(factor_source, tmp_path, monkeypatch):
    with MODEL_PATH.open(encoding="utf-8") as fh:
        features = json.load(fh)["features"]
    assert features == backtest_ml.FEATURES
    assert len(features) == 21

    with sqlite3.connect(ABILITY_DB) as conn:
        selected = set(_latest_complete_races(conn))
        assert len(selected) == N_RACES
        date_from = min(key[0] for key in selected)
        date_to = max(key[0] for key in selected)
        runs = load_runs(conn, date_from, date_to)

    snapshot_path = tmp_path / "factor_snapshot.json"
    monkeypatch.setattr(scoring, "FACTOR_SNAPSHOT_PATH", str(snapshot_path))
    if factor_source == "ability_snapshot":
        as_of = fold_as_of_for(date_to)
        provider = FoldFactorTableProvider(
            ABILITY_DB,
            as_of,
            pedigree_cache_path=ROOT / "pedigree_cache.json",
            legacy_api_dir=ROOT / "api",
        )
        payload = build_snapshot_payload(
            provider.tables,
            as_of=provider.as_of,
            stats_from=provider.stats_from,
            generated_at="2026-07-13T00:00:00+09:00",
        )
        write_factor_snapshot(snapshot_path, payload)
    reset_snapshot_cache()

    cfg = load_win5_cfg()
    X, _y, race_keys, meta = backtest_ml.build_dataset(runs, date_from, cfg)
    training = {
        id(cur): dict(zip(features, vector))
        for vector, key, cur in zip(X, race_keys, meta) if key in selected
    }
    assert training

    by_horse = defaultdict(list)
    for run in runs:
        if run.get("horse"):
            by_horse[run["horse"]].append(run)
    positions = {}
    for history in by_horse.values():
        history.sort(key=lambda item: item["date"])
        for index, run in enumerate(history):
            positions[id(run)] = (history, index)

    import pedigree_store
    pedigree = pedigree_store.load_all()
    criteria = {venue: analysis.load_csv_criteria(venue, str(ROOT / "api"))
                for venue in {key[1] for key in selected}}
    sire_lineage = analysis.load_sire_lineage(str(ROOT / "api"))
    mawari = load_mawari()
    factor_tables = {}
    live_by_race = defaultdict(list)

    for cur in meta:
        key = (cur["date"], cur["place"], cur["r"])
        if key not in selected or id(cur) not in training:
            continue
        history, index = positions[id(cur)]
        prior = history[max(0, index - 4):index][::-1]
        h, rc = _live_horse(cur, prior, pedigree, criteria, sire_lineage, mawari)
        live_by_race[key].append((cur, h, rc))

    mismatches = {name: 0 for name in features}
    counts = {name: 0 for name in features}
    examples = defaultdict(list)
    for key, members in live_by_race.items():
        scoring.attach_live_pace_features([h for _cur, h, _rc in members])
        for cur, h, rc in members:
            fkey = (cur["place"], cur["track_type"], cur["distance"])
            if fkey not in factor_tables:
                factor_tables[fkey] = scoring.load_factor_table(*fkey, str(ROOT / "api"))
            live = scoring._ml_features(h, rc, factor_tables[fkey], cfg)
            trained = training[id(cur)]
            for name in features:
                counts[name] += 1
                diff = abs(float(trained[name]) - float(live[name]))
                if diff > 1e-6:
                    mismatches[name] += 1
                    if len(examples[name]) < 3:
                        examples[name].append((key, cur["horse"], trained[name], live[name]))

    _print_report(features, mismatches, counts)
    unexpected = {name: examples[name] for name in features
                  if name not in EXPECTED_MISMATCHES and mismatches[name]}
    place_feature_mismatches = {
        name: examples[name] for name in NO_MARKET_FEATURES if mismatches[name]
    }
    assert not place_feature_mismatches, (
        "T36 place-model live/training mismatches: "
        f"{place_feature_mismatches}"
    )
    assert not unexpected, f"unexpected live/training feature mismatches: {unexpected}"
    assert all(counts[name] > 0 for name in features)
