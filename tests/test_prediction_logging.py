import json
import sqlite3

from api.logging_store import LoggingStore
from api.prediction_logging import log_race_prediction, race_id_for


def test_common_ids_and_web_prediction_logging(tmp_path):
    store = LoggingStore(tmp_path / "predictions.db")
    result = {
        "race_date": "20260711", "race_num": 3, "venue": "東京",
        "horses": [{"num": 7, "name": "テスト馬", "score": 12.5, "score_ml": 8.2,
                    "place_prob": 0.345, "place_prob_details": ["複勝 騎手: +0.2"],
                    "score_details": ["根拠"], "odds": "4.6", "pop": "2"}],
    }
    assert race_id_for(result) == "20260711:東京:03"
    context = log_race_prediction(
        result, app_name="web", config={"version": 2}, model_name="web_score",
        model_version=2, base_dir=tmp_path, store=store)
    assert context["race_id"] == "20260711:東京:03"
    assert result["horses"][0]["_horse_id"] == "20260711:東京:03:07"
    with sqlite3.connect(store.db_path) as conn:
        app_name, version = conn.execute(
            "SELECT app_name,model_version FROM prediction_runs").fetchone()
        web_score, win5_score, place_probability, details_json = conn.execute(
            "SELECT web_score,win5_score,place_probability,score_details_json "
            "FROM predictions").fetchone()
        odds = conn.execute("SELECT win_odds FROM odds_snapshots").fetchone()[0]
    assert (app_name, version) == ("web", "2")
    assert (web_score, win5_score, odds) == (12.5, 8.2, 4.6)
    assert place_probability == 0.345
    assert json.loads(details_json)["place"] == ["複勝 騎手: +0.2"]
