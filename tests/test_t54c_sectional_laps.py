import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import requests

import t54c_sectional_laps as t54c


def _page(
    *,
    year=2018,
    distance=1700,
    corners=(2, 2, 1, 1),
    corner_numbers=(1, 2, 3, 4),
    status="1",
    race_name="テスト特別",
    laps=None,
    up="4F 50.4 - 3F 38.2",
    aggregate_label="3コーナー",
    include_laps=True,
    course_suffix="ダート・右",
):
    if laps is None:
        count = round(distance / 200)
        if distance % 200:
            count += 1
        laps = [12.0 + index / 10 for index in range(count)]
    corner_items = "".join(
        f'<li title="{number}コーナー通過順位">{position}</li>'
        for number, position in zip(corner_numbers, corners)
    )
    lap_row = (
        "<tr><th scope=\"row\">ハロンタイム</th>"
        f"<td>{' - '.join(map(str, laps))}</td></tr>"
        if include_laps else ""
    )
    return f"""<!doctype html><html><body>
      <h1>レース結果{year}年2月10日（土曜）1回小倉1日 8レース</h1>
      <h2>{race_name}</h2>
      <p>コース：{distance:,}メートル（{course_suffix}）</p>
      <table id="race_result">
        <tr><th>着順</th><th>馬番</th><th>馬名</th>
            <th>コーナー通過順位</th></tr>
        <tr><td>{status}</td><td>16</td><td>テスト馬</td>
            <td class="corner"><div class="corner_list"><ul>{corner_items}</ul>
            </div></td></tr>
      </table>
      <table class="time">
        {lap_row}
        <tr><th scope="row">上り</th><td>{up}</td></tr>
        <tr><th scope="row">{aggregate_label}</th><td>16,2,3</td></tr>
      </table>
    </body></html>"""


def _parse(html, *, year=2018):
    url = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        f"CNAME=pw01sde1010{year}010108{year}0210%2F81"
    )
    return t54c.parse_sectional_html(
        html, source_url=url, fetched_at="2026-07-23T00:00:00+00:00"
    )


def test_old_four_corner_fixture_and_current_short_fixture():
    old = _parse(_page(year=2018), year=2018)
    assert old["date"] == "20180210"
    assert old["place"] == "小倉"
    assert old["r"] == 8
    assert old["runners"][0]["corner_numbers"] == [1, 2, 3, 4]
    assert old["runners"][0]["corner_positions"] == [2, 2, 1, 1]
    assert old["runners"][0]["c4"] == 1

    current = _parse(
        _page(
            year=2026,
            distance=1200,
            corners=(12, 14),
            corner_numbers=(3, 4),
        ),
        year=2026,
    )
    assert current["runners"][0]["corner_numbers"] == [3, 4]
    assert current["runners"][0]["corner_positions"] == [12, 14]
    assert len(current["lap_sequence"]) == 6


@pytest.mark.parametrize("status", ["取消", "除外", "競走中止"])
def test_non_runner_and_stopped_fixture_keeps_empty_corner_array(status):
    parsed = _parse(_page(corners=(), corner_numbers=(), status=status))
    assert parsed["runners"][0]["corner_positions"] == []
    assert parsed["runners"][0]["c4"] is None
    assert parsed["warnings"] == []


def test_obstacle_fixture_parses_but_is_explicitly_excluded_without_laps():
    parsed = _parse(
        _page(
            distance=2860,
            corners=(),
            corner_numbers=(),
            status="1",
            race_name="障害3歳以上未勝利",
            include_laps=False,
        )
    )
    assert parsed["excluded_reason"] == "obstacle"
    assert parsed["distance"] == 2860
    assert parsed["runners"][0]["umaban"] == 16


def test_second_lap_annotation_does_not_break_runner_corner_parse():
    parsed = _parse(
        _page(distance=2400, aggregate_label="3コーナー(2周目)")
    )
    assert parsed["lap_sequence"]
    assert parsed["runners"][0]["corner_positions"] == [2, 2, 1, 1]


def test_niigata_straight_course_has_valid_zero_corner_sequence():
    html = _page(
        distance=1000,
        corners=(),
        corner_numbers=(),
        course_suffix="芝・直",
    ).replace("1回小倉1日", "1回新潟1日")
    parsed = _parse(html)
    assert parsed["place"] == "新潟"
    assert parsed["is_straight"] is True
    assert parsed["runners"][0]["corner_positions"] == []
    assert parsed["warnings"] == []


def test_lap_header_exact_match_never_uses_adjacent_up_row():
    parsed = _parse(
        _page(
            distance=1200,
            corners=(1, 1),
            corner_numbers=(3, 4),
            laps=[11.7, 10.4, 11.0, 11.3, 11.5, 12.3],
            up="4F 46.1 - 3F 35.1",
        )
    )
    assert parsed["lap_sequence"] == [11.7, 10.4, 11.0, 11.3, 11.5, 12.3]
    assert 46.1 not in parsed["lap_sequence"]
    assert 35.1 not in parsed["lap_sequence"]


class _Response:
    status_code = 200
    content = b"<html>cached</html>"

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.headers = {}
        self.get_calls = 0
        self.post_calls = 0

    def get(self, *_args, **_kwargs):
        self.get_calls += 1
        return _Response()

    def post(self, *_args, **_kwargs):
        self.post_calls += 1
        return _Response()


def test_cache_hit_performs_zero_additional_http_requests(tmp_path):
    session = _Session()
    fetcher = t54c.RateLimitedFetcher(
        cache_root=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.sqlite",
        interval=1.0,
        session=session,
    )
    url = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1010201801010820180210%2F81"
    )
    first = fetcher.fetch(url)
    before = (session.get_calls, session.post_calls, fetcher.network_requests)
    second = fetcher.fetch(url)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert (session.get_calls, session.post_calls, fetcher.network_requests) == before
    assert first.sha256 == second.sha256


def test_encoded_and_literal_cname_slash_share_one_cache_key(tmp_path):
    session = _Session()
    fetcher = t54c.RateLimitedFetcher(
        cache_root=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.sqlite",
        interval=1.0,
        session=session,
    )
    encoded = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1010201801010820180210%2F81"
    )
    literal = encoded.replace("%2F", "/")
    assert fetcher.fetch(encoded).cache_hit is False
    assert fetcher.fetch(literal).cache_hit is True
    assert session.get_calls == 1
    with sqlite3.connect(tmp_path / "manifest.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM fetch_manifest"
        ).fetchone()[0] == 1


def test_cache_sha_tampering_is_rejected_instead_of_reused(tmp_path):
    fetcher = t54c.RateLimitedFetcher(
        cache_root=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.sqlite",
        interval=1.0,
        session=_Session(),
    )
    url = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1010201801010820180210%2F81"
    )
    cached = fetcher.fetch(url)
    cached.cache_path.write_bytes(b"tampered")
    with pytest.raises(t54c.T54cError, match="SHA-256不一致"):
        fetcher.fetch(url)


class _FailedSession(_Session):
    def get(self, *_args, **_kwargs):
        self.get_calls += 1
        raise requests.ConnectionError("offline")


def test_fetch_failure_records_error_and_does_not_create_html(tmp_path):
    fetcher = t54c.RateLimitedFetcher(
        cache_root=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.sqlite",
        interval=1.0,
        max_retries=1,
        failure_threshold=2,
        session=_FailedSession(),
    )
    url = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1010201801010820180210%2F81"
    )
    with pytest.raises(t54c.T54cError, match="取得に失敗"):
        fetcher.fetch(url)
    assert not t54c.cache_path_for(url, tmp_path / "raw").exists()
    with sqlite3.connect(tmp_path / "manifest.sqlite") as connection:
        status, digest, error = connection.execute(
            "SELECT http_status,sha256,error FROM fetch_manifest WHERE url=?",
            (url,),
        ).fetchone()
    assert status is None and digest is None
    assert "offline" in error


def test_consecutive_fetch_failures_trigger_automatic_stop(tmp_path, monkeypatch):
    fetcher = t54c.RateLimitedFetcher(
        cache_root=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.sqlite",
        interval=1.0,
        max_retries=1,
        failure_threshold=2,
        session=_FailedSession(),
    )
    monkeypatch.setattr(fetcher, "_wait", lambda: None)
    first = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1010201801010120180210%2F8E"
    )
    second = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1010201801010220180210%2F43"
    )
    with pytest.raises(t54c.T54cError, match="取得に失敗"):
        fetcher.fetch(first)
    with pytest.raises(t54c.ConsecutiveFetchError, match="2件連続"):
        fetcher.fetch(second)


def _runs_db(path: Path, c4=1):
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE runs (
            date TEXT, place TEXT, r INTEGER, umaban INTEGER,
            c4 INTEGER, sentinel TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO runs (date,place,r,umaban,c4,sentinel) VALUES (?,?,?,?,?,?)",
        ("20180210", "小倉", 8, 16, c4, "untouched"),
    )
    connection.commit()
    return connection


def test_explicit_insert_preserves_runs_and_matches_existing_c4(tmp_path):
    connection = _runs_db(tmp_path / "ability.db")
    before_schema = connection.execute("PRAGMA table_info(runs)").fetchall()
    before_rows = connection.execute("SELECT * FROM runs").fetchall()
    parsed = _parse(_page())

    t54c.init_sectional_schema(connection)
    saved = t54c.save_sectional_race(connection, parsed)

    assert saved["checked"] == 1
    assert saved["mismatches"] == 0
    assert saved["legacy_sequence_encoded"] == 0
    assert connection.execute("PRAGMA table_info(runs)").fetchall() == before_schema
    assert connection.execute("SELECT * FROM runs").fetchall() == before_rows
    assert json.loads(connection.execute(
        "SELECT corner_positions_json FROM runner_corners"
    ).fetchone()[0]) == [2, 2, 1, 1]
    assert json.loads(connection.execute(
        "SELECT lap_sequence_json FROM race_laps"
    ).fetchone()[0]) == parsed["lap_sequence"]


def test_c4_mismatch_fails_before_writing_new_tables(tmp_path):
    connection = _runs_db(tmp_path / "ability.db", c4=9)
    t54c.init_sectional_schema(connection)
    with pytest.raises(t54c.ParseError, match="不一致"):
        t54c.save_sectional_race(connection, _parse(_page()))
    assert connection.execute("SELECT COUNT(*) FROM runner_corners").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM race_laps").fetchone()[0] == 0


def test_legacy_full_sequence_integer_is_audited_but_does_not_block_save(tmp_path):
    connection = _runs_db(tmp_path / "ability.db", c4=2211)
    t54c.init_sectional_schema(connection)
    saved = t54c.save_sectional_race(connection, _parse(_page()))
    assert saved["checked"] == 1
    assert saved["mismatches"] == 0
    assert saved["legacy_sequence_encoded"] == 1
    assert connection.execute("SELECT c4 FROM runs").fetchone()[0] == 2211
    assert connection.execute("SELECT COUNT(*) FROM race_laps").fetchone()[0] == 1


def test_shared_scrape_lock_rejects_concurrent_owner(tmp_path):
    lock = tmp_path / "scrape.lock"
    with t54c.scrape_lock(lock):
        with pytest.raises(t54c.ScrapeLockError, match="実行中"):
            with t54c.scrape_lock(lock):
                pass
    assert not lock.exists()


def test_month_and_venue_link_parsers_keep_official_cnames():
    monthly = """
      <a onclick="return doAction('/JRADB/accessS.html',
        'pw01srl10062018010120180106/63');">1回中山1日</a>
    """
    assert t54c._onclick_cnames(monthly, "pw01srl") == [
        "pw01srl10062018010120180106/63"
    ]


def test_parser_sha_matches_original_cp932_bytes():
    raw = _page().encode("cp932")
    parsed = t54c.parse_sectional_html(
        raw,
        source_url="https://example.test/?CNAME=pw01sde",
        fetched_at="2026-07-23T00:00:00+00:00",
    )
    assert parsed["sha256"] == hashlib.sha256(raw).hexdigest()
