import sqlite3

import pytest

from backfill_runs_netkeiba import (
    RUN_COLUMNS,
    RacePageError,
    insert_race,
    parse_race_html,
)


def _race_html(course="芝右1600m / 天候 : 晴 / 芝 : 良", win_pay="370"):
    return f"""
    <html><body>
      <div class="data_intro"><h1>テスト未勝利</h1><p>{course}</p></div>
      <table class="race_table_01">
        <tr>
          <th>着順</th><th>枠番</th><th>馬番</th><th>馬名</th><th>性齢</th>
          <th>斤量</th><th>騎手</th><th>タイム</th><th>着差</th>
          <th>通過</th><th>上り</th><th>単勝</th><th>人気</th>
          <th>馬体重</th><th>調教師</th>
        </tr>
        <tr><td>1</td><td>1</td><td>2</td><td><a>勝ち馬</a></td><td>牡3</td>
          <td>57.0</td><td><a>東騎手</a></td><td>1:34.5</td><td></td>
          <td>2-2</td><td>34.5</td><td>3.7</td><td>2</td>
          <td>472(+2)</td><td>[東] 東調教師</td></tr>
        <tr><td>2</td><td>2</td><td>5</td><td><a>二着馬</a></td><td>牝3</td>
          <td>55.0</td><td><a>西騎手</a></td><td>1:34.7</td><td>1</td>
          <td>5-4</td><td>34.3</td><td>2.5</td><td>1</td>
          <td>450(-4)</td><td>[西] 西調教師</td></tr>
        <tr><td>3</td><td>3</td><td>8</td><td><a>三着馬</a></td><td>セ4</td>
          <td>58.0</td><td><a>地騎手</a></td><td>1:35.0</td><td>2</td>
          <td>8-7</td><td>34.6</td><td>8.0</td><td>3</td>
          <td>500(0)</td><td>[地] 地方調教師</td></tr>
        <tr><td>取消</td><td>4</td><td>9</td><td>取消馬</td><td>牡3</td>
          <td>57</td><td>騎手</td><td></td><td></td><td></td><td></td>
          <td>---</td><td></td><td>480(0)</td><td>[東] 調教師</td></tr>
      </table>
      <table class="pay_table_01">
        <tr><th>単勝</th><td>2</td><td>{win_pay}</td></tr>
        <tr><th>複勝</th><td>2<br/>5<br/>8</td><td>150<br/>120<br/>200</td></tr>
      </table>
    </body></html>
    """


def _create_runs(connection):
    definitions = ",".join(f"{column} TEXT" for column in RUN_COLUMNS)
    connection.execute(f"CREATE TABLE runs ({definitions})")


def test_parse_result_page_to_target_compatible_rows():
    rows = parse_race_html(_race_html(), "20260104", "中山", 1)

    assert len(rows) == 3
    winner = dict(zip(RUN_COLUMNS, rows[0]))
    second = dict(zip(RUN_COLUMNS, rows[1]))
    assert winner["horse"] == "勝ち馬"
    assert winner["sex"] == "牡"
    assert winner["age"] == 3
    assert winner["total_horses"] == 3
    assert winner["track_type"] == "芝"
    assert winner["condition"] == "良"
    assert winner["time_sec"] == 94.5
    assert winner["c4"] == 2
    assert winner["affi"] == "美浦"
    assert winner["win_pay"] == "370"
    assert winner["fukusho_pay"] == 150.0
    assert winner["pci"] is not None
    assert second["win_pay"] == "(2.5)"
    assert second["affi"] == "栗東"


def test_payout_mismatch_rejects_whole_race():
    with pytest.raises(RacePageError) as exc_info:
        parse_race_html(_race_html(win_pay="500"), "20260104", "中山", 1)

    assert exc_info.value.kind == "verify"


def test_obstacle_race_is_excluded():
    with pytest.raises(RacePageError) as exc_info:
        parse_race_html(
            _race_html(course="障害2880m / 天候 : 晴 / 障害 : 良"),
            "20260104", "中山", 4,
        )

    assert exc_info.value.kind == "obstacle"


def test_insert_is_race_level_idempotent():
    connection = sqlite3.connect(":memory:")
    _create_runs(connection)
    rows = parse_race_html(_race_html(), "20260104", "中山", 1)

    assert insert_race(connection, rows) is True
    assert insert_race(connection, rows) is False
    assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
