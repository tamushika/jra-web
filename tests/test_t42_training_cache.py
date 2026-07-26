import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import requests

import t42_training_cache as t42


MEMBER_HTML = b"""
<html><body><table>
<tr>
  <td rowspan="2" class="Horse_Info">
    <span class="Horse_Name"><a href="/horse/2021106347/">Alpha</a></span>
  </td>
  <td class="Training_Day">2026/07/01</td><td>MihoW</td><td>Good</td>
  <td class="TrainingTimeData"><ul class="TrainingTimeDataList">
    <li>54.4 <span class="RapTime">(15.1)</span></li>
  </ul><div class="Comment_Cell"><p>paired</p></div></td>
  <td class="TrainingLoad">Strong</td><td class="Training_Critic">Good</td>
  <td class="Rank_B">B</td>
</tr>
<tr><td class="Training_Day">2026/06/24</td><td>Miho</td><td>Good</td>
  <td class="TrainingTimeData"><ul class="TrainingTimeDataList"><li>68.3</li></ul></td>
  <td class="TrainingLoad">Easy</td><td class="Training_Critic">OK</td>
  <td class="Rank_C">C</td></tr>
</table></body></html>
"""
PAYWALL_HTML = MEMBER_HTML.replace(
    b"</body>", b'<div class="Premium_Regist_Msg">register</div></body>'
)
INDEX_HTML = b'<a href="/race/202603020312/">12R</a>'
# Skeleton is present (Training_Day + TrainingTimeDataList) but every lap value
# is masked, as a limited trial tier might render it.
MASKED_HTML = b"""
<html><body><table>
<tr>
  <td rowspan="2" class="Horse_Info">
    <span class="Horse_Name"><a href="/horse/2021106347/">Alpha</a></span>
  </td>
  <td class="Training_Day">2026/07/01</td><td>MihoW</td><td>Good</td>
  <td class="TrainingTimeData"><ul class="TrainingTimeDataList">
    <li>**.* <span class="RapTime">(**.*)</span></li>
  </ul></td>
  <td class="TrainingLoad">-</td><td class="Training_Critic">-</td>
</tr>
</table></body></html>
"""


# db.netkeiba.com horse_training pages use race_table_01 tables with a
# 調教タイム header column (confirmed live 2026-07-26, Stage 0) and a pager
# such as 18件中1〜10件目.
HORSE_HTML = """
<html><head><meta http-equiv="Content-Type"
 content="text/html; charset=euc-jp"></head><body>
<a href="/horse/2021106347/">Alpha</a>
<div class="pager">18件中1〜10件目</div>
<table class="race_table_01 nk_tb_common">
<tr><th>日付</th><th>コース</th><th>調教タイム</th></tr>
<tr><td>2026/07/01</td><td>MihoW</td><td>54.4 (15.1) 39.3</td></tr>
</table></body></html>
""".encode("euc-jp")
HORSE_MASKED_HTML = """
<html><head><meta http-equiv="Content-Type"
 content="text/html; charset=euc-jp"></head><body>
<a href="/horse/2021106347/">Alpha</a>
<table class="race_table_01 nk_tb_common">
<tr><th>日付</th><th>コース</th><th>調教タイム</th></tr>
<tr><td>2026/07/01</td><td>MihoW</td><td>**.* (**.*)</td></tr>
</table></body></html>
""".encode("euc-jp")


class FakeResponse:
    def __init__(self, content=MEMBER_HTML, status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, url, timeout):
        self.calls.append((url, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def cookies(path: Path):
    path.write_text(json.dumps([
        {"name": "session", "value": "SECRET", "domain": ".netkeiba.com",
         "path": "/"}
    ]), encoding="utf-8")


def fetcher(tmp_path, session, **kwargs):
    cookie_path = tmp_path / "cookies.json"
    cookies(cookie_path)
    return t42.TrainingFetcher(
        cache_root=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.sqlite",
        cookies_path=cookie_path,
        interval=1.05,
        live=True,
        trial_acknowledged=True,
        session=session,
        **kwargs,
    )


def test_member_page_is_cached_and_cache_hit_uses_zero_http(tmp_path):
    session = FakeSession([FakeResponse()])
    archive = fetcher(tmp_path, session)
    target = t42.race_target("202603020312", ["2021106347"])

    first = archive.fetch(target)
    second = archive.fetch(target)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(session.calls) == 1
    assert first.path.read_bytes() == MEMBER_HTML
    assert first.sha256 == hashlib.sha256(MEMBER_HTML).hexdigest()


def test_paywall_is_recorded_but_never_cached(tmp_path):
    archive = fetcher(tmp_path, FakeSession([FakeResponse(PAYWALL_HTML)]))
    target = t42.race_target("202603020312")

    with pytest.raises(t42.T42Error, match="fetch rejected"):
        archive.fetch(target)

    assert not t42.cache_path_for(target, tmp_path / "raw").exists()
    with sqlite3.connect(tmp_path / "manifest.sqlite") as db:
        row = db.execute(
            "SELECT cache_path, sha256, error_code FROM fetch_manifest"
        ).fetchone()
    assert row == (None, None, "member_content_missing")


def test_masked_timing_values_are_never_cached(tmp_path):
    archive = fetcher(tmp_path, FakeSession([FakeResponse(MASKED_HTML)]))
    target = t42.race_target("202603020312")

    with pytest.raises(t42.T42Error, match="fetch rejected"):
        archive.fetch(target)

    assert not t42.cache_path_for(target, tmp_path / "raw").exists()
    with sqlite3.connect(tmp_path / "manifest.sqlite") as db:
        row = db.execute("SELECT error_code FROM fetch_manifest").fetchone()
    assert row == ("training_values_masked",)


def test_three_consecutive_paywalls_stop_for_session_refresh(tmp_path):
    session = FakeSession([FakeResponse(PAYWALL_HTML) for _ in range(3)])
    archive = fetcher(tmp_path, session)
    for suffix in ("01", "02"):
        with pytest.raises(t42.T42Error):
            archive.fetch(t42.race_target(f"2026030203{suffix}"))
    with pytest.raises(t42.StopAfterFailures, match="refresh"):
        archive.fetch(t42.race_target("202603020303"))


def test_expected_horse_coverage_is_fail_closed(tmp_path):
    archive = fetcher(tmp_path, FakeSession([FakeResponse()]))
    target = t42.race_target("202603020312", ["2021106347", "missinghorse"])
    with pytest.raises(t42.T42Error):
        archive.fetch(target)
    assert not t42.cache_path_for(target, tmp_path / "raw").exists()


def test_expected_horse_count_is_fail_closed(tmp_path):
    archive = fetcher(tmp_path, FakeSession([FakeResponse()]))
    target = t42.race_target("202603020312", expected_horse_count=2)
    with pytest.raises(t42.T42Error):
        archive.fetch(target)
    assert not t42.cache_path_for(target, tmp_path / "raw").exists()


def test_missing_cookie_file_has_clear_secret_free_error(tmp_path):
    archive = t42.TrainingFetcher(
        cache_root=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.sqlite",
        cookies_path=tmp_path / "absent.json",
        interval=1.05,
        live=True,
        trial_acknowledged=True,
        session=FakeSession([]),
    )
    with pytest.raises(t42.CookieConfigError, match="contents were not read"):
        archive.fetch(t42.race_target("202603020312"))


def test_cookie_value_is_not_in_failure_or_manifest(tmp_path):
    session = FakeSession([requests.ConnectionError("network down")])
    archive = fetcher(tmp_path, session, max_retries=1)
    with pytest.raises(t42.T42Error) as caught:
        archive.fetch(t42.race_target("202603020312"))
    assert "SECRET" not in str(caught.value)
    assert b"SECRET" not in (tmp_path / "manifest.sqlite").read_bytes()


def test_network_requires_both_live_gates(tmp_path):
    archive = t42.TrainingFetcher(
        cache_root=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.sqlite",
        cookies_path=tmp_path / "cookies.json",
        interval=1.05,
        live=False,
        trial_acknowledged=False,
        session=FakeSession([]),
    )
    with pytest.raises(t42.LiveGateError):
        archive.fetch(t42.race_target("202603020312"))


def test_sha_tampering_is_rejected_without_http(tmp_path):
    session = FakeSession([FakeResponse()])
    archive = fetcher(tmp_path, session)
    target = t42.race_target("202603020312")
    cached = archive.fetch(target)
    cached.path.write_bytes(b"tampered")
    with pytest.raises(t42.CacheIntegrityError, match="SHA-256"):
        archive.fetch(target)
    assert len(session.calls) == 1


def test_orphan_file_refuses_immutable_overwrite(tmp_path):
    target = t42.race_target("202603020312")
    path = t42.cache_path_for(target, tmp_path / "raw")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"orphan")
    archive = fetcher(tmp_path, FakeSession([FakeResponse()]))
    with pytest.raises(t42.CacheIntegrityError, match="orphaned"):
        archive.fetch(target)


def test_daily_limit_counts_request_attempts(tmp_path):
    archive = fetcher(
        tmp_path, FakeSession([FakeResponse(), FakeResponse()]), daily_limit=1,
    )
    archive.fetch(t42.race_target("202603020301"))
    with pytest.raises(t42.DailyLimitReached):
        archive.fetch(t42.race_target("202603020302"))


def test_five_consecutive_network_failures_stop(tmp_path):
    session = FakeSession([requests.ConnectionError("no") for _ in range(5)])
    archive = fetcher(
        tmp_path, session, max_retries=1, failure_threshold=5,
    )
    for number in range(1, 5):
        with pytest.raises(t42.T42Error):
            archive.fetch(t42.race_target(f"2026030203{number:02d}"))
    with pytest.raises(t42.StopAfterFailures, match="5 consecutive"):
        archive.fetch(t42.race_target("202603020305"))


def test_index_contract_and_resolution_are_cached(tmp_path):
    archive = fetcher(tmp_path, FakeSession([FakeResponse(INDEX_HTML)]))
    assert t42.resolve_day(archive, "20260704") == {("福島", 12): "202603020312"}
    assert t42.resolve_day(archive, "20260704") == {("福島", 12): "202603020312"}
    assert archive.network_requests == 1


def test_parser_extracts_skeleton_fields():
    records = t42.parse_training_records(MEMBER_HTML, source_key="202603020312")
    assert len(records) == 2
    assert records[0]["horse_id"] == "2021106347"
    assert records[0]["training_day"] == "2026/07/01"
    assert records[0]["laps"] == ["54.4 (15.1)"]
    assert records[0]["load"] == "Strong"
    assert records[0]["rank"] == "B"


def test_horse_training_page_is_cached_with_db_dom(tmp_path):
    session = FakeSession([FakeResponse(HORSE_HTML)])
    f = fetcher(tmp_path, session)
    page = f.fetch(t42.horse_target("2021106347"))
    assert page.body == HORSE_HTML
    assert (tmp_path / "raw" / "horse" / "2021106347.html").exists()
    # cache hit needs zero HTTP
    f.fetch(t42.horse_target("2021106347"))
    assert len(session.calls) == 1


def test_horse_training_masked_values_are_never_cached(tmp_path):
    session = FakeSession([FakeResponse(HORSE_MASKED_HTML)])
    f = fetcher(tmp_path, session)
    with pytest.raises(t42.T42Error):
        f.fetch(t42.horse_target("2021106347"))
    assert not (tmp_path / "raw" / "horse" / "2021106347.html").exists()


def test_horse_training_rejects_foreign_horse_page(tmp_path):
    # A page whose tables look right but that belongs to another horse must
    # not be cached under this horse's key.
    session = FakeSession([
        FakeResponse(HORSE_HTML.replace(b"2021106347", b"2019999999")),
    ])
    f = fetcher(tmp_path, session)
    with pytest.raises(t42.T42Error):
        f.fetch(t42.horse_target("2021106347"))
    assert not (tmp_path / "raw" / "horse" / "2021106347.html").exists()


def test_http_400_is_permanent_gap_and_does_not_trip_failure_stop(tmp_path):
    # 2022年の順延開催ではdb.netkeiba由来のrace_idをrace.netkeibaが400で拒否する。
    # 400の連なりで5連続失敗停止すると再実行デッドロックになるため、
    # 恒久ギャップとして記録して続行すること。
    responses = [FakeResponse(b"", status_code=400) for _ in range(6)]
    responses.append(FakeResponse())  # 7件目は正常ページ
    session = FakeSession(responses)
    f = fetcher(tmp_path, session)
    targets = [
        t42.race_target(f"2022050408{n:02d}", ["2021106347"]) for n in range(1, 7)
    ] + [t42.race_target("202603020312", ["2021106347"])]
    summary = t42.run_targets(f, targets)
    assert summary["permanent_gaps"] == 6
    assert summary["failures"] == 0
    assert summary["stopped"] is None  # 6連続400でも停止しない
    assert summary["fetched"] == 1  # 後続の正常ページは取得される
    with sqlite3.connect(tmp_path / "manifest.sqlite") as db:
        codes = [row[0] for row in db.execute(
            "SELECT error_code FROM fetch_manifest WHERE error_code IS NOT NULL"
        )]
    assert codes.count("http_400_id_not_on_race_site") == 6
    # 400は1回ずつしかリクエストしない (リトライ3回を浪費しない)
    assert len(session.calls) == 7


def test_horse_enumeration_uses_paid_race_cache(tmp_path):
    race_dir = tmp_path / "race"
    race_dir.mkdir()
    (race_dir / "202603020312.html").write_bytes(MEMBER_HTML)
    (race_dir / "202003020312.html").write_bytes(
        MEMBER_HTML.replace(b"2021106347", b"2017100001")
    )
    assert t42.horse_ids_from_race_cache(tmp_path, {2026}) == ["2021106347"]
    assert t42.horse_ids_from_race_cache(tmp_path, {2020}) == ["2017100001"]


def test_status_does_not_require_cookie_or_network(tmp_path, capsys):
    assert t42.main(["--manifest", str(tmp_path / "m.sqlite"), "--status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requests_today_jst"] == 0


def test_stage0_enforces_explicit_target_count():
    with pytest.raises(SystemExit, match="1..100"):
        t42.main(["--stage0"])


def test_scrape_lock_is_shared_with_t54c(tmp_path):
    lock = tmp_path / "shared.lock"
    with t42.scrape_lock(lock):
        with pytest.raises(t42.ScrapeLockError):
            with t42.scrape_lock(lock):
                pass
