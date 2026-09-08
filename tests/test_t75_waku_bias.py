"""SPEC-T75 (競馬場ごとの枠 (内・外) バイアス強度表示) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T75-waku-bias-strength.md §4
  - compute_waku_bias の単体テスト (a)〜(e)
  - get_track_bias_data を一時SQLite DBで通しテストし、waku_bias と既存キーが両方あること
"""
import os
import sqlite3
import sys

import pytest

API_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

import past_data_service as pds  # noqa: E402


# ─── テストデータビルダ ──────────────────────────────────────────────────────
# 16頭立てレースでの calculate_waku(u, 16) の実際の対応:
#   1→1枠, 2→2枠, 3→3枠, 4→4枠, 5〜7→5枠, 8〜10→6枠, 11〜13→7枠, 14〜16→8枠
# → 内(1〜3枠) = 馬番{1,2,3} / 中(4〜5枠) = 馬番{4,5,6,7} / 外(6〜8枠) = 馬番{8..16}

# 6レース分の3着以内馬番 (内寄りに集中させたシナリオ (a))
_PODIUMS_INNER_HEAVY = [
    [1, 2, 3],
    [1, 2, 16],
    [1, 2, 3],
    [1, 2, 16],
    [1, 2, 3],
    [1, 2, 16],
]

# 6レース分の3着以内馬番 (フラットに近いシナリオ (b): in=3, mid=4, out=11)
_PODIUMS_FLAT = [
    [8, 9, 10],
    [11, 12, 13],
    [1, 4, 14],
    [2, 5, 15],
    [3, 6, 16],
    [7, 8, 9],
]


def _make_rows(podiums, track_type="芝", total_horses=16, distance=1600,
               race_name_prefix="テストR", kaisai="1回中山1日"):
    """compute_waku_bias 用の行 (dict) リストを生成する。

    各レースにつき馬番1..total_horses の全頭を1行ずつ作り、podium (rank1,2,3の馬番リスト)
    に含まれる馬は rank 1/2/3、それ以外は rank 4 (着順としては雑だが top3判定にのみ使う)。
    """
    rows = []
    for i, podium in enumerate(podiums):
        race_name = f"{race_name_prefix}{i+1}"
        for u in range(1, total_horses + 1):
            if u in podium:
                rank = podium.index(u) + 1
            else:
                rank = 4
            rows.append({
                "rank": rank,
                "track_type": track_type,
                "horse_number": u,
                "corner_4": "5",
                "popularity": u,
                "total_horses": total_horses,
                "time": "1:34.5" if rank == 1 else None,
                "distance": distance,
                "race_name": race_name,
                "kaisai": kaisai,
            })
    return rows


# ─── (a) 内枠集中 → 内・中/強 ────────────────────────────────────────────────

def test_compute_waku_bias_inner_heavy_detects_inner_bias():
    rows = _make_rows(_PODIUMS_INNER_HEAVY)
    result = pds.compute_waku_bias(rows)

    shiba = result["芝"]
    assert shiba["direction"] == "内"
    assert shiba["level"] in ("中", "強")
    assert shiba["z"] > 0
    assert shiba["races"] == 6
    assert shiba["in"]["index"] > 1.0
    assert shiba["out"]["index"] < 1.0

    # ダートは行が無いのでデータ不足になる
    assert result["ダート"]["level"] == "データ不足"
    assert result["ダート"]["direction"] == "なし"


# ─── (b) フラット → 弱 ──────────────────────────────────────────────────────

def test_compute_waku_bias_flat_distribution_is_weak():
    rows = _make_rows(_PODIUMS_FLAT)
    result = pds.compute_waku_bias(rows)

    shiba = result["芝"]
    assert shiba["level"] == "弱"
    assert shiba["direction"] == "なし"
    assert abs(shiba["z"]) < 1.0


# ─── (c) レース数不足 → データ不足 ──────────────────────────────────────────

def test_compute_waku_bias_too_few_races_is_insufficient():
    rows = _make_rows(_PODIUMS_INNER_HEAVY[:2])
    result = pds.compute_waku_bias(rows)

    shiba = result["芝"]
    assert shiba["level"] == "データ不足"
    assert shiba["direction"] == "なし"
    assert shiba["races"] == 2
    assert "データ不足" in shiba["label"]
    assert "2R" in shiba["label"]


# ─── (d) 8頭立てのみ (枠=馬番) でも動く ──────────────────────────────────────

def test_compute_waku_bias_handles_small_fields_of_eight():
    # 8頭立て×5レース。内(1〜3),中(4〜5),外(6〜8)。
    podiums_8 = [
        [1, 2, 3],
        [1, 2, 8],
        [1, 2, 3],
        [1, 2, 8],
        [1, 2, 3],
    ]
    rows = _make_rows(podiums_8, total_horses=8)
    result = pds.compute_waku_bias(rows)

    shiba = result["芝"]
    # データ不足にならず、判定が返ること (in/out の頭数は 5*3=15 >= 10)
    assert shiba["level"] in ("弱", "中", "強")
    assert shiba["in"]["n"] == 15
    assert shiba["out"]["n"] == 15
    assert shiba["races"] == 5


# ─── (e) 障害レース・rank非数値の行が除外される ──────────────────────────────

def test_compute_waku_bias_excludes_hurdle_and_non_numeric_rank():
    base_rows = _make_rows(_PODIUMS_INNER_HEAVY)
    baseline = pds.compute_waku_bias(base_rows)

    extra_rows = list(base_rows)
    # 障害レース (race_name に「障」を含む) は除外されるべき
    extra_rows.append({
        "rank": 1, "track_type": "芝", "horse_number": 1, "corner_4": "1",
        "popularity": 1, "total_horses": 16, "time": "1:30.0", "distance": 3000,
        "race_name": "障害テストR", "kaisai": "1回中山1日",
    })
    # rank が非数値 (取消・中止等) の行は除外されるべき
    extra_rows.append({
        "rank": "中止", "track_type": "芝", "horse_number": 4, "corner_4": None,
        "popularity": 4, "total_horses": 16, "time": None, "distance": 1600,
        "race_name": "テストR7", "kaisai": "1回中山1日",
    })

    result = pds.compute_waku_bias(extra_rows)
    shiba = result["芝"]
    base_shiba = baseline["芝"]

    assert shiba["races"] == base_shiba["races"]
    assert shiba["in"] == base_shiba["in"]
    assert shiba["mid"] == base_shiba["mid"]
    assert shiba["out"] == base_shiba["out"]
    assert shiba["level"] == base_shiba["level"]
    assert shiba["direction"] == base_shiba["direction"]


# ─── compute_waku_bias が例外を握りつぶすこと ────────────────────────────────

def test_compute_waku_bias_never_raises_on_malformed_rows():
    malformed = [
        {"rank": None, "track_type": "芝"},
        {"rank": 1, "track_type": "芝", "horse_number": None, "total_horses": None,
         "race_name": "x", "kaisai": "y", "distance": 1600},
        "not-a-dict-like-row-but-should-not-crash-the-whole-call",
    ]
    # 想定外の行 (dict以外) が混じっても例外を外に投げず、surfaceごとに
    # データ不足へフォールバックすること (compute_waku_bias 内部の try/except)。
    result = pds.compute_waku_bias(malformed)
    assert result["芝"]["level"] == "データ不足"
    assert result["ダート"]["level"] == "データ不足"


# ─── get_track_bias_data 通しテスト (一時SQLite DB) ─────────────────────────

_RACES_COLUMNS = [
    "date", "kaisai", "rank", "track_type", "distance", "condition",
    "horse_number", "corner_4", "jockey", "time", "agari_3f", "popularity",
    "odds", "race_name", "weight", "total_horses", "place", "race_class",
]


@pytest.fixture()
def sqlite_bias_db(tmp_path):
    db_path = str(tmp_path / "past_data_v2_t75.db")
    conn = sqlite3.connect(db_path)
    conn.execute(f"""
        CREATE TABLE races (
            {", ".join(f"{c} TEXT" for c in _RACES_COLUMNS)}
        )
    """)

    from datetime import datetime
    today = datetime.now()
    date_str = today.strftime("%y%m%d")  # YYMMDD

    place = "中山"
    kaisai = "1回中山1日"
    rows_to_insert = []
    for i, podium in enumerate(_PODIUMS_INNER_HEAVY):
        race_name = f"テストR{i+1}"
        for u in range(1, 17):
            rank = (podium.index(u) + 1) if u in podium else (u + 3 if u + 3 <= 16 else 16)
            rows_to_insert.append({
                "date": date_str, "kaisai": kaisai, "rank": str(rank),
                "track_type": "芝", "distance": "1600", "condition": "良",
                "horse_number": str(u), "corner_4": "5", "jockey": "テスト騎手",
                "time": "1:34.5" if rank == 1 else "1:36.0",
                "agari_3f": "35.0", "popularity": str(u), "odds": "5.0",
                "race_name": race_name, "weight": "480", "total_horses": "16",
                "place": place, "race_class": "3勝クラス",
            })

    cols = _RACES_COLUMNS
    placeholders = ", ".join(["?"] * len(cols))
    conn.executemany(
        f"INSERT INTO races ({', '.join(cols)}) VALUES ({placeholders})",
        [[r[c] for c in cols] for r in rows_to_insert],
    )
    conn.commit()
    conn.close()

    return db_path, place, date_str


def test_get_track_bias_data_includes_waku_bias_and_existing_keys(monkeypatch, sqlite_bias_db):
    db_path, place, date_str = sqlite_bias_db

    def _fake_get_db_connection(base_dir):
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(pds, "get_db_connection", _fake_get_db_connection)

    data = pds.get_track_bias_data("dummy_base_dir", place)

    assert "error" not in data
    assert data["success"] is True
    assert data["latest_date"] == date_str
    assert data["place"] == place

    # 既存キーは不変であること
    assert "evaluations" in data
    assert "race_details" in data
    assert "track_speed" in data
    assert "芝" in data["evaluations"]

    # 新規キー
    assert "waku_bias" in data
    assert "芝" in data["waku_bias"]
    assert "ダート" in data["waku_bias"]
    shiba_wb = data["waku_bias"]["芝"]
    assert shiba_wb["level"] in ("弱", "中", "強", "データ不足")
    assert "in" in shiba_wb and "out" in shiba_wb and "mid" in shiba_wb
    assert "label" in shiba_wb


# ═══════════════════════════════════════════════════════════════════════════
# SPEC-T75b: 直近複数開催日 (近い日ほど重視) での枠バイアス判定
#   対応する仕様: docs/codex/SPEC-T75b-waku-bias-multiday.md §3
# ═══════════════════════════════════════════════════════════════════════════

# 内枠集中の6レース分 (最新日用)
_PODIUMS_INNER_HEAVY_T75B = [
    [1, 2, 3], [1, 2, 16], [1, 2, 3], [1, 2, 16], [1, 2, 3], [1, 2, 16],
]

# 外枠集中の6レース分 (過去日用): 16頭立てで馬番14,15,16 (waku=8=外) に集中
_PODIUMS_OUTER_HEAVY_T75B = [
    [14, 15, 16], [14, 15, 1], [14, 15, 16], [14, 15, 1], [14, 15, 16], [14, 15, 1],
]


def _make_rows_dated(podiums, date, track_type="芝", total_horses=16, distance=1600,
                      race_name_prefix="テストR", kaisai="1回中山1日"):
    """compute_waku_bias 用の行 (dict) リストを生成する (date 列付き)。"""
    rows = []
    for i, podium in enumerate(podiums):
        race_name = f"{race_name_prefix}{i+1}_{date}"
        for u in range(1, total_horses + 1):
            rank = (podium.index(u) + 1) if u in podium else 4
            rows.append({
                "rank": rank, "track_type": track_type, "horse_number": u,
                "total_horses": total_horses, "distance": distance,
                "race_name": race_name, "kaisai": kaisai, "date": date,
            })
    return rows


def test_compute_waku_bias_multiday_weights_recent_day_more_heavily():
    """最新日=内枠集中・10日前=外枠集中 (頭数同じ) → 加重により内が優勢になり、
    全日を等重み (half_life_days を極大化) にした場合と |z| が異なること。"""
    latest_date = "260906"
    old_date = "260827"  # 10日前
    rows = (_make_rows_dated(_PODIUMS_INNER_HEAVY_T75B, latest_date)
            + _make_rows_dated(_PODIUMS_OUTER_HEAVY_T75B, old_date))

    weighted = pds.compute_waku_bias(rows, latest_date=latest_date, half_life_days=7.0)
    equal = pds.compute_waku_bias(rows, latest_date=latest_date, half_life_days=1e9)

    shiba_w = weighted["芝"]
    shiba_e = equal["芝"]

    # 加重ありでは直近日 (内枠集中) が優勢 → direction=内
    assert shiba_w["direction"] == "内"
    assert shiba_w["level"] in ("中", "強")

    # 等重み (half_life_days 極大) にした場合と |z| が異なること
    assert shiba_w["z"] != pytest.approx(shiba_e["z"])

    # days: 2件・重み降順 (直近日の重み=1.0が先頭)
    assert len(shiba_w["days"]) == 2
    assert shiba_w["days"][0]["date"] == latest_date
    assert shiba_w["days"][0]["weight"] == 1.0
    assert shiba_w["days"][1]["date"] == old_date
    assert 0.0 < shiba_w["days"][1]["weight"] < 1.0
    assert shiba_w["days"][0]["weight"] >= shiba_w["days"][1]["weight"]

    assert shiba_w["window_days"] == 14
    assert shiba_w["half_life_days"] == 7.0


def test_compute_waku_bias_legacy_rows_without_date_default_to_weight_one():
    """date列の無い行だけを渡す旧形式の呼び出しが従来どおり動く (重み1.0扱い)。"""
    rows = _make_rows(_PODIUMS_INNER_HEAVY)  # 既存ヘルパ (date列なし)
    result = pds.compute_waku_bias(rows)

    shiba = result["芝"]
    assert shiba["direction"] == "内"
    assert shiba["level"] in ("中", "強")
    assert shiba["days"] == []  # date情報が無いので日別内訳は空


@pytest.fixture()
def sqlite_bias_db_multiday(tmp_path):
    """同一競馬場に D (最新) / D-1 / D-8 の3開催日を持つ一時SQLite DB。"""
    db_path = str(tmp_path / "past_data_v2_t75b.db")
    conn = sqlite3.connect(db_path)
    conn.execute(f"""
        CREATE TABLE races (
            {", ".join(f"{c} TEXT" for c in _RACES_COLUMNS)}
        )
    """)

    from datetime import datetime, timedelta
    today = datetime.now()
    date_d0 = today.strftime("%y%m%d")
    date_d1 = (today - timedelta(days=1)).strftime("%y%m%d")
    date_d8 = (today - timedelta(days=8)).strftime("%y%m%d")

    place = "中山"
    kaisai = "1回中山1日"
    rows_to_insert = []
    for date_str in (date_d0, date_d1, date_d8):
        for i, podium in enumerate(_PODIUMS_INNER_HEAVY):
            race_name = f"テストR{i+1}_{date_str}"
            for u in range(1, 17):
                rank = (podium.index(u) + 1) if u in podium else (u + 3 if u + 3 <= 16 else 16)
                rows_to_insert.append({
                    "date": date_str, "kaisai": kaisai, "rank": str(rank),
                    "track_type": "芝", "distance": "1600", "condition": "良",
                    "horse_number": str(u), "corner_4": "5", "jockey": "テスト騎手",
                    "time": "1:34.5" if rank == 1 else "1:36.0",
                    "agari_3f": "35.0", "popularity": str(u), "odds": "5.0",
                    "race_name": race_name, "weight": "480", "total_horses": "16",
                    "place": place, "race_class": "3勝クラス",
                })

    cols = _RACES_COLUMNS
    placeholders = ", ".join(["?"] * len(cols))
    conn.executemany(
        f"INSERT INTO races ({', '.join(cols)}) VALUES ({placeholders})",
        [[r[c] for c in cols] for r in rows_to_insert],
    )
    conn.commit()
    conn.close()

    return db_path, place, date_d0, date_d1, date_d8


def test_get_track_bias_data_aggregates_multiday_but_evaluations_stay_latest_only(
    monkeypatch, sqlite_bias_db_multiday
):
    db_path, place, date_d0, date_d1, date_d8 = sqlite_bias_db_multiday

    def _fake_get_db_connection(base_dir):
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(pds, "get_db_connection", _fake_get_db_connection)

    data = pds.get_track_bias_data("dummy_base_dir", place)

    assert "error" not in data
    assert data["latest_date"] == date_d0

    # waku_bias: 3開催日 (D, D-1, D-8) が全て14日以内 → 3日分・6R×3日=18R
    shiba_wb = data["waku_bias"]["芝"]
    assert shiba_wb["races"] == 18
    days = shiba_wb["days"]
    assert len(days) == 3
    assert {d["date"] for d in days} == {date_d0, date_d1, date_d8}
    # 重み降順 (直近日が先頭)
    assert days[0]["weight"] >= days[1]["weight"] >= days[2]["weight"]
    assert days[0]["date"] == date_d0
    assert days[0]["weight"] == 1.0

    # evaluations / race_details は従来通り最新日 (D) のみで計算されていること:
    # 最新日は6レース×3着=18行のみが top3 として race_details に残るはず
    assert len(data["race_details"]) == 18
    assert all(d["race_name"].endswith(f"_{date_d0}") for d in data["race_details"])
