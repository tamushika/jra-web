from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "api"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(API_DIR))

import index as api_index  # noqa: E402
import jra_ev  # noqa: E402
import jra_win5  # noqa: E402
import scoring  # noqa: E402


JUMP_FIXTURE = ROOT / "tests" / "fixtures" / "t48" / "jump_race_20260719_kokura1.html"
JUMP_URL = (
    "https://www.jra.go.jp/JRADB/accessD.html?"
    "CNAME=pw01dde0110202602080120260719%2FCA"
)


class _Response:
    def __init__(self, content):
        self.content = content
        self.encoding = None

    @property
    def text(self):
        return self.content.decode(self.encoding or "cp932", errors="replace")


def test_real_jra_jump_fixture_is_fail_closed(monkeypatch):
    raw = JUMP_FIXTURE.read_bytes()
    monkeypatch.setattr(api_index.requests, "get", lambda *_args, **_kwargs: _Response(raw))

    result = api_index.analyze_race_url(JUMP_URL)

    assert result["success"] is True
    assert result["analysis_excluded"] is True
    assert result["race_type"] == "障害"
    assert result["dist_val"] == 2860
    assert result["horses"] == []
    assert "障害レースは解析対象外" in result["analysis_message"]


def test_jump_returner_in_results_column_keeps_flat_race():
    # 2026-07-26 中京6R 木曽川特別 (芝2000m 平地): 出走馬の戦績欄に
    # 「新潟 障害未勝利」があり、全文検索だと障害と誤判定される実ページ。
    from bs4 import BeautifulSoup
    raw = (ROOT / "tests" / "fixtures" / "t48"
           / "flat_race_20260726_chukyo6_jump_returner.html").read_bytes()
    soup = BeautifulSoup(raw.decode("cp932", errors="replace"), "html.parser")
    page_text = soup.get_text()
    assert "障害" in page_text  # 前提: 障害帰りの馬がいる
    header = api_index._race_header_text(soup, page_text)
    assert "障害" not in header
    assert api_index.detect_race_type(header, "木曽川特別", 2000) == "芝"


def test_race_header_text_keeps_jump_detection_on_real_fixture():
    from bs4 import BeautifulSoup
    raw = JUMP_FIXTURE.read_bytes()
    soup = BeautifulSoup(raw.decode("cp932", errors="replace"), "html.parser")
    header = api_index._race_header_text(soup, soup.get_text())
    assert api_index.detect_race_type(header, "", 2860) == "障害"


def test_race_header_text_falls_back_to_page_text():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup("<html><body>no header</body></html>", "html.parser")
    assert api_index._race_header_text(soup, "FULL TEXT") == "FULL TEXT"


def test_jump_name_in_horse_column_does_not_misclassify_flat_race():
    page = "東京 芝1600 サンライズジャンプ"
    assert api_index.detect_race_type(page, "3歳未勝利", 1600) == "芝"
    assert api_index.detect_race_type("コース：芝", "京都ジャンプ", 3170) == "障害"
    assert api_index.detect_race_type("コース：芝", "中山J・GII", 4250) == "障害"


def test_long_distance_warning_does_not_block_flat_classification():
    warnings = []
    assert api_index.detect_race_type(
        "コース：芝", "テスト競走", 3700, warnings.append) == "芝"
    assert warnings and "取りこぼし疑い" in warnings[0]
    warnings.clear()
    assert api_index.detect_race_type(
        "コース：芝", "ステイヤーズ", 3600, warnings.append) == "芝"
    assert warnings == []


def test_nar_venue_code_never_enters_jra_win5_url_map(monkeypatch):
    html = b'<a href="/JRADB/accessD.html?CNAME=pw01dde0130202601010920260101/AA">x</a>'
    monkeypatch.setattr(api_index.requests, "get", lambda *_args, **_kwargs: _Response(html))
    races = [{"idx": 0, "venue": "東京", "race_num": 9}]
    assert api_index._find_win5_urls(races, "20260101") == {}


def test_ev_jump_race_is_not_monitored(monkeypatch, capsys):
    monkeypatch.setattr(jra_ev, "analyze_race_url", lambda *_args, **_kwargs: {
        "analysis_excluded": True, "race_type": "障害", "venue": "小倉",
        "race_num": 1, "race_info": "【小倉 1R】障害2860m",
    })
    assert jra_ev.analyze_one("fixture://jump", dict(jra_ev.STATE["params"])) is None
    assert "障害レースのため監視対象外: 小倉1R" in capsys.readouterr().out


def _ml_model():
    return {
        "features": ["n_prior"], "mean": [0.0], "sd": [1.0],
        "coef": [2.0], "display_scale": 1.0,
    }


def test_debut_ml_and_place_are_none_with_explicit_details(monkeypatch):
    horse = {"name": "デビュー", "hist": []}
    monkeypatch.setattr(scoring, "load_ml_model", _ml_model)
    score, details = scoring.compute_score_ml(horse, {}, None, {})
    assert score is None
    assert any("初出走" in detail for detail in details)

    monkeypatch.setattr(
        scoring, "load_place_model",
        lambda: {
            "features": [name for name in scoring._ML_LABELS if name != "ln_odds"],
            "mean": [0.0] * (len(scoring._ML_LABELS) - 1),
            "scale": [1.0] * (len(scoring._ML_LABELS) - 1),
            "coef": [0.0] * (len(scoring._ML_LABELS) - 1),
            "intercept": 0.0,
            "calibration": {"method": "platt", "slope": 1.0, "intercept": 0.0},
        },
    )
    probability, details = scoring.compute_place_prob(horse, {}, None, {})
    assert probability is None
    assert any("初出走" in detail for detail in details)


def test_non_debut_ml_score_is_unchanged(monkeypatch):
    horse = {"hist": [{"rank": "4"}]}
    monkeypatch.setattr(scoring, "load_ml_model", _ml_model)
    monkeypatch.setattr(scoring, "_ml_features", lambda *_args: {"n_prior": 1.0})
    assert scoring.compute_score_ml(horse, {}, None, {})[0] == 2.0


def test_debut_majority_warning_excludes_new_races():
    horses = [{"hist": []}, {"hist": []}, {"hist": [{"rank": "5"}]}]
    warning = scoring.debut_field_warning(horses, "未勝利", "東京1R")
    assert warning and "2/3頭" in warning and "パース異常" in warning
    assert scoring.debut_field_warning(horses, "新馬", "東京5R") is None
    assert scoring.debut_field_warning(
        [{"hist": []}, {"hist": [{"rank": "5"}]}], "未勝利") is None


def test_ev_debut_is_excluded_from_probability_and_logged(monkeypatch, capsys):
    monkeypatch.setattr(jra_ev, "analyze_race_url", lambda *_args, **_kwargs: {
        "venue": "東京", "race_type": "芝", "dist_val": 1600,
        "race_class": "未勝利", "race_info": "【東京 1R】芝1600m 10:00発走",
        "baba_cond": "良", "horses": [
            {"num": 1, "name": "デビュー", "hist": [], "odds": 5.0, "pop": 2},
            {"num": 2, "name": "既走馬", "hist": [{"rank": "4"}], "odds": 3.0, "pop": 1},
        ],
    })
    monkeypatch.setattr(jra_ev.scoring, "load_score_weights", lambda *_args: {})
    monkeypatch.setattr(jra_ev.scoring, "load_factor_table", lambda *_args: {})
    monkeypatch.setattr(
        jra_ev.scoring, "compute_score_ml",
        lambda horse, *_args: ((None, ["初出走"]) if not horse["hist"] else (1.5, [])),
    )
    monkeypatch.setattr(jra_ev, "LoggingStore", None)
    rec = jra_ev.analyze_one("fixture://flat", dict(jra_ev.STATE["params"]))
    assert rec["horses"][0]["ml_score"] is None
    assert rec["horses"][0]["ev"] is None
    assert rec["horses"][0]["picked"] is False
    # 2026-09-05 中山5R不具合対応: scored=1頭はML_MIN_SCORED_HORSES(2)未満のため
    # 縮退ガードが発動し、softmax(=1.0)を確率として採用しなくなった。
    assert rec["horses"][1]["win_prob"] is None
    assert rec["horses"][1]["ev"] is None
    assert rec["horses"][1]["picked"] is False
    assert rec["ml_coverage"] == {"scored": 1, "total": 2, "ok": False}
    out = capsys.readouterr().out
    assert "初出走のためML/EV対象外: デビュー" in out
    assert "MLスコア付き馬が不足のためEV対象外: 1/2頭" in out


def test_win5_keeps_debut_runner_and_other_scores(monkeypatch):
    result = {
        "venue": "東京", "race_type": "芝", "dist_val": 1600,
        "race_class": "未勝利", "race_info": "東京1R", "horses": [
            {"num": 1, "name": "デビュー", "hist": []},
            {"num": 2, "name": "既走A", "hist": [{"rank": "4"}]},
            {"num": 3, "name": "既走B", "hist": [{"rank": "6"}]},
        ],
    }
    monkeypatch.setattr(jra_win5.scoring, "load_score_weights", lambda *_args: {"use_ml": True})
    monkeypatch.setattr(jra_win5.scoring, "load_factor_table", lambda *_args: {})
    monkeypatch.setattr(
        jra_win5.scoring, "compute_score_ml",
        lambda horse, *_args: ((None, ["初出走"]) if not horse["hist"] else (float(horse["num"]), [])),
    )
    monkeypatch.setattr(jra_win5, "get_upset", lambda *_args: ("B", 0.0))
    monkeypatch.setattr(jra_win5, "log_race_prediction", lambda *_args, **_kwargs: None)
    output = jra_win5._analyze_one_body(0, "fixture://flat", result)
    assert len(output["horses"]) == 3
    assert [horse["score"] for horse in output["horses"]] == [None, 2.0, 3.0]
