"""T62b display-only frozen race confidence score."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "eval" / "t62_freeze_manifest.json"
EXPECTED_MANIFEST_SHA256 = "db8df644fc5025f91d1ae6a7c67fe394a02f690a54f73f8b6aeb4d445055b1f3"
FEATURE_NAMES = (
    "js_divergence", "top1_disagree", "rank_corr", "model_entropy",
    "market_entropy", "market_top1_margin", "hist_coverage", "odds_takeout",
    "field_size", "is_maiden", "class_level",
)
CLASS_LEVELS = {"新馬": 0.0, "未勝利": 0.0, "1勝クラス": 1.0,
                "2勝クラス": 2.0, "3勝クラス": 3.0,
                "オープン": 4.0, "重賞": 4.0}


def canonical_sha256(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    sealed = manifest.get("manifest_sha256")
    payload = {key: value for key, value in manifest.items()
               if key != "manifest_sha256"}
    if sealed != EXPECTED_MANIFEST_SHA256 or canonical_sha256(payload) != sealed:
        raise ValueError("T62 manifest SHA-256 mismatch")
    if tuple(manifest.get("features", ())) != FEATURE_NAMES:
        raise ValueError("T62 manifest feature contract mismatch")
    return manifest


def _average_descending_ranks(values: np.ndarray) -> np.ndarray:
    order = np.argsort(-values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    position = 0
    while position < len(values):
        end = position + 1
        while end < len(values) and values[order[end]] == values[order[position]]:
            end += 1
        ranks[order[position:end]] = (position + 1 + end) / 2.0
        position = end
    return ranks


def compute_features(model_probabilities, market_odds, history_flags, *, race_class):
    model = np.asarray(model_probabilities, dtype=float)
    odds = np.asarray(market_odds, dtype=float)
    flags = np.asarray(history_flags, dtype=float)
    if not len(model) or not (len(model) == len(odds) == len(flags)):
        raise ValueError("race feature inputs must be non-empty and aligned")
    inverse = 1.0 / odds
    market = inverse / inverse.sum()
    if (not np.all(np.isfinite(model)) or not np.all(np.isfinite(odds))
            or not np.all(np.isfinite(flags)) or np.any(model <= 0)
            or np.any(odds <= 1) or not np.isclose(model.sum(), 1.0)):
        raise ValueError("race feature inputs must be finite, valid, and normalized")
    midpoint = .5 * (model + market)
    js = .5 * float(np.sum(model * np.log(model / midpoint)))
    js += .5 * float(np.sum(market * np.log(market / midpoint)))
    model_ranks = _average_descending_ranks(model)
    market_ranks = _average_descending_ranks(market)
    rank_corr = (0.0 if np.std(model_ranks) == 0 or np.std(market_ranks) == 0
                 else float(np.corrcoef(model_ranks, market_ranks)[0, 1]))
    normalizer = math.log(len(model)) if len(model) > 1 else 1.0
    market_sorted = np.sort(market)[::-1]
    class_text = str(race_class or "")
    return np.asarray([
        js, float(np.argmax(model) != np.argmax(market)), rank_corr,
        -float(np.sum(model * np.log(model))) / normalizer,
        -float(np.sum(market * np.log(market))) / normalizer,
        float(market_sorted[0] - market_sorted[1]) if len(market) > 1 else 0.0,
        float(np.mean(flags)), float(inverse.sum() - 1.0), float(len(model)),
        float(class_text in ("新馬", "未勝利")), CLASS_LEVELS.get(class_text, 0.0),
    ], dtype=float)


def score_features(features, manifest=None) -> float:
    manifest = manifest or load_manifest()
    vector = np.asarray(features, dtype=float)
    mean = np.asarray(manifest["scaler_mean"], dtype=float)
    scale = np.asarray(manifest["scaler_scale"], dtype=float)
    coefficients = np.asarray(manifest["coefficients"], dtype=float)
    if vector.shape != mean.shape or np.any(scale == 0):
        raise ValueError("T62 feature vector shape mismatch")
    return float(((vector - mean) / scale) @ coefficients
                 + float(manifest["intercept"]))


def unavailable(reason: str) -> dict:
    return {"status": "out_of_scope", "label": "対象外", "reason": reason,
            "score": None, "selected": None}


def build_live_score(horses, *, race_no, race_type, race_class, stage,
                     cutoff_at=None, snapshot_source="jra") -> dict:
    if str(stage) != "30":
        return unavailable("30分前cutoff以外")
    if int(race_no or 0) < 9:
        return unavailable("9R未満")
    if str(race_type or "") == "障害":
        return unavailable("障害レース")
    # 歴史評価の母集団 (ability.db runs) は実際に出走した馬のみ。取消・除外馬を
    # 落とさずに全馬オッズ必須検査へ進むと、取消が1頭いるだけでレースが対象外に
    # なり、歴史との母集団定義がずれる (選定率ドリフト計測を汚染する)
    runners = [horse for horse in (horses or ())
               if not horse.get("scratched")]
    if len(runners) < 8:
        return unavailable("8頭未満")
    try:
        model = [float(horse["win_prob"]) for horse in runners]
        odds = [float(horse["odds"]) for horse in runners]
    except (KeyError, TypeError, ValueError):
        return unavailable("全馬の予測確率・単勝オッズが必要")
    if any(not math.isfinite(value) or value <= 0 for value in model):
        return unavailable("全馬の予測確率が必要")
    if any(not math.isfinite(value) or value <= 1 for value in odds):
        return unavailable("全馬の単勝オッズが必要")
    model_sum = sum(model)
    if model_sum <= 0:
        return unavailable("予測確率を正規化できない")
    model = [value / model_sum for value in model]
    history = [float(bool(horse.get("history_available"))) for horse in runners]
    try:
        manifest = load_manifest()
        features = compute_features(model, odds, history, race_class=race_class)
        score = score_features(features, manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "label": "表示停止", "reason": type(exc).__name__,
                "score": None, "selected": None}
    threshold = float(manifest["selection_threshold"])
    return {
        "status": "ok", "label": "選定" if score >= threshold else "非選定",
        "score": score, "threshold": threshold, "selected": score >= threshold,
        "features": features.tolist(),
        "model_probs": {str(horse.get("num")): probability
                        for horse, probability in zip(runners, model)},
        "market_odds": {str(horse.get("num")): odd
                        for horse, odd in zip(runners, odds)},
        "manifest_sha256": manifest["manifest_sha256"], "cutoff_at": cutoff_at,
        "snapshot_source": snapshot_source,
    }
