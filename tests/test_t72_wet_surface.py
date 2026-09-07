"""T72: 道悪適性ファクターの芝/ダート分離 (same_surface_only) の単体テスト。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "api"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(API_DIR))

import scoring  # noqa: E402
from backtest_wet import classify_gap  # noqa: E402


def _cfg(same_surface_only=False):
    return {
        "params": {
            "wet_aptitude": {
                "rank_gap": 2,
                "bonus_better": 2.0,
                "penalty_worse": -1.5,
                "same_surface_only": same_surface_only,
            },
        },
    }


def _run(track_type, condition, rank, place="中山", distance=1200):
    return {
        "place": place,
        "track_type": track_type,
        "distance": distance,
        "condition": condition,
        "rank": rank,
    }


# ── eval_wet_aptitude ────────────────────────────────────────────────────

def test_default_mixed_surface_reproduces_current_behavior():
    # ダート稍重3着・5着 vs 芝良1着、当日 芝・重 (T72起票の実例を再現)
    h = {"hist": [
        _run("ダ", "稍重", "3"),
        _run("ダ", "稍重", "5"),
        _run("芝", "良", "1"),
    ]}
    race_context = {"type": "芝", "baba_cond": "重"}
    pts, details = scoring.eval_wet_aptitude(h, race_context, _cfg(same_surface_only=False))
    assert pts == -1.5
    assert details
    assert "道悪の方が悪い" in details[0]
    assert "同surfaceのみ" not in details[0]


def test_same_surface_only_excludes_cross_surface_and_yields_no_signal():
    # 同じ馬でも same_surface_only=True なら芝のwet実績が無く相対比較不能 → 0.0
    h = {"hist": [
        _run("ダ", "稍重", "3"),
        _run("ダ", "稍重", "5"),
        _run("芝", "良", "1"),
    ]}
    race_context = {"type": "芝", "baba_cond": "重"}
    pts, details = scoring.eval_wet_aptitude(h, race_context, _cfg(same_surface_only=True))
    assert pts == 0.0
    assert details == []


def test_same_surface_only_bonus_with_marker_in_detail():
    # 芝稍重1着・芝良4着 (当日芝) → +2.0、detail に「同surfaceのみ」
    h = {"hist": [
        _run("芝", "稍重", "1"),
        _run("芝", "良", "4"),
    ]}
    race_context = {"type": "芝", "baba_cond": "重"}
    pts, details = scoring.eval_wet_aptitude(h, race_context, _cfg(same_surface_only=True))
    assert pts == 2.0
    assert details
    assert "道悪の方が良い" in details[0]
    assert "同surfaceのみ" in details[0]


def test_same_surface_only_drops_turf_runs_when_today_is_dirt():
    # 当日ダートのとき、芝の走 (稍重1着) が集計から除外されベスト道悪が消える
    h = {"hist": [
        _run("ダ", "良", "4"),
        _run("芝", "稍重", "1"),
    ]}
    race_context = {"type": "ダート", "baba_cond": "重"}

    # same_surface_only=False なら芝の稍重1着が拾われ「道悪の方が良い」+2.0
    pts_mixed, _ = scoring.eval_wet_aptitude(h, race_context, _cfg(same_surface_only=False))
    assert pts_mixed == 2.0

    # same_surface_only=True では芝走が除外され、ダートのwet実績が無く比較不能
    pts_same, details_same = scoring.eval_wet_aptitude(h, race_context, _cfg(same_surface_only=True))
    assert pts_same == 0.0
    assert details_same == []


# ── _ml_features の wet_match は same_surface_only の影響を受けない ────────

def test_ml_features_wet_match_unaffected_by_same_surface_only():
    h = {
        "hist": [
            _run("ダ", "稍重", "3"),
            _run("ダ", "稍重", "5"),
            _run("芝", "良", "1"),
        ],
        "sex_age": "牡3",
        "kg": "57.0",
        "odds": 10.0,
        "interval_days": 28,
    }
    race_context = {"type": "芝", "baba_cond": "重"}

    f_mixed = scoring._ml_features(h, race_context, None, _cfg(same_surface_only=False))
    f_same = scoring._ml_features(h, race_context, None, _cfg(same_surface_only=True))
    assert f_mixed["wet_match"] == f_same["wet_match"]
    assert f_mixed["wet_match"] == -1.0


# ── backtest_wet.classify_gap (純関数) ──────────────────────────────────

def _p(track_type, condition, rank):
    return {"track_type": track_type, "condition": condition, "rank": rank}


def test_classify_gap_better():
    prior4 = [_p("芝", "稍", 1), _p("芝", "良", 5)]
    assert classify_gap(prior4, "芝", same_surface=False) == "better"


def test_classify_gap_worse():
    prior4 = [_p("芝", "稍", 5), _p("芝", "良", 1)]
    assert classify_gap(prior4, "芝", same_surface=False) == "worse"


def test_classify_gap_neutral():
    prior4 = [_p("芝", "稍", 3), _p("芝", "良", 2)]
    assert classify_gap(prior4, "芝", same_surface=False) == "neutral"


def test_classify_gap_no_signal_when_only_wet_or_only_dry():
    assert classify_gap([_p("芝", "稍", 1)], "芝", same_surface=False) == "no_signal"
    assert classify_gap([_p("芝", "良", 1)], "芝", same_surface=False) == "no_signal"
    assert classify_gap([], "芝", same_surface=False) == "no_signal"
    # rank が None の走は除外される
    assert classify_gap([_p("芝", "稍", None), _p("芝", "良", 1)], "芝",
                         same_surface=False) == "no_signal"


def test_classify_gap_same_surface_excludes_other_track():
    # ダートの稍1着とダートの良5着だけなら same_surface=True/当日芝 では両方除外され no_signal
    prior4 = [_p("ダート", "稍", 1), _p("ダート", "良", 5)]
    assert classify_gap(prior4, "ダート", same_surface=False) == "better"
    assert classify_gap(prior4, "芝", same_surface=True) == "no_signal"
    # 当日と同じ track_type (ダート) なら same_surface=True でも結果は変わらない
    assert classify_gap(prior4, "ダート", same_surface=True) == "better"
