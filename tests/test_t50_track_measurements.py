from pathlib import Path
import sqlite3

import pytest
import requests

from t50_track_measurements import (
    CollectionHalted,
    Fetcher,
    archive_links,
    open_store,
    parse_legacy_text,
    parse_modern_text,
    reconcile_race_day_flags,
    save_measurements,
)


FIXTURES = Path(__file__).parent / "fixtures" / "t50"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_legacy_layout_keeps_friday_but_marks_only_race_days():
    rows = parse_legacy_text(
        fixture("legacy_tokyo01_layout.txt"), "東京",
        source_url="fixture://2021/tokyo01", fetched_at="2026-07-19T00:00:00+00:00",
        pdf_sha256="abc",
    )

    assert [row.date for row in rows] == [
        "2021-01-29", "2021-01-30", "2021-01-31",
        "2021-02-05", "2021-02-06", "2021-02-07",
    ]
    assert [row.is_race_day for row in rows] == [0, 1, 1, 0, 1, 1]
    assert rows[0].cushion == 8.4
    assert rows[1].turf_moisture_goal == 16.2
    assert rows[2].dirt_moisture_4c == 10.6
    assert rows[2].published_at is None
    assert rows[2].format_version == "legacy-2021-2024"


def test_image_only_transcription_is_parseable_and_complete():
    text = (Path(__file__).parents[1] / "docs" / "codex" / "fixtures" /
            "T50-2021-chukyo06-transcription.txt").read_text(encoding="utf-8")
    rows = parse_legacy_text(text, "中京")

    assert len(rows) == 9
    assert sum(row.is_race_day for row in rows) == 6
    assert rows[-1].date == "2021-12-19"
    assert rows[-1].cushion == 9.3
    assert rows[-1].dirt_moisture_4c == 12.6


def test_parse_modern_layout_separates_measurement_times_from_publication():
    rows = parse_modern_text(
        fixture("modern_tokyo01_layout.txt"), "東京",
        source_url="fixture://2025/tokyo01", fetched_at="2026-07-19T00:00:00+00:00",
    )

    assert [row.date for row in rows] == [
        "2025-01-31", "2025-02-01", "2025-02-02", "2025-02-08"]
    assert [row.is_race_day for row in rows] == [0, 1, 1, 0]
    assert rows[1].cushion == 9.4
    assert rows[1].turf_moisture_4c == 15.4
    assert rows[1].dirt_moisture_goal == 11.1
    assert rows[1].cushion_measured_at == "2025-02-01T07:00:00+09:00"
    assert rows[1].moisture_measured_at == "2025-02-01T05:30:00+09:00"
    assert rows[1].published_at is None


def test_archive_links_accept_only_official_year_pdf_names():
    html = b"""<a href='/keiba/baba/archive/2025pdf/tokyo01.pdf'>x</a>
    <a href='./2025pdf/chukyo02.pdf'>y</a><a href='/other.pdf'>z</a>"""
    assert archive_links(html, 2025) == [
        ("https://www.jra.go.jp/keiba/baba/archive/2025pdf/chukyo02.pdf", "中京", 2),
        ("https://www.jra.go.jp/keiba/baba/archive/2025pdf/tokyo01.pdf", "東京", 1),
    ]


def test_sqlite_upsert_has_one_date_venue_row(tmp_path):
    rows = parse_modern_text(fixture("modern_tokyo01_layout.txt"), "東京")
    connection = open_store(tmp_path / "t50.sqlite")
    try:
        assert save_measurements(connection, rows) == 4
        assert save_measurements(connection, rows) == 4
        assert connection.execute("SELECT COUNT(*) FROM track_measurements").fetchone()[0] == 4
        assert connection.execute(
            "SELECT published_at FROM track_measurements WHERE date='2025-02-01'"
        ).fetchone()[0] is None
    finally:
        connection.close()


def test_fetcher_enforces_interval_and_halts_on_tenth_failure():
    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, _url, **_kwargs):
            raise requests.ConnectionError("offline")

    now = [0.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    fetcher = Fetcher(session=Session(), sleep=sleep, clock=clock)
    for _ in range(9):
        with pytest.raises(requests.ConnectionError):
            fetcher.get("https://example.test/fail")
    with pytest.raises(CollectionHalted):
        fetcher.get("https://example.test/fail")

    assert len(sleeps) == 9
    assert all(seconds >= 1.0 for seconds in sleeps)


def test_race_day_reconciliation_fixes_irregular_holiday_columns(tmp_path):
    t50_db = tmp_path / "t50.sqlite"
    connection = open_store(t50_db)
    rows = parse_legacy_text("""
    第1日・第2日（2024年10月11日～14日）
    クッション値 芝 9.0 9.1 9.2 9.3
    含水率 芝 ゴール前 10.0 10.1 10.2 10.3
    4コーナー 11.0 11.1 11.2 11.3
    ダート ゴール前 5.0 5.1 5.2 5.3
    4コーナー 6.0 6.1 6.2 6.3
    """, "新潟")
    save_measurements(connection, rows)
    connection.close()

    calendar_db = tmp_path / "ability.sqlite"
    calendar = sqlite3.connect(calendar_db)
    calendar.execute("CREATE TABLE runs(date TEXT, place TEXT, rank INTEGER)")
    calendar.executemany("INSERT INTO runs VALUES (?,?,1)",
                         [("20241012", "新潟"), ("20241014", "新潟")])
    calendar.commit()
    calendar.close()

    result = reconcile_race_day_flags(t50_db, calendar_db)
    connection = sqlite3.connect(t50_db)
    flags = connection.execute(
        "SELECT date,is_race_day FROM track_measurements ORDER BY date").fetchall()
    connection.close()

    assert result["changed_flags"] == 2
    assert flags == [("2024-10-11", 0), ("2024-10-12", 1),
                     ("2024-10-13", 0), ("2024-10-14", 1)]
