"""WIN5 の点数オプション (2026-09-25: 300/400点をユーザー要望で追加)。

- index_win5.html の点数ラジオに 50/100/150/200/300/400/500 が揃っていること。
- ラジオで選べる点数のうち prob 配分の「今週の確信度」参考分布 (win5_weights.json
  allocation.est_reference) を持つものが 100 以上の全点数であること
  (T68 の中央値超え通知は est_reference のある点数でしか発火しないため)。
- backtest_ml.py / backtest_win5.py のシミュレーション点数リストにも 300/400 が含まれること。
"""
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(BASE_DIR, rel), "rb") as f:
        return f.read().decode("utf-8")


def _radio_points():
    html = _read("index_win5.html")
    return sorted(int(v) for v in re.findall(r'name="pts" value="(\d+)"', html))


def test_points_radio_includes_300_and_400():
    assert _radio_points() == [50, 100, 150, 200, 300, 400, 500]


def test_est_reference_covers_every_radio_budget_of_100_or_more():
    cfg = json.loads(_read("api/data_files/common/win5_weights.json"))
    ref = cfg["allocation"]["est_reference"]
    for pts in _radio_points():
        if pts >= 100:
            assert str(pts) in ref, f"est_reference lacks {pts}点"
            median, p75 = ref[str(pts)]
            assert 0.0 < median <= p75 < 1.0


def test_backtests_simulate_300_and_400():
    assert "budgets=[50, 100, 150, 200, 300, 400, 500]" in _read("backtest_ml.py")
    assert "[50, 100, 150, 200, 300, 400, 500]" in _read("backtest_win5.py")
