import math

from bs4 import BeautifulSoup

from api import index, scoring


def _horse_row(weight_text):
    return BeautifulSoup(f"""
        <table><tr><td class="horse"><div class="cell weight">{weight_text}</div></td></tr></table>
    """, "html.parser").select_one("tr")


def test_current_weight_parser_and_unpublished_fallback():
    assert index.parse_current_weight(_horse_row("464kg (-4)")) == (464, -4)
    assert index.parse_current_weight(_horse_row("500kg (+12)")) == (500, 12)
    assert index.parse_current_weight(_horse_row("448kg (初出走)")) == (448, None)
    assert index.parse_current_weight(_horse_row("馬体重未発表")) == (None, None)


def test_live_weight_cache_reuses_first_published_value():
    index._LIVE_WEIGHT_CACHE.clear()
    first = [{"num": 1, "current_weight": 478, "weight_change": 6}]
    index._apply_live_weight_cache(first, "https://example.test/?CNAME=race01/AA")
    assert first[0]["weight_source"] == "jra_live"

    later = [{"num": 1, "current_weight": None, "weight_change": None}]
    index._apply_live_weight_cache(later, "https://example.test/?CNAME=race01/AA")
    assert later[0]["current_weight"] == 478
    assert later[0]["weight_change"] == 6
    assert later[0]["weight_source"] == "jra_live_cache"


def test_pci_is_parsed_from_existing_jra_history_cell_without_extra_request():
    cell = BeautifulSoup("""
        <td class="past p2">2026年6月7日 東京 未勝利 4 着 16 頭 5 番人気
        55.0 kg 1600芝 1:35.2 良 470 kg 4 4 3F 34.8 テスト馬(0.4)</td>
    """, "html.parser").select_one("td")
    parsed = index.fetch_history_data(cell, "https://example.test", "テスト", "簡易", False)
    expected = scoring.compute_pci(95.2, 34.8, 1600)
    assert parsed["time_sec"] == 95.2
    assert parsed["agari_3f"] == 34.8
    assert parsed["pci"] == expected
    assert parsed["track_type"] == "芝"


def test_attach_live_pace_features_matches_training_formula():
    horses = []
    styles = []
    for offset in range(5):
        pcis = [50.0 + offset, 52.0 + offset]
        devs = [pci - scoring.PCI_TRACK_MEAN["芝"] for pci in pcis]
        styles.append(sum(devs) / len(devs))
        horses.append({"hist": [
            {"place": "東京", "track_type": "芝", "distance": 1600,
             "condition": "良", "race_class": "未勝利", "pci": pci}
            for pci in pcis
        ]})

    predicted = scoring.attach_live_pace_features(horses)
    assert math.isclose(predicted, sum(styles) / len(styles))
    for horse, style in zip(horses, styles):
        assert math.isclose(horse["_pace_fit"], style * predicted)
        assert horse["pace_fit_source"] == "jra_history"


def test_ml_weight_prefers_current_and_flags_previous_run_fallback():
    cfg = scoring.load_score_weights(str(index.get_base_dir()), "win5_weights.json")
    base = {
        "hist": [{"weight": "456 kg"}], "sex_age": "牡3", "kg": "57.0",
        "odds": 10.0, "interval_days": 28,
    }
    current = {**base, "current_weight": 482}
    features = scoring._ml_features(current, {}, None, cfg)
    assert features["weight"] == 482
    assert current["weight_source"] == "jra_live"

    fallback = {**base, "current_weight": None}
    features = scoring._ml_features(fallback, {}, None, cfg)
    assert features["weight"] == 456
    assert fallback["weight_source"] == "previous_run"
