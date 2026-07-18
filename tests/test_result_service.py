import sqlite3

import pytest

from api.logging_store import LoggingStore
from api.result_service import (
    ResultNotReady,
    fetch_and_save_result,
    fetch_and_save_win5_result,
    normalize_race_date,
    parse_result_html,
    parse_win5_result_html,
    result_url_candidates,
    sync_results_for_date,
)


HTML = """
<html><body>
<h1><span class="opt">2026年7月11日（土曜）2回東京1日 1レース</span></h1>
<div id="race_result"><table><thead><tr><th>着順</th><th>馬番</th><th>馬名</th></tr></thead><tbody>
<tr><td class="place">1</td><td class="num">13</td><td class="horse">勝馬</td></tr>
<tr><td class="place">2</td><td class="num">12</td><td class="horse">二着馬</td></tr>
<tr><td class="place">取消</td><td class="num">8</td><td class="horse">取消馬</td></tr>
</tbody></table></div>
<div class="refund_unit"><ul>
<li class="win"><div class="line"><div class="num">13</div><div class="yen">530円</div></div></li>
<li class="place"><div class="line"><div class="num">13</div><div class="yen">150円</div></div>
<div class="line"><div class="num">12</div><div class="yen">110円</div></div></li>
</ul></div></body></html>
"""


def test_parse_official_result_and_payouts():
    result = parse_result_html(HTML, source_url="fixture://result")
    assert result["race_id"] == "20260711:東京:01"
    assert result["rows"][0]["horse_id"] == "20260711:東京:01:13"
    assert result["rows"][0]["finish_position"] == 1
    assert result["rows"][0]["win_payout"] == 530
    assert result["rows"][0]["final_win_odds"] == 5.3
    assert result["rows"][1]["place_payout"] == 110
    assert result["rows"][2]["finish_position"] is None
    assert "non_numeric_finish_status" in result["rows"][2]["data_quality_flags"]


def test_missing_result_is_retryable():
    with pytest.raises(ResultNotReady):
        parse_result_html("<html><body>集計中</body></html>")


def test_result_url_is_resolved_from_card_link_not_checksum_replacement():
    card_url = ("https://www.jra.go.jp/JRADB/accessD.html?"
                "CNAME=pw01dde0105202604010120260711/6E")
    result_url = ("https://www.jra.go.jp/JRADB/accessS.html?"
                  "CNAME=pw01sde0105202604010120260711/2A")
    other_race = ("https://www.jra.go.jp/JRADB/accessS.html?"
                  "CNAME=pw01sde0105202604010220260711/99")
    card_html = f'<a href="{other_race}">2R</a><a href="{result_url}">結果</a>'

    assert result_url_candidates(card_url, card_html=card_html) == [result_url]


def test_fetch_uses_official_result_link_from_card(monkeypatch, tmp_path):
    card_url = ("https://www.jra.go.jp/JRADB/accessD.html?"
                "CNAME=pw01dde0105202604010120260711/6E")
    result_url = ("https://www.jra.go.jp/JRADB/accessS.html?"
                  "CNAME=pw01sde0105202604010120260711/2A")
    card_html = f'<html><a href="{result_url}">レース結果</a></html>'
    calls = []

    class Response:
        def __init__(self, url, text):
            self.url = url
            self.text = text
            self.encoding = None

        def raise_for_status(self):
            return None

    def fake_get(url, **_kwargs):
        calls.append(url)
        return Response(url, card_html if url == card_url else HTML)

    monkeypatch.setattr("api.result_service.requests.get", fake_get)
    result = fetch_and_save_result(card_url, store=LoggingStore(tmp_path / "fetch.db"))

    assert calls == [card_url, result_url]
    assert result["race_id"] == "20260711:東京:01"
    assert result["save_stats"]["inserted"] == 3


def test_sync_results_for_monitored_date(monkeypatch, tmp_path):
    store = LoggingStore(tmp_path / "sync.db")
    race_id = "20260711:東京:01"
    store.save_monitor_state("東京_1", {"url": "https://example.test/card"}, race_id=race_id)

    def fake_fetch(url, *, store, timeout):
        assert url == "https://example.test/card"
        return {"race_id": race_id, "save_stats": {"inserted": 12}}

    monkeypatch.setattr("api.result_service.fetch_and_save_result", fake_fetch)
    # T55: sync_results_for_date also best-effort fetches the day's WIN5 payout.
    # Stub it here so this (pre-existing) test never touches the network.
    monkeypatch.setattr("api.result_service.fetch_and_save_win5_result",
                         lambda date, *, store, timeout=20: {"already_stored": False, "payout_yen": None,
                                                              "hit_ticket_count": None, "carryover_flag": False})
    result = sync_results_for_date("2026-07-11", store=store, max_workers=1)

    assert result["date"] == "20260711"
    assert (result["sources"], result["synced"], result["failed"]) == (1, 1, 0)
    assert result["win5_result"]["fetched"] is True


def test_sync_results_for_date_is_fail_soft_when_win5_fetch_fails(monkeypatch, tmp_path):
    # T55: a WIN5-fetch failure (network error, page not yet published, ...) must
    # not affect the official race-result sync -- it only shows up as fetched=False.
    store = LoggingStore(tmp_path / "sync-win5-fail.db")

    monkeypatch.setattr("api.result_service.fetch_and_save_win5_result",
                         lambda date, *, store, timeout=20: (_ for _ in ()).throw(ResultNotReady("not published yet")))
    result = sync_results_for_date("2026-07-11", store=store, max_workers=1)

    assert result["sources"] == 0
    assert result["win5_result"]["fetched"] is False
    assert "not published yet" in result["win5_result"]["error"]


# ─── WIN5 payout (T55) ───────────────────────────────────────────────────────
# Fragments trimmed from real netkeiba pages fetched during implementation
# (race.netkeiba.com/top/win5.html?date=20260712 and ...date=20260201), kept
# under the project's netkeiba access budget (one fetch per page, used only to
# derive these fixtures).

WIN5_HIT_HTML = """
<html><body>
<h3 class="TitleHeading"><span class="TitleHeadingWin">2026年07月12日のWIN5</span></h3>
<div class="WIN5_AllResult">
<table class="RaceCommon_Table Win5_Table"><tbody><tr>
<th class="W136">発売票数</th><td>7,236,518票</td>
<th class="W136">発売金額</th><td>7億2365万1800円</td>
</tr></tbody></table>
<table class="RaceCommon_Table Win5_Table"><tbody>
<tr><th>的中馬番</th><td>
<div class="Win5_UmabanWrap fc"><ul>
<li class="w5 Waku8">10</li><li class="w5 Waku8">11</li><li class="w5 Waku6">10</li>
<li class="w5 Waku6">6</li><li class="w5 Waku6">11</li>
</ul></div>
</td></tr>
<tr class="Result"><th>払戻金</th><td>641万2100円</td></tr>
<tr class="Result"><th>的中票数</th><td>79票</td></tr>
</tbody></table>
</div>
</body></html>
"""

WIN5_CARRYOVER_HTML = """
<html><body>
<h3 class="TitleHeading"><span class="TitleHeadingWin">2026年02月01日のWIN5</span></h3>
<div class="WIN5_AllResult">
<table class="RaceCommon_Table Win5_Table"><tbody><tr>
<th class="W136">発売票数</th><td>7,712,932票</td>
<th class="W136">発売金額</th><td>7億7129万3200円</td>
</tr></tbody></table>
<table class="RaceCommon_Table Win5_Table"><tbody>
<tr><th>的中馬番</th><td>
<div class="Win5_UmabanWrap fc"><ul>
<li class="w5 Waku2">2</li><li class="w5 Waku8">14</li><li class="w5 Waku4">6</li>
<li class="w5 Waku7">14</li><li class="w5 Waku1">2</li>
</ul></div>
</td></tr>
<tr class="Result"><th>払戻金</th><td>-円</td></tr>
<tr class="Result"><th>的中票数</th><td>0票</td></tr>
</tbody></table>
<table summary="結果" class="RaceCommon_Table Win5_Table">
<tr class="Header"><th>次週への<br/>キャリーオーバー</th><td>5億3990万5240円</td></tr>
</table>
</div>
</body></html>
"""


def test_parse_win5_result_hit_day():
    result = parse_win5_result_html(WIN5_HIT_HTML, source_url="fixture://win5-hit")
    assert result["race_date"] == "20260712"
    assert result["payout_yen"] == 6_412_100
    assert result["hit_ticket_count"] == 79
    assert result["carryover_flag"] is False
    assert result["carryover_amount"] is None
    assert result["winning_numbers"] == [10, 11, 10, 6, 11]


def test_parse_win5_result_carryover_day():
    result = parse_win5_result_html(WIN5_CARRYOVER_HTML, source_url="fixture://win5-carryover")
    assert result["race_date"] == "20260201"
    assert result["payout_yen"] is None
    assert result["hit_ticket_count"] == 0
    assert result["carryover_flag"] is True
    assert result["carryover_amount"] == 539_905_240
    assert result["winning_numbers"] == [2, 14, 6, 14, 2]


def test_parse_win5_result_not_yet_published_is_retryable():
    with pytest.raises(ResultNotReady):
        parse_win5_result_html("<html><body>WIN5対象レース発表前です</body></html>", race_date="20260719")


def test_fetch_and_save_win5_result_persists_and_skips_repeat_fetch(monkeypatch, tmp_path):
    store = LoggingStore(tmp_path / "win5-fetch.db")
    calls = []

    class Response:
        status_code = 200
        text = WIN5_HIT_HTML
        apparent_encoding = "utf-8"

        def raise_for_status(self):
            return None

    def fake_get(url, headers, timeout):
        calls.append(url)
        return Response()

    monkeypatch.setattr("api.result_service.requests.get", fake_get)
    monkeypatch.setattr("api.result_service.time.sleep", lambda _s: None)

    first = fetch_and_save_win5_result("2026-07-12", store=store)
    assert first["already_stored"] is False
    assert first["payout_yen"] == 6_412_100
    assert calls == ["https://race.netkeiba.com/top/win5.html?date=20260712"]

    stored = store.get_win5_result("20260712")
    assert stored["payout_yen"] == 6_412_100
    assert stored["hit_ticket_count"] == 79
    assert stored["source_url"] == calls[0]

    # A second call for the same date must not hit the network again (T55 §3.1:
    # "1開催日=1リクエスト厳守").
    second = fetch_and_save_win5_result("2026-07-12", store=store)
    assert second["already_stored"] is True
    assert calls == ["https://race.netkeiba.com/top/win5.html?date=20260712"]


@pytest.mark.parametrize("value", ["2026/07/11", "2026-02-30", ""])
def test_invalid_result_date(value):
    with pytest.raises(ValueError):
        normalize_race_date(value)


def test_result_upsert_keeps_correction_history(tmp_path):
    store = LoggingStore(tmp_path / "results.db")
    parsed = parse_result_html(HTML)
    first = store.save_race_results(parsed["rows"])
    same = store.save_race_results(parsed["rows"])
    corrected_html = HTML.replace("<body>", "<body>訂正 ").replace(
        '<td class="place">2</td><td class="num">12</td>',
        '<td class="place">3</td><td class="num">12</td>')
    corrected = parse_result_html(corrected_html)
    changed = store.save_race_results(corrected["rows"])
    assert first == {"inserted": 3, "updated": 0, "unchanged": 0}
    assert same == {"inserted": 0, "updated": 0, "unchanged": 3}
    assert changed == {"inserted": 0, "updated": 3, "unchanged": 0}
    with sqlite3.connect(store.db_path) as conn:
        status, position = conn.execute(
            "SELECT official_status,finish_position FROM race_results WHERE horse_id LIKE '%:12'"
        ).fetchone()
        revisions = conn.execute("SELECT count(*) FROM race_result_revisions").fetchone()[0]
    assert (status, position) == ("corrected", 3)
    assert revisions == 6


def test_prediction_result_match_summary(tmp_path):
    store = LoggingStore(tmp_path / "match.db")
    run = store.start_run(app_name="ev_monitor")
    race_id = "20260711:東京:01"
    store.save_predictions(run, [
        {"race_id": race_id, "horse_id": f"{race_id}:13"},
        {"race_id": race_id, "horse_id": f"{race_id}:12"},
    ])
    rows = parse_result_html(HTML)["rows"]
    store.save_race_results(rows[:1])
    assert store.result_match_summary(race_id) == {"predicted": 2, "matched": 1, "unmatched": 1}
