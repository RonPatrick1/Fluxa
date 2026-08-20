from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('video', 'music')),
    root_path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_scan_at TEXT,
    last_scan_error TEXT
);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    title TEXT NOT NULL,
    sort_title TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('video', 'audio')),
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
    last_seen_scan TEXT NOT NULL,
    probe_status TEXT NOT NULL DEFAULT 'pending',
    probe_error TEXT,
    duration_ms INTEGER,
    container TEXT,
    video_codec TEXT,
    width INTEGER,
    height INTEGER,
    audio_codec TEXT,
    audio_channels INTEGER,
    audio_tracks INTEGER,
    subtitle_tracks INTEGER,
    chapters_json TEXT,
    subtitle_streams_json TEXT,
    show_title TEXT,
    season_number INTEGER,
    episode_number INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS media_library_idx ON media(library_id, available);
CREATE INDEX IF NOT EXISTS media_sort_idx ON media(sort_title);
CREATE INDEX IF NOT EXISTS media_show_idx
    ON media(show_title, season_number, episode_number);

CREATE TABLE IF NOT EXISTS playback_progress (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    position_ms INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS loudness_analyses (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    analyzer_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    integrated_lufs REAL,
    true_peak_db REAL,
    sample_interval_ms INTEGER,
    envelope_json TEXT,
    spike_segments_json TEXT,
    error TEXT,
    analyzed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS global_compressor_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1,
    threshold_db REAL NOT NULL DEFAULT -24,
    ratio REAL NOT NULL DEFAULT 8,
    ceiling_db REAL NOT NULL DEFAULT -3,
    attack_ms REAL NOT NULL DEFAULT 15,
    release_ms REAL NOT NULL DEFAULT 750,
    knee REAL NOT NULL DEFAULT 4,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO global_compressor_settings(id) VALUES (1);

CREATE TABLE IF NOT EXISTS media_compressor_settings (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL,
    threshold_db REAL NOT NULL,
    ratio REAL NOT NULL,
    ceiling_db REAL NOT NULL,
    attack_ms REAL NOT NULL,
    release_ms REAL NOT NULL,
    knee REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    sort_title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'mixed' CHECK (kind IN ('video', 'audio', 'mixed')),
    source_item_count INTEGER NOT NULL DEFAULT 0,
    matched_item_count INTEGER NOT NULL DEFAULT 0,
    source_updated_at INTEGER,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS playlists_sort_idx ON playlists(sort_title, id);

CREATE TABLE IF NOT EXISTS playlist_items (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    source_item_id TEXT,
    source_order REAL,
    source_path TEXT,
    source_title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(playlist_id, position)
);

CREATE INDEX IF NOT EXISTS playlist_items_playlist_idx
    ON playlist_items(playlist_id, position);
CREATE INDEX IF NOT EXISTS playlist_items_media_idx ON playlist_items(media_id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(media)")
            }
            if "chapters_json" not in columns:
                connection.execute("ALTER TABLE media ADD COLUMN chapters_json TEXT")
            if "subtitle_streams_json" not in columns:
                connection.execute("ALTER TABLE media ADD COLUMN subtitle_streams_json TEXT")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
