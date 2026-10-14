"""SPEC-T84: race_results の部分取り込み (上位5頭で止まる不具合) の修正を固定するテスト。

Stage A で確認した事実 (フィクスチャは tests/fixtures/t84/ の実データ):
  - 影響レース (20260829:新潟:11) / 正常レース (20261012:東京:11) のいずれも
    `#race_result` 配下は table 3つ (競走成績 / タイム / コーナー通過順位)。
    競走成績テーブルは単一 <tbody> に全18行が入っており、現在は静的HTMLに
    6着以下が存在する (JS遅延読み込みではない)。
  - 旧 parse_result_html (select_one で最初の1テーブルのみ + tbody-or-tr[1:])
    でも、今この2レースのページを再取得すると実は全18行取れてしまう。
    つまり当時の欠落は「テーブル分割」自体の再現ではなく、取得タイミング
    (集計未確定時点での取得) 起因である可能性が高い。ただし JRA が将来
    6着以下を別テーブル/別tbodyに分けて配信するケースにも対応できるよう、
    Stage B の修正 (#race_result 配下の全テーブル・全tbodyを走査) は仕様
    どおり実施し、本ファイルで合成HTMLにより検証する。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from api.logging_store import LoggingStore
from api.result_service import ResultNotReady, parse_result_html, save_result_html

FIXTURES = Path(__file__).parent / "fixtures" / "t84"

_HEAD = '<h1><span class="opt">2026年7月11日（土曜）2回東京1日 1レース</span></h1>'
_SIDE_TABLES = """
<table class="basic narrow"><caption>タイム</caption><tbody class="th_left td_left">
<tr><th scope="row">ハロンタイム</th><td>12.8 - 10.6 - 12.0</td></tr>
</tbody></table>
<table class="basic narrow"><caption>コーナー通過順位</caption><tbody class="th_left td_left">
<tr><th scope="row">1コーナー</th><td>(1,2)-3-4</td></tr>
</tbody></table>
"""
_REFUND = """
<div class="refund_unit"><ul>
<li class="win"><div class="line"><div class="num">1</div><div class="yen">150円</div></div></li>
<li class="place"><div class="line"><div class="num">1</div><div class="yen">110円</div></div>
<div class="line"><div class="num">2</div><div class="yen">120円</div></div>
<div class="line"><div class="num">3</div><div class="yen">105円</div></div></li>
</ul></div>
"""


def _page(result_body: str) -> str:
    return f"<html><body>{_HEAD}<div id=\"race_result\">{result_body}{_SIDE_TABLES}</div>{_REFUND}</body></html>"


def _row(tr_id: int, place) -> str:
    return (f'<tr><td class="place">{place}</td><td class="num">{tr_id}</td>'
            f'<td class="horse">馬{tr_id}</td></tr>')


# ─── 1. 分割テーブル/分割tbody (SPEC §5-1) ─────────────────────────────────

def test_rows_split_across_two_tbody_in_one_table_are_all_collected():
    body = (
        "<table><tbody>"
        + _row(1, 1) + _row(2, 2) + _row(3, 3) + _row(4, 4) + _row(5, 5)
        + "</tbody><tbody>"
        + _row(6, 6) + _row(7, 7)
        + "</tbody></table>"
    )
    result = parse_result_html(_page(body))
    assert [r["finish_position"] for r in result["rows"]] == [1, 2, 3, 4, 5, 6, 7]
    assert result["rows"][-1]["horse_id"].endswith(":07")
    assert result["rows"][-1]["horse_name"] == "馬7"


def test_rows_split_across_two_tables_are_all_collected():
    top5 = "<table><tbody>" + "".join(_row(n, n) for n in range(1, 6)) + "</tbody></table>"
    rest = "<table><tbody>" + _row(6, 6) + _row(7, 7) + "</tbody></table>"
    result = parse_result_html(_page(top5 + rest))
    assert len(result["rows"]) == 7
    assert [r["finish_position"] for r in result["rows"]] == [1, 2, 3, 4, 5, 6, 7]


# ─── 2. 既存の単一テーブル構造 (2018 fixture) は 1 文字も変わらない ─────────

def test_legacy_single_table_2018_fixture_is_unchanged():
    path = Path("data/t54c/raw/2018/pw01sde1001201801010120180728_58-18998d28880d.html")
    html = path.read_text(encoding="cp932", errors="replace")
    result = parse_result_html(html, source_url="fixture://2018")

    assert result["race_id"] == "20180728:札幌:01"
    assert len(result["rows"]) == 6
    expected = [
        (1, "01", "コントラチェック", 1.1, 110, 100, []),
        (2, "06", "ブラックウォーリア", None, None, 130, ["final_win_odds_unavailable"]),
        (3, "03", "テングレート", None, None, None, ["final_win_odds_unavailable"]),
        (4, "04", "イチザテイオー", None, None, None, ["final_win_odds_unavailable"]),
        (5, "05", "トーセンイマジン", None, None, None, ["final_win_odds_unavailable"]),
        (6, "02", "ロングファイナリー", None, None, None, ["final_win_odds_unavailable"]),
    ]
    for row, (position, horse_no, name, odds, win_payout, place_payout, flags) in zip(result["rows"], expected):
        assert row["finish_position"] == position
        assert row["horse_id"] == f"20180728:札幌:01:{horse_no}"
        assert row["horse_name"] == name
        assert row["final_win_odds"] == odds
        assert row["win_payout"] == win_payout
        assert row["place_payout"] == place_payout
        assert row["data_quality_flags"] == flags


@pytest.mark.parametrize("fixture_name,expected_count,expected_first,expected_last", [
    ("affected_20260829_niigata_11", 18, ("18", "ピースフルマインド", 1, 8460),
     ("10", "ノブステップ", 18)),
    ("normal_20261012_tokyo_11", 18, ("02", "アスクセクシーモア", 1, 340),
     ("11", "セイウンプラチナ", 18)),
])
def test_real_2026_result_pages_are_fully_parsed(fixture_name, expected_count, expected_first, expected_last):
    """Stage A で実際に取得した本番ページ (tests/fixtures/t84/) が全馬分
    取れることを固定する。2 レースとも #race_result 直下は単一 <tbody> に
    全馬が入っており (分割は再現しなかった)、現在は両方とも静的 HTML に
    6 着以下が存在する。"""
    html = (FIXTURES / f"{fixture_name}.html").read_text(encoding="utf-8")
    result = parse_result_html(html, source_url=f"fixture://t84/{fixture_name}")
    assert len(result["rows"]) == expected_count

    first = result["rows"][0]
    no, name, position, win_payout = expected_first
    assert (first["horse_id"].rsplit(":", 1)[-1], first["horse_name"], first["finish_position"], first["win_payout"]) \
        == (no, name, position, win_payout)

    last = result["rows"][-1]
    no, name, position = expected_last
    assert (last["horse_id"].rsplit(":", 1)[-1], last["horse_name"], last["finish_position"]) == (no, name, position)


# ─── 3. 同着で着順ラベルが飛ぶケース ────────────────────────────────────────

def test_dead_heat_finish_labels_are_preserved():
    body = "<table><tbody>" + "".join([
        _row(1, 1), _row(2, 2), _row(3, 3), _row(4, 3), _row(5, 5),
    ]) + "</tbody></table>"
    result = parse_result_html(_page(body))
    assert [r["finish_position"] for r in result["rows"]] == [1, 2, 3, 3, 5]
    assert len(result["rows"]) == 5


# ─── 4. ヘッダ行・空行は結果行として混入しない ─────────────────────────────

def test_header_and_empty_rows_are_not_mistaken_for_result_rows():
    body = (
        "<table><thead><tr><th>着順</th><th>馬番</th><th>馬名</th></tr></thead><tbody>"
        + "<tr></tr>"
        + _row(1, 1) + _row(2, 2)
        + '<tr><td class="place"></td><td class="num"></td><td class="horse"></td></tr>'
        + "</tbody></table>"
    )
    result = parse_result_html(_page(body))
    assert len(result["rows"]) == 2
    assert [r["finish_position"] for r in result["rows"]] == [1, 2]


# ─── 5. 同一馬の重複は1件に畳み、警告を出す ─────────────────────────────────

def test_duplicate_horse_row_is_collapsed_and_warns(caplog):
    top5 = "<table><tbody>" + "".join(_row(n, n) for n in range(1, 6)) + "</tbody></table>"
    # 6着以下の別テーブルに、既に確定済みの1番がもう一度混入したケース
    # (例: ページ側の表示崩れ)。最初の出現 (1着) を正とする。
    dup_and_rest = "<table><tbody>" + _row(1, 99) + _row(6, 6) + "</tbody></table>"
    with caplog.at_level(logging.WARNING, logger="api.result_service"):
        result = parse_result_html(_page(top5 + dup_and_rest))

    assert len(result["rows"]) == 6
    horse_1 = next(r for r in result["rows"] if r["horse_id"].endswith(":01"))
    assert horse_1["finish_position"] == 1  # 最初の出現を採用、2回目(99)は捨てる
    assert any("duplicate" in record.message for record in caplog.records)


# ─── 6. field_size より明らかに少ない行数で partial_result_rows が立つ ─────

def _store_with_field_size(tmp_path, race_id: str, field_size: int) -> LoggingStore:
    store = LoggingStore(tmp_path / "t84.db")
    store.initialize()
    store.save_odds([{
        "race_id": race_id, "horse_id": f"{race_id}:01", "observed_at": "2026-07-11T00:00:00+09:00",
        "source": "jra", "field_size": field_size,
    }])
    return store


def _write_html(tmp_path, name: str, html: str) -> Path:
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    return path


def test_partial_result_rows_flag_is_set_when_row_count_is_far_below_field_size(tmp_path):
    race_id = "20260711:東京:01"
    store = _store_with_field_size(tmp_path, race_id, field_size=18)
    body = "<table><tbody>" + "".join(_row(n, n) for n in range(1, 6)) + "</tbody></table>"  # 5 rows
    html_path = _write_html(tmp_path, "partial.html", _page(body))

    result = save_result_html(html_path, store=store)

    assert result["partial_result_rows_flagged"] is True
    assert result["save_stats"] == {"inserted": 5, "updated": 0, "unchanged": 0}
    with sqlite3.connect(store.db_path) as conn:
        flags = [row[0] for row in conn.execute(
            "SELECT data_quality_flags_json FROM race_results WHERE race_id=?", (race_id,)
        ).fetchall()]
    assert all("partial_result_rows" in flag for flag in flags)


def test_partial_result_rows_flag_is_not_set_when_counts_are_close(tmp_path):
    race_id = "20260711:東京:01"
    store = _store_with_field_size(tmp_path, race_id, field_size=18)
    body = "<table><tbody>" + "".join(_row(n, n) for n in range(1, 17)) + "</tbody></table>"  # 16 rows
    html_path = _write_html(tmp_path, "close.html", _page(body))

    result = save_result_html(html_path, store=store)

    assert result["partial_result_rows_flagged"] is False
    with sqlite3.connect(store.db_path) as conn:
        flags = [row[0] for row in conn.execute(
            "SELECT data_quality_flags_json FROM race_results WHERE race_id=?", (race_id,)
        ).fetchall()]
    assert all("partial_result_rows" not in (flag or "") for flag in flags)


def test_partial_result_rows_flag_is_not_set_for_genuinely_small_field(tmp_path):
    # 20260905:阪神:02 相当: 実際に5頭立てのレースは誤検知してはいけない。
    race_id = "20260711:東京:01"
    store = _store_with_field_size(tmp_path, race_id, field_size=5)  # 5頭立て: 本当に少ない
    body = "<table><tbody>" + "".join(_row(n, n) for n in range(1, 6)) + "</tbody></table>"  # 5 rows
    html_path = _write_html(tmp_path, "small_field.html", _page(body))

    result = save_result_html(html_path, store=store)

    assert result["partial_result_rows_flagged"] is False


def test_partial_result_rows_flag_is_not_set_without_known_field_size(tmp_path):
    race_id = "20260711:東京:01"
    store = LoggingStore(tmp_path / "no_odds.db")  # odds_snapshots has no row for this race
    body = "<table><tbody>" + "".join(_row(n, n) for n in range(1, 6)) + "</tbody></table>"
    html_path = _write_html(tmp_path, "unknown_field.html", _page(body))

    result = save_result_html(html_path, store=store)

    assert result["partial_result_rows_flagged"] is False


# ─── 既存の挙動: 未公開ページは依然 ResultNotReady ──────────────────────────

def test_missing_result_table_is_still_retryable():
    with pytest.raises(ResultNotReady):
        parse_result_html("<html><body>集計中</body></html>")
