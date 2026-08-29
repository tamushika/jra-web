import ast
import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

import backtest_t62_race_selection as historical
import backtest_t62b_shadow as shadow
from api.logging_store import LoggingStore
from api import race_confidence


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SHA = "db8df644fc5025f91d1ae6a7c67fe394a02f690a54f73f8b6aeb4d445055b1f3"


def test_manifest_is_sealed_and_matches_t62_result_ledger():
    manifest = race_confidence.load_manifest()
    assert manifest["manifest_sha256"] == MANIFEST_SHA
    entries = [json.loads(line) for line in (ROOT / "eval/experiments.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    result = next(row for row in entries
                  if row["experiment_id"] == "T62-race-relative-confidence-v1-result")
    assert result["data_hashes"]["freeze_manifest_sha256"] == MANIFEST_SHA


def test_manifest_mutation_fails_closed(tmp_path):
    manifest = json.loads((ROOT / "eval/t62_freeze_manifest.json").read_text(encoding="utf-8"))
    manifest["intercept"] += 1
    path = tmp_path / "mutated.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        race_confidence.load_manifest(path)


def test_live_feature_and_score_reproduce_historical_definition():
    model = np.asarray([.26, .18, .14, .12, .10, .08, .07, .05])
    odds = np.asarray([3.4, 4.8, 6.3, 7.5, 9.0, 11.0, 14.0, 20.0])
    inverse = 1 / odds
    market = inverse / inverse.sum()
    history = np.asarray([1, 1, 1, 0, 1, 1, 0, 1])
    live = race_confidence.compute_features(model, odds, history, race_class="2勝クラス")
    expected = historical.compute_race_features(
        model, market, odds, history, race_class="2勝クラス")
    np.testing.assert_allclose(live, expected, rtol=0, atol=1e-14)
    manifest = race_confidence.load_manifest()
    expected_score = historical.score_frozen(manifest, [{"features": expected}])[0]
    assert race_confidence.score_features(live, manifest) == pytest.approx(expected_score, abs=1e-15)


@pytest.mark.parametrize("kwargs", [
    {"race_no": 8, "race_type": "芝"},
    {"race_no": 9, "race_type": "障害"},
])
def test_out_of_population_has_no_score(kwargs):
    horses = [{"num": i + 1, "win_prob": .125, "odds": 8.0,
               "history_available": True} for i in range(8)]
    result = race_confidence.build_live_score(
        horses, race_class="2勝クラス", stage=30, **kwargs)
    assert result["status"] == "out_of_scope"
    assert result["score"] is None


def test_under_eight_runners_and_missing_odds_have_no_score():
    horses = [{"num": i + 1, "win_prob": 1 / 7, "odds": 7.0,
               "history_available": True} for i in range(7)]
    assert race_confidence.build_live_score(
        horses, race_no=9, race_type="芝", race_class="重賞", stage=30)["score"] is None
    horses.append({"num": 8, "win_prob": 1 / 8, "odds": None, "history_available": True})
    assert race_confidence.build_live_score(
        horses, race_no=9, race_type="芝", race_class="重賞", stage=30)["score"] is None


def test_scratched_runners_are_excluded_not_fatal():
    # 取消馬はオッズを持たないが、歴史母集団 (実出走馬のみ) と揃えるため
    # レースを対象外にせず除外して残り馬でスコアを出すこと
    horses = [{"num": i + 1, "win_prob": 1 / 9, "odds": 9.0,
               "history_available": True} for i in range(9)]
    horses.append({"num": 10, "win_prob": None, "odds": None,
                   "scratched": True, "history_available": False})
    result = race_confidence.build_live_score(
        horses, race_no=9, race_type="芝", race_class="重賞", stage=30)
    assert result["status"] == "ok"
    assert result["score"] is not None
    assert "10" not in result["model_probs"]  # 取消馬は記録にも含めない

    # 取消を除いて8頭未満になるレースは従来どおり対象外
    few = horses[:7] + [horses[-1]]
    assert race_confidence.build_live_score(
        few, race_no=9, race_type="芝", race_class="重賞", stage=30)["score"] is None


def test_display_contains_caution_and_no_purchase_language():
    html = (ROOT / "index_boards.html").read_text(encoding="utf-8")
    assert "本日の勝負レース度" in html
    assert "人気上位馬をモデルが市場より正確に評価できている可能性" in html
    assert "歴史検証では優位が1-3人気帯に集中し、中穴・大穴帯ではむしろ悪化" in html
    for prohibited in ("買い", "推奨", "◎", "⭐", "勝負確定"):
        assert prohibited not in html


def test_notification_function_sources_are_frozen():
    # ast.dumpのハッシュ固定はPythonバージョンでダンプ形式が変わると壊れる
    # (2026-08-01 レビューで発覚)。凍結契約なのでソース断片そのものを固定する。
    # 変更が必要になったら、その変更自体を裁定してからハッシュを更新すること。
    source = (ROOT / "jra_ev.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    segments = {
        node.name: ast.get_source_segment(source, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in ("_send_line", "_send_discord", "refresh_and_alert")
    }
    expected = {
        # 2026-08-30 SPEC-T71 (暫定閾値1.1・参考文言・旧ワイド110%文言の削除) を上位モデルが
        # 裁定したうえで _send_line/_send_discord のハッシュを更新。refresh_and_alert は不変。
        "_send_line": "b34eef74cf4d38e8d1344761c50e0d1c91e817175cc48f81bd3e708dbc6b8292",
        "_send_discord": "41ada625a0e4e87a30c1d125d0657dd5e8fb28b9e867ef42f3b78bd85c78df8f",
        "refresh_and_alert": "4193fbdb992cc192b5d02bdb5118db75dcfb432c8972f796d50143e2a98c6b7c",
    }
    for name, digest in expected.items():
        value = hashlib.sha256(segments[name].encode()).hexdigest()
        assert value == digest, name


def test_snapshot_schema_is_asof_only_and_rejects_results(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    store.initialize()
    with sqlite3.connect(store.db_path) as connection:
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(race_confidence_snapshots)")}
    assert {"date", "place", "r", "cutoff_at", "snapshot_source",
            "model_probs_json", "market_odds_json", "features_json", "score",
            "threshold", "selected", "manifest_sha256", "created_at"} <= columns
    assert not ({"winner", "finish_position", "final_win_odds"} & columns)
    with pytest.raises(ValueError, match="post-race fields"):
        store.save_race_confidence({"date": "20260801", "place": "新潟", "r": 9,
                                    "snapshot_source": "jra", "winner": 1})


def test_gate_exposes_counts_only_and_blocks_before_threshold(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    store.initialize()
    for index in range(3):
        store.save_race_confidence({
            "date": "20260801", "place": "新潟", "r": 9 + index,
            "snapshot_source": "jra", "model_probs": {"1": .5, "2": .5},
            "market_odds": {"1": 2.0, "2": 2.0}, "features": [0] * 11,
            "score": .01, "threshold": .008, "selected": True,
            "manifest_sha256": MANIFEST_SHA,
        })
    status = shadow.accumulation_gate(store.db_path)
    assert (status.event_days, status.races, status.ready) == (1, 3, False)
    with pytest.raises(RuntimeError, match="3/200 races; 1/4 event days"):
        shadow.require_accumulation_gate(store.db_path)


def test_t62b_experiment_is_preregistered_and_unadjudicated():
    entries = [json.loads(line) for line in (ROOT / "eval/experiments.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    rows = [row for row in entries if row["experiment_id"] == shadow.EXPERIMENT_ID]
    assert len(rows) == 1
    assert rows[0]["benchmark_type"] == "prospective"
    assert rows[0]["adjudication"] is None
    assert rows[0]["result_summary"] is None
