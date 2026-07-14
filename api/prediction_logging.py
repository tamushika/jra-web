"""Shared best-effort logging adapter for web, WIN5, and EV-compatible IDs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

try:
    from .logging_store import LoggingStore
except ImportError:
    from logging_store import LoggingStore


def race_id_for(result: Mapping[str, Any]) -> str:
    race_date = str(result.get("race_date") or "").replace("-", "")
    venue = str(result.get("venue") or "unknown")
    race_no = int(result.get("race_num") or 0)
    if len(race_date) != 8 or not race_date.isdigit():
        raise ValueError("race_date is required for common logging")
    return f"{race_date}:{venue}:{race_no:02d}"


def data_version(base_dir: str | Path) -> str | None:
    root = Path(base_dir)
    candidates = (root / "past_data_v2.db", root.parent / "ability.db")
    parts = []
    for path in candidates:
        try:
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            continue
    return "|".join(parts) or None


def log_race_prediction(result: dict[str, Any], *, app_name: str, config: Any,
                        model_name: str, model_version: str | int | None,
                        trigger_type: str = "manual", base_dir: str | Path,
                        store: LoggingStore | None = None) -> dict[str, str]:
    """Persist one analyzed race without allowing logging errors to escape."""
    target = store or LoggingStore()
    race_id = race_id_for(result)
    observed_at = datetime.now().astimezone()
    run_id = target.start_run(
        app_name=app_name, trigger_type=trigger_type, model_name=model_name,
        model_version=model_version, config=config, data_version=data_version(base_dir),
    )
    target.save_race(
        race_id=race_id, race_date=str(result["race_date"]), venue=str(result.get("venue") or "unknown"),
        race_no=int(result.get("race_num") or 0), race_name=result.get("race_name"),
        surface=result.get("race_type"), distance_m=result.get("dist_val"),
        race_class=result.get("race_class"), start_time=result.get("start_time"),
    )
    predictions, odds = [], []
    for horse in result.get("horses", []):
        try:
            horse_no = int(float(horse.get("num")))
        except (TypeError, ValueError):
            continue
        horse_id = f"{race_id}:{horse_no:02d}"
        horse["_horse_id"] = horse_id
        predictions.append({
            "race_id": race_id, "horse_id": horse_id, "predicted_at": observed_at,
            "web_score": horse.get("web_score", horse.get("score")) if app_name == "web" else horse.get("web_score"),
            "win5_score": horse.get("score") if app_name == "win5" else horse.get("score_ml"),
            "ml_score": horse.get("score_ml", horse.get("score") if app_name == "win5" else None),
            "raw_win_probability": horse.get("win_prob"),
            "calibrated_win_probability": horse.get("calibrated_win_probability"),
            "place_probability": horse.get("place_prob"),
            "score_details": {
                "primary": horse.get("score_details", []),
                "ml": horse.get("score_ml_details", []),
                "place": horse.get("place_prob_details", []),
                "grade": horse.get("grade"),
            },
            "feature_snapshot": {
                "horse_no": horse_no, "horse_name": horse.get("name"), "popularity": horse.get("pop"),
                "sire": horse.get("sire"), "jockey": horse.get("jock"),
            },
            "data_quality_flags": [] if horse.get("score") is not None else ["primary_score_unavailable"],
        })
        try:
            win_odds = float(horse.get("odds"))
            if win_odds <= 1.0:
                win_odds = None
        except (TypeError, ValueError):
            win_odds = None
        try:
            popularity = int(float(horse.get("pop")))
        except (TypeError, ValueError):
            popularity = None
        odds.append({
            "race_id": race_id, "horse_id": horse_id, "observed_at": observed_at,
            "win_odds": win_odds, "popularity": popularity, "source": "jra", "fetch_id": run_id,
            "idempotency_key": f"{run_id}:{horse_id}",
            "data_quality_flags": [] if win_odds is not None else ["win_odds_unavailable"],
        })
    target.save_predictions(run_id, predictions)
    target.save_odds(odds)
    target.finish_run(run_id, status="success" if predictions else "partial",
                      error_summary=None if predictions else "no valid runners")
    context = {"prediction_run_id": run_id, "race_id": race_id}
    result["_logging"] = context
    return context
