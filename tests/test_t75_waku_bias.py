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
