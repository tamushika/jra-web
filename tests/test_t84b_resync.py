"""SPEC-T84b: 部分取り込み (上位数頭で止まった) レース結果の再同期を検証する。

対応する仕様: docs/codex/SPEC-T84b-result-resync.md §6
  1. find_incomplete_results: フラグ付き / 行数不足 / 正常(5頭立て) / source_url無し
  2. resync_result: 行が増える / 増えない / 取得失敗
  3. 既存行の着順・払戻が確定値で上書きされる (同一horse_id)
  4. resync_incomplete_results: limitと間隔が守られる
  5. 日次ガード: 24時間以内の再実行がスキップされる
  6. 閲覧モードでは実行されない
  7. ネットワークはmonkeypatchし、実アクセスしない

このファイルは一切ネットワークにアクセスしない。requests.get はすべて
monkeypatchで差し替える。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import requests

from api.logging_store import LoggingStore
from api.result_service import (
    DAILY_RESYNC_KIND,
    find_incomplete_results,
    resync_incomplete_results,
    resync_incomplete_results_daily,
    resync_result,
)

# ─── HTML組み立てヘルパ (tests/test_t84_result_parser.py と同趣旨の最小再現) ──

_SIDE_TABLES = """
<table class="basic narrow"><caption>タイム</caption><tbody class="th_left td_left">
<tr><th scope="row">ハロンタイム</th><td>12.8 - 10.6 - 12.0</td></tr>
</tbody></table>
"""


def _head(date_text: str, race_no: int) -> str:
    return f'<h1><span class="opt">{date_text} {race_no}レース</span></h1>'


def _row(tr_id: int, place) -> str:
    return (f'<tr><td class="place">{place}</td><td class="num">{tr_id}</td>'
            f'<td class="horse">馬{tr_id}</td></tr>')


def _refund(win_pairs, place_pairs) -> str:
    win_lines = "".join(
        f'<div class="line"><div class="num">{n}</div><div class="yen">{y}円</div></div>'
        for n, y in win_pairs)
    place_lines = "".join(
        f'<div class="line"><div class="num">{n}</div><div class="yen">{y}円</div></div>'
        for n, y in place_pairs)
    return (f'<div class="refund_unit"><ul><li class="win">{win_lines}</li>'
            f'<li class="place">{place_lines}</li></ul></div>')


def _page(date_text: str, race_no: int, n_rows: int, *, with_payouts: bool = True) -> str:
    body = "<table><tbody>" + "".join(_row(n, n) for n in range(1, n_rows + 1)) + "</tbody></table>"
    win_pairs = [(1, 150)] if with_payouts else []
    place_pairs = [(1, 110), (2, 120), (3, 105)] if with_payouts else []
    refund = _refund(win_pairs, place_pairs)
    return (f"<html><body>{_head(date_text, race_no)}"
            f'<div id="race_result">{body}{_SIDE_TABLES}</div>{refund}</body></html>')


RACE_ID = "20260711:東京:01"
DATE_TEXT = "2026年7月11日（土曜）2回東京1日"
SOURCE_URL = "https://www.jra.go.jp/JRADB/accessS.html?CNAME=pw01sde0105202604010120260711/2A"


def _recent_iso(days_ago: float = 1) -> str:
    """find_incomplete_results の既定 max_age_days=60 に確実に収まる、実行時刻
    基準の最近のタイムスタンプ (レースIDが示す日付とは独立)。"""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)) \
        .isoformat(timespec="microseconds").replace("+00:00", "Z")


class _FakeResponse:
    def __init__(self, url, text):
        self.url = url
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        return None


def _seed_initial_rows(store: LoggingStore, *, race_id=RACE_ID, n_rows=5, field_size=None,
                       flags=None, source_url=SOURCE_URL, result_fetched_at=None):
    result_fetched_at = result_fetched_at or _recent_iso(1)
    rows = []
    for n in range(1, n_rows + 1):
        rows.append({
            "race_id": race_id, "horse_id": f"{race_id}:{n:02d}", "horse_name": f"馬{n}",
            "finish_position": n, "official_status": "official",
            "final_win_odds": None, "win_payout": None, "place_payout": None,
            "result_fetched_at": result_fetched_at, "source_url": source_url,
            "source_hash": "seed-hash", "data_quality_flags": list(flags or []),
        })
    store.save_race_results(rows)
    if field_size is not None:
        store.save_odds([{
            "race_id": race_id, "horse_id": f"{race_id}:01", "observed_at": result_fetched_at,
            "source": "jra", "field_size": field_size,
        }])


def _row_count(store: LoggingStore, race_id: str) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM race_results WHERE race_id=?", (race_id,)
        ).fetchone()[0]


# ─── 1. find_incomplete_results ────────────────────────────────────────────

def test_finds_race_flagged_partial_result_rows(tmp_path):
    store = LoggingStore(tmp_path / "a.db")
    _seed_initial_rows(store, flags=["partial_result_rows"])

    targets = find_incomplete_results(store.db_path)

    assert len(targets) == 1
    item = targets[0]
    assert item["race_id"] == RACE_ID
    assert item["rows"] == 5
    assert item["source_url"] == SOURCE_URL
    assert item["result_fetched_at"]


def test_finds_race_with_row_count_far_below_field_size(tmp_path):
    store = LoggingStore(tmp_path / "b.db")
    _seed_initial_rows(store, n_rows=5, field_size=18)  # no explicit flag

    targets = find_incomplete_results(store.db_path)

    assert len(targets) == 1
    assert targets[0]["field_size"] == 18


def test_genuinely_small_field_is_not_flagged(tmp_path):
    store = LoggingStore(tmp_path / "c.db")
    _seed_initial_rows(store, n_rows=5, field_size=5)  # real 5-horse field

    targets = find_incomplete_results(store.db_path)

    assert targets == []


def test_race_without_source_url_is_excluded(tmp_path):
    store = LoggingStore(tmp_path / "d.db")
    _seed_initial_rows(store, n_rows=5, field_size=18, source_url=None)

    targets = find_incomplete_results(store.db_path)

    assert targets == []


def test_row_count_close_to_field_size_is_not_a_target(tmp_path):
    store = LoggingStore(tmp_path / "e.db")
    _seed_initial_rows(store, n_rows=16, field_size=18)  # within 3 of field size

    targets = find_incomplete_results(store.db_path)

    assert targets == []


# ─── 2/3. resync_result ─────────────────────────────────────────────────────

def test_resync_result_upserts_rows_when_count_increases_and_overwrites_existing(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "f.db")
    _seed_initial_rows(store, n_rows=5, field_size=18)  # horse 1 has no payout yet

    full_html = _page(DATE_TEXT, 1, 18, with_payouts=True)
    monkeypatch.setattr("api.result_service.requests.get",
                        lambda url, **_kw: _FakeResponse(SOURCE_URL, full_html))

    outcome = resync_result(RACE_ID, db_path=store.db_path, sleep_seconds=0)

    assert outcome["status"] == "resynced"
    assert outcome["rows_before"] == 5
    assert outcome["rows_after"] == 18
    assert _row_count(store, RACE_ID) == 18

    with sqlite3.connect(store.db_path) as conn:
        horse1 = conn.execute(
            "SELECT finish_position, win_payout FROM race_results WHERE race_id=? AND horse_id=?",
            (RACE_ID, f"{RACE_ID}:01"),
        ).fetchone()
    # item 3: 既存行 (horse 1) が確定値で上書きされる
    assert horse1 == (1, 150)


def test_resync_result_clears_partial_flag_once_fully_resolved(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "g.db")
    _seed_initial_rows(store, n_rows=5, flags=["partial_result_rows"], field_size=18)

    full_html = _page(DATE_TEXT, 1, 18, with_payouts=True)
    monkeypatch.setattr("api.result_service.requests.get",
                        lambda url, **_kw: _FakeResponse(SOURCE_URL, full_html))

    resync_result(RACE_ID, db_path=store.db_path, sleep_seconds=0)

    with sqlite3.connect(store.db_path) as conn:
        flags = [row[0] for row in conn.execute(
            "SELECT data_quality_flags_json FROM race_results WHERE race_id=?", (RACE_ID,)
        ).fetchall()]
    assert all("partial_result_rows" not in (flag or "") for flag in flags)


def test_resync_result_does_not_write_when_row_count_does_not_increase(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "h.db")
    _seed_initial_rows(store, n_rows=5, field_size=18)

    same_html = _page(DATE_TEXT, 1, 5, with_payouts=True)  # still only 5 rows
    monkeypatch.setattr("api.result_service.requests.get",
                        lambda url, **_kw: _FakeResponse(SOURCE_URL, same_html))

    outcome = resync_result(RACE_ID, db_path=store.db_path, sleep_seconds=0)

    assert outcome["status"] == "no_change"
    assert _row_count(store, RACE_ID) == 5
    with sqlite3.connect(store.db_path) as conn:
        win_payout = conn.execute(
            "SELECT win_payout FROM race_results WHERE race_id=? AND horse_id=?",
            (RACE_ID, f"{RACE_ID}:01"),
        ).fetchone()[0]
    assert win_payout is None  # 元の (未確定) 値のまま、上書きされていない


def test_resync_result_fails_soft_on_network_error(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "i.db")
    _seed_initial_rows(store, n_rows=5, field_size=18)

    def _raise(url, **_kw):
        raise requests.RequestException("boom")

    monkeypatch.setattr("api.result_service.requests.get", _raise)

    outcome = resync_result(RACE_ID, db_path=store.db_path, sleep_seconds=0)

    assert outcome["status"] == "failed"
    assert "error" in outcome
    assert _row_count(store, RACE_ID) == 5


def test_resync_result_fails_soft_when_no_source_url_on_file(tmp_path):
    store = LoggingStore(tmp_path / "j.db")
    _seed_initial_rows(store, n_rows=5, field_size=18, source_url=None)

    outcome = resync_result(RACE_ID, db_path=store.db_path, sleep_seconds=0)

    assert outcome["status"] == "failed"


# ─── 4. resync_incomplete_results: limit / 間隔 ────────────────────────────

def test_resync_incomplete_results_respects_limit_and_sleeps_between_requests(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "k.db")
    race_ids = ["20260711:東京:01", "20260712:東京:02", "20260713:東京:03"]
    for i, race_id in enumerate(race_ids):
        _seed_initial_rows(store, race_id=race_id, n_rows=5, field_size=18,
                           source_url=f"https://example.test/{race_id}",
                           result_fetched_at=_recent_iso(3 - i))  # race0が最古、race2が最新

    # 常に行が増えない (no_change) ページを返す -- 書き込みを発生させず、呼び出し
    # 回数とsleepの呼び出し回数だけを検証する。
    monkeypatch.setattr("api.result_service.requests.get",
                        lambda url, **_kw: _FakeResponse(url, _page(DATE_TEXT, 1, 5)))
    sleep_calls = []
    monkeypatch.setattr("api.result_service.time.sleep", lambda s: sleep_calls.append(s))

    result = resync_incomplete_results(store.db_path, limit=2, sleep_seconds=1.5)

    assert result["targets"] == 2  # limit=2 が3件中2件に制限する
    assert len(sleep_calls) == 2  # resync_result 1回につき1回sleepする
    assert all(s == 1.5 for s in sleep_calls)
    # 古い順 (2026-07-11, 2026-07-12) が処理され、最新 (07-13) は対象外
    processed_ids = [item["race_id"] for item in result["details"]]
    assert processed_ids == ["20260711:東京:01", "20260712:東京:02"]


# ─── 5. 日次ガード ──────────────────────────────────────────────────────────

def test_daily_guard_runs_on_first_call_and_skips_within_24_hours(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "l.db")
    _seed_initial_rows(store, n_rows=5, field_size=18)

    calls = []

    def _fake_resync(db_path, *, limit=50, sleep_seconds=1.5, max_age_days=60, timeout=20):
        calls.append(db_path)
        return {"targets": 0, "resynced": 0, "no_change": 0, "failed": 0, "details": []}

    monkeypatch.setattr("api.result_service.resync_incomplete_results", _fake_resync)

    first = resync_incomplete_results_daily(store.db_path, limit=20)
    assert first["skipped"] is False
    assert len(calls) == 1

    second = resync_incomplete_results_daily(store.db_path, limit=20)
    assert second["skipped"] is True
    assert len(calls) == 1  # 24時間以内の再実行はネットワークに触れない


def test_daily_guard_runs_again_once_the_window_has_passed(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "m.db")
    _seed_initial_rows(store, n_rows=5, field_size=18)
    store.mark_resync_run(DAILY_RESYNC_KIND, run_at="2020-01-01T00:00:00Z")

    calls = []
    monkeypatch.setattr(
        "api.result_service.resync_incomplete_results",
        lambda db_path, **_kw: calls.append(db_path) or
        {"targets": 0, "resynced": 0, "no_change": 0, "failed": 0, "details": []},
    )

    result = resync_incomplete_results_daily(store.db_path, limit=20)

    assert result["skipped"] is False
    assert len(calls) == 1


# ─── 6. 閲覧モードでは実行されない ───────────────────────────────────────────

def test_viewer_mode_skips_the_analysis_complete_hook(monkeypatch):
    import jra_ev
    import result_service as result_service_flat  # api/ はjra_ev.sys.pathで解決される別名

    monkeypatch.setenv("JRA_VIEWER_MODE", "1")
    calls = []
    monkeypatch.setattr(result_service_flat, "resync_incomplete_results_daily",
                        lambda *a, **kw: calls.append(True))

    jra_ev._t84b_resync_incomplete_results_hook()

    assert calls == []


def test_normal_mode_runs_the_analysis_complete_hook(monkeypatch):
    import jra_ev
    import result_service as result_service_flat

    monkeypatch.delenv("JRA_VIEWER_MODE", raising=False)
    calls = []
    monkeypatch.setattr(result_service_flat, "resync_incomplete_results_daily",
                        lambda *a, **kw: calls.append(True) or
                        {"skipped": False, "targets": 0, "resynced": 0, "no_change": 0, "failed": 0, "details": []})

    jra_ev._t84b_resync_incomplete_results_hook()

    assert calls == [True]
