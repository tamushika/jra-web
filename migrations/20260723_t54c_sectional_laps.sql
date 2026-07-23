CREATE TABLE IF NOT EXISTS runner_corners (
    date TEXT NOT NULL,
    place TEXT NOT NULL,
    r INTEGER NOT NULL,
    umaban INTEGER NOT NULL,
    corner_positions_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (date, place, r, umaban)
);

CREATE TABLE IF NOT EXISTS race_laps (
    date TEXT NOT NULL,
    place TEXT NOT NULL,
    r INTEGER NOT NULL,
    lap_sequence_json TEXT NOT NULL,
    distance INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (date, place, r)
);
