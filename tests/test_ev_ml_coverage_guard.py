"""2026-09-05 中山5R メイクデビュー不具合対応の受け入れテスト。

対応する仕様 (口頭仕様、docs化なし):
  A. jra_ev.compute_picks に ML縮退ガードを追加。
     scored (ml_score有、非scratched) が母数 (非scratched) の過半かつ2頭以上でなければ
     softmaxを計算せず全馬 win_prob/ev/picked を None/None/False のままにする。
  B. api/index.get_race_class で「メイクデビュー」を新馬判定に含める
     (オープン判定より前)。
"""
from datetime import datetime, timezone, timedelta

import pytest

import jra_ev
from api.index import get_race_class


JST = timezone(timedelta(hours=9))
PARAMS = {"ev_threshold": 1.1, "max_odds": 50.0, "min_prob": 0.02, "wide_overlay": None}


def _horse(num, odds=None, pop=None, ml_score=None, scratched=False, web_score=None):
    return {
        "num": num, "name": f"馬{num}", "odds": odds, "pop": pop,
        "ml_score": ml_score, "scratched": scratched, "web_score": web_score,
    }


# ─── (a) 11頭・ml_score1頭のみ → 全馬None、n_picked==0 ─────────────────────────

def test_single_scored_horse_degrades_to_no_picks():
    horses = [_horse(i + 1, odds=str(3.0 + i), pop=i + 1) for i in range(11)]
    # 9番相当: 履歴1行だけでml_scoreが付いた馬 (odds 35.2で通常なら1.0が付き即pick)
    horses[8]["ml_score"] = -4.8
    horses[8]["odds"] = "35.2"

    n_picked = jra_ev.compute_picks(horses, PARAMS)

    assert n_picked == 0
    for h in horses:
        assert h["win_prob"] is None
        assert h["ev"] is None
        assert h["picked"] is False


# ─── (b) 16頭・ml_score13頭 → 13頭に確率、3頭はNone、pickedは従来どおり ──────────

def test_sufficient_coverage_keeps_existing_softmax_behavior(monkeypatch):
    horses = [_horse(i + 1, odds="2.0", pop=i + 1, ml_score=1.0) for i in range(13)]
    horses += [_horse(i + 14, odds="10.0", pop=i + 14, ml_score=None) for i in range(3)]
    # 先頭馬だけ高確率・低オッズならず、ev>=1.1になるようoddsを調整
    horses[0]["odds"] = "3.0"

    fake_probs = [0.5] + [0.5 / 12] * 12

    monkeypatch.setattr(jra_ev.scoring, "win_probs_from_ml_scores",
                         lambda scores: fake_probs if len(scores) == 13 else None)

    n_picked = jra_ev.compute_picks(horses, PARAMS)

    scored_horses = horses[:13]
    unscored_horses = horses[13:]

    # win_probはcompute_picks内でround(p, 4)されるため、丸め誤差込みでほぼ1.0
    assert sum(h["win_prob"] for h in scored_horses) == pytest.approx(1.0, abs=1e-3)
    for h in unscored_horses:
        assert h["win_prob"] is None
        assert h["ev"] is None
        assert h["picked"] is False

    # horse[0]: p=0.5, odds=3.0 -> ev=1.5 >=1.1, odds<=50, p>=0.02 -> picked
    assert horses[0]["win_prob"] == pytest.approx(0.5)
    assert horses[0]["ev"] == pytest.approx(1.5)
    assert horses[0]["picked"] is True

    # horse[1..12]: p=0.5/12, odds=2.0 -> ev far below 1.1 -> not picked
    for h in horses[1:13]:
        assert h["ev"] == pytest.approx(round(0.5 / 12 * 2.0, 2))
        assert h["picked"] is False

    assert n_picked == 1


# ─── (c) 10頭・ml_score3頭 (30%) → ガード発動で全馬None ────────────────────────

def test_below_ratio_threshold_degrades_to_no_picks():
    horses = [_horse(i + 1, odds="5.0", pop=i + 1) for i in range(10)]
    for i in range(3):
        horses[i]["ml_score"] = 1.0

    n_picked = jra_ev.compute_picks(horses, PARAMS)

    assert n_picked == 0
    for h in horses:
        assert h["win_prob"] is None
        assert h["ev"] is None
        assert h["picked"] is False


# ─── (d) 10頭中4頭scratched・残6頭中4頭にml_score → 母数6で4/6>=0.5、確率が付く ──

def test_scratched_horses_excluded_from_denominator(monkeypatch):
    horses = [_horse(i + 1, odds="4.0", pop=i + 1) for i in range(10)]
    for i in range(4):
        horses[i]["scratched"] = True  # ml_score無し、母数から除外
    for i in range(4, 8):
        horses[i]["ml_score"] = 1.0  # 非scratched6頭中4頭に付与
    # 残り2頭 (index 8,9) はml_score無し (非scratched)

    monkeypatch.setattr(jra_ev.scoring, "win_probs_from_ml_scores",
                         lambda scores: [1.0 / len(scores)] * len(scores) if scores else None)

    n_picked = jra_ev.compute_picks(horses, PARAMS)

    scored_horses = horses[4:8]
    for h in scored_horses:
        assert h["win_prob"] == pytest.approx(0.25)
    for h in horses[:4] + horses[8:]:
        assert h["win_prob"] is None
        assert h["ev"] is None
        assert h["picked"] is False
    # n_picked は EV=0.25*4.0=1.0 <1.1 のため0
    assert n_picked == 0


# ─── (e) get_race_class: メイクデビュー→新馬、オープン判定より優先 ──────────────

def test_get_race_class_recognizes_make_debut_before_open():
    assert get_race_class("メイクデビュー中山") == "新馬"
    assert get_race_class("2歳新馬") == "新馬"
    assert get_race_class("○○ステークス(L)") == "オープン"
    assert get_race_class("3歳未勝利") == "未勝利"


# ─── (f) analyze_one の監視レコードに ml_coverage が入る ────────────────────────

def test_analyze_one_record_includes_ml_coverage(monkeypatch):
    raw_horses = [
        {"num": index + 1, "name": f"馬{index + 1}", "jock": "騎手",
         "odds": 3.0 + index, "pop": index + 1, "score": 1.0}
        for index in range(11)
    ]

    monkeypatch.setattr(jra_ev, "LoggingStore", None)
    monkeypatch.setattr(jra_ev, "analyze_race_url", lambda *args, **kwargs: {
        "venue": "中山", "race_type": "芝", "dist_val": 1600,
        "race_class": "新馬", "baba_cond": "良",
        "race_info": "【中山 5R】芝1600m 10:20発走　メイクデビュー",
        "horses": raw_horses,
    })
    monkeypatch.setattr(jra_ev.scoring, "load_score_weights", lambda *args: {"version": 1})
    monkeypatch.setattr(jra_ev.scoring, "load_factor_table", lambda *args: {})
    # 9番相当の1頭だけmlスコアが付く (新馬戦で履歴1行だけ検出されたケースを模す)
    monkeypatch.setattr(
        jra_ev.scoring, "compute_score_ml",
        lambda h, rc, factor_table, cfg: (-4.8 if h.get("num") == 9 else None, {}))
    monkeypatch.setattr(jra_ev.scoring, "is_debut_horse", lambda h: h.get("num") != 9)
    monkeypatch.setattr(jra_ev, "_data_version", lambda: "test")

    base = datetime(2026, 9, 5, 10, 20, tzinfo=JST)
    result = jra_ev.analyze_one(
        "https://example.invalid/race", PARAMS, base_date=base)

    assert result is not None
    assert result["ml_coverage"] == {"scored": 1, "total": 11, "ok": False}
    for h in result["horses"]:
        assert h["win_prob"] is None
        assert h["picked"] is False
    assert result["n_picked"] == 0
