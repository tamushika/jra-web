import sqlite3

import extend_ability_from_neon as subject


def test_restore_supplemental_fields_preserves_local_weight_only_for_neon_null():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE runs (date TEXT, place TEXT, r INTEGER, umaban INTEGER, "
        "kinryo REAL, fukusho_pay TEXT)"
    )
    connection.executemany(
        "INSERT INTO runs VALUES (?,?,?,?,?,?)",
        [
            ("20260601", "東京", 1, 1, None, None),
            ("20260601", "東京", 1, 2, 58.0, None),
        ],
    )
    subject.restore_supplemental_fields(
        connection.cursor(),
        [("(120)", "20260601", "東京", 1, 1)],
        [
            (55.0, "20260601", "東京", 1, 1),
            (54.0, "20260601", "東京", 1, 2),
        ],
    )
    rows = connection.execute(
        "SELECT umaban, kinryo, fukusho_pay FROM runs ORDER BY umaban"
    ).fetchall()
    assert rows == [(1, 55.0, "(120)"), (2, 58.0, None)]


def test_candidate_safe_sync_inserts_only_new_keys_and_preserves_existing():
    connection = sqlite3.connect(":memory:")
    columns = subject.RUN_COLS
    definitions = ",".join(f'"{name}"' for name in columns)
    connection.execute(f"CREATE TABLE runs ({definitions})")

    def row(date, horse, number):
        values = dict.fromkeys(columns)
        values.update(date=date, place="東京", r=1, umaban=number, horse=horse)
        return tuple(values[name] for name in columns)

    connection.execute(
        f"INSERT INTO runs ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
        row("20260601", "既存馬", 1),
    )
    incoming = [row("20260601", "上流で変わった名前", 1), row("20260705", "新規馬", 2)]
    added, retained = subject.insert_new_rows_only(connection.cursor(), incoming)
    assert (added, retained) == (1, 1)
    assert connection.execute(
        "SELECT date, horse FROM runs ORDER BY date"
    ).fetchall() == [("20260601", "既存馬"), ("20260705", "新規馬")]
