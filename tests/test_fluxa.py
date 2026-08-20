from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fluxa.auth import AuthManager, AuthenticationError
from fluxa.config import FluxaConfig, LibraryConfig
from fluxa.database import Database
from fluxa.library import (
    LibraryScanner,
    clean_title,
    episode_identity,
    episode_title,
    show_title_from_path,
)
from fluxa.loudness import LoudnessSample, build_gain_envelope, detect_spike_segments
from fluxa.media import run_ffprobe
from fluxa.plex import import_plex_playlists
from fluxa.transcode import CompatibilityManager, _video_rates
from server import RangeNotSatisfiable, parse_byte_range, request_origin_allowed


class LibraryScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.database = Database(self.root / "data" / "fluxa.db")
        self.database.initialize()
        self.config = FluxaConfig(
            config_path=self.root / "fluxa.json",
            host="127.0.0.1",
            port=8097,
            data_dir=self.root / "data",
            libraries=(LibraryConfig("Test videos", "video", self.media_root),),
        )
        self.scanner = LibraryScanner(self.database, self.config)
        self.scanner.sync_libraries()

    def tearDown(self):
        self.temporary.cleanup()

    def test_discovers_media_without_copying_it(self):
        source = self.media_root / "Example.Show.S02E03.mkv"
        unrelated_audio = self.media_root / "Soundtrack.mp3"
        source.write_bytes(b"not a real video")
        unrelated_audio.write_bytes(b"not part of a video library")

        result = self.scanner.scan_all()[0]

        self.assertEqual(result.discovered, 1)
        self.assertEqual(result.added, 1)
        self.assertTrue(source.exists())
        self.assertTrue(unrelated_audio.exists())
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM media").fetchone()
        self.assertEqual(row["path"], str(source))
        self.assertEqual(row["show_title"], "Example Show")
        self.assertEqual(row["season_number"], 2)
        self.assertEqual(row["episode_number"], 3)
        self.assertLess(self.database.path.stat().st_size, 1_000_000)

    def test_marks_missing_files_unavailable_after_complete_scan(self):
        source = self.media_root / "Temporary.mp4"
        source.write_bytes(b"media")
        self.scanner.scan_all()
        source.unlink()

        result = self.scanner.scan_all()[0]

        self.assertEqual(result.unavailable, 1)
        with self.database.connect() as connection:
            available = connection.execute("SELECT available FROM media").fetchone()[0]
        self.assertEqual(available, 0)

    def test_changed_file_returns_to_pending_probe(self):
        source = self.media_root / "Changed.mp4"
        source.write_bytes(b"first")
        self.scanner.scan_all()
        with self.database.connect() as connection:
            connection.execute("UPDATE media SET probe_status = 'done'")
        source.write_bytes(b"second version")
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

        result = self.scanner.scan_all()[0]

        self.assertEqual(result.changed, 1)
        with self.database.connect() as connection:
            status = connection.execute("SELECT probe_status FROM media").fetchone()[0]
        self.assertEqual(status, "pending")

    def test_disc_rip_filename_wins_over_generic_embedded_title(self):
        source = self.media_root / "Resident Alien" / "001-S1D1-Pilot.mkv"
        source.parent.mkdir()
        source.write_bytes(b"video")
        self.scanner.scan_all()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE media SET title = ?, sort_title = ?, probe_status = 'done'",
                ("Resident Alien: Season One (Disc 1)", "resident alien season one"),
            )

        self.scanner.scan_all()

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT title, show_title, season_number FROM media"
            ).fetchone()
        self.assertEqual(row["title"], "Pilot")
        self.assertEqual(row["show_title"], "Resident Alien")
        self.assertEqual(row["season_number"], 1)


class MediaTests(unittest.TestCase):
    def test_reads_audio_metadata_with_ffprobe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "silence.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48_000)
                output.writeframes(b"\0\0\0\0" * 48_000)

            result = run_ffprobe(path)

        self.assertEqual(result.audio_codec, "pcm_s16le")
        self.assertEqual(result.audio_channels, 2)
        self.assertEqual(result.duration_ms, 1000)
        self.assertEqual(result.chapters, ())
        self.assertEqual(result.subtitles, ())

    def test_reads_chapters_and_caption_track_types(self):
        payload = {
            "format": {"duration": "600.0", "format_name": "matroska"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "subrip",
                    "tags": {"language": "eng", "title": "English SDH"},
                    "disposition": {"default": 1, "hearing_impaired": 1},
                },
                {
                    "index": 4,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "eng"},
                    "disposition": {},
                },
            ],
            "chapters": [
                {
                    "start_time": "0.0",
                    "end_time": "90.5",
                    "tags": {"title": "Opening"},
                }
            ],
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with patch("fluxa.media.subprocess.run", return_value=completed):
            result = run_ffprobe(Path("example.mkv"))

        self.assertEqual(result.chapters[0].title, "Opening")
        self.assertEqual(result.chapters[0].end_ms, 90_500)
        self.assertEqual(result.subtitles[0].kind, "text")
        self.assertTrue(result.subtitles[0].hearing_impaired)
        self.assertEqual(result.subtitles[1].kind, "bitmap")

    def test_parses_http_byte_ranges(self):
        self.assertEqual(parse_byte_range("bytes=10-19", 100), (10, 19))
        self.assertEqual(parse_byte_range("bytes=90-", 100), (90, 99))
        self.assertEqual(parse_byte_range("bytes=-10", 100), (90, 99))
        with self.assertRaises(RangeNotSatisfiable):
            parse_byte_range("bytes=100-110", 100)


class CompatibilityStreamTests(unittest.TestCase):
    def test_selects_bounded_video_rates_by_resolution(self):
        self.assertEqual(_video_rates(720, 480), ("1600k", "2400k", "4800k"))
        self.assertEqual(_video_rates(1920, 1080), ("6000k", "8000k", "16000k"))

    def test_offsets_predictive_gain_commands_when_resuming(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CompatibilityManager(Path(directory))
            session = manager.root / "test-session"
            session.mkdir()
            command_file = manager._write_leveler_commands(
                session,
                15_000,
                "done",
                "[[0,0.0],[10000,-6.0],[20000,-6.0],[30000,0.0]]",
            )
            self.assertIsNotNone(command_file)
            commands = command_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual(commands[0], "0.000 volume@leveler volume 0.50118723;")
        self.assertEqual(commands[-1], "15.000 volume@leveler volume 1.00000000;")

    def test_live_leveler_and_bitmap_captions_are_in_the_ffmpeg_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CompatibilityManager(Path(directory))
            command = manager._command(
                source=Path("episode.mkv"),
                directory=manager.root,
                start_ms=0,
                width=1920,
                height=1080,
                encoder="h264_nvenc",
                leveler_path=None,
                segment_format="fmp4",
                subtitle_ordinal=0,
                subtitle_kind="bitmap",
            )

        self.assertIn("dynaudnorm", command[command.index("-af") + 1])
        self.assertIn("[0:s:0]", command[command.index("-filter_complex") + 1])
        self.assertEqual(command[command.index("-forced-idr") + 1], "1")
        self.assertEqual(command[command.index("-progress") + 1], "pipe:2")
        self.assertEqual(command[command.index("-max_interleave_delta") + 1], "0")


class AuthenticationTests(unittest.TestCase):
    def test_password_and_signed_session_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AuthManager(Path(directory))
            manager.set_password("a-long-household-password")

            self.assertTrue(manager.verify_password("a-long-household-password"))
            self.assertFalse(manager.verify_password("wrong-password"))
            token = manager.issue_session(now=1_000)
            self.assertTrue(manager.verify_session(token, now=1_001))
            self.assertFalse(manager.verify_session(token, now=99_999_999))
            self.assertEqual(manager.path.stat().st_mode & 0o777, 0o600)

    def test_rejects_short_password(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AuthManager(Path(directory))
            with self.assertRaises(AuthenticationError):
                manager.set_password("too-short")

    def test_same_origin_login_evidence_allows_privacy_browsers(self):
        expected = "https://patrick-lamphier.com"
        self.assertTrue(
            request_origin_allowed(
                origin=None,
                referer=None,
                fetch_site="same-origin",
                expected=expected,
                require_evidence=True,
            )
        )
        self.assertTrue(
            request_origin_allowed(
                origin=None,
                referer="https://patrick-lamphier.com/fluxa/login",
                fetch_site=None,
                expected=expected,
                require_evidence=True,
            )
        )
        self.assertFalse(
            request_origin_allowed(
                origin="https://example.net",
                referer=None,
                fetch_site="same-origin",
                expected=expected,
                require_evidence=True,
            )
        )
        self.assertFalse(
            request_origin_allowed(
                origin=None,
                referer=None,
                fetch_site="cross-site",
                expected=expected,
                require_evidence=True,
            )
        )


class PlexPlaylistImportTests(unittest.TestCase):
    def test_imports_order_and_preserves_unavailable_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            available_path = media_root / "Available Episode.mkv"
            available_path.write_bytes(b"media stays here")
            database = Database(root / "fluxa.db")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO libraries(name, kind, root_path) VALUES ('Videos', 'video', ?)",
                    (str(media_root),),
                )
                connection.execute(
                    """
                    INSERT INTO media(
                        library_id, path, relative_path, file_name, title, sort_title,
                        media_type, extension, size_bytes, modified_ns, last_seen_scan
                    ) VALUES (1, ?, 'Available Episode.mkv', 'Available Episode.mkv',
                              'Available Episode', 'available episode', 'video', '.mkv',
                              16, 1, 'test')
                    """,
                    (str(available_path),),
                )

            plex_path = root / "plex.db"
            with sqlite3.connect(plex_path) as plex:
                plex.executescript(
                    """
                    CREATE TABLE metadata_items(
                        id INTEGER PRIMARY KEY, metadata_type INTEGER, title TEXT,
                        title_sort TEXT, media_item_count INTEGER, updated_at INTEGER,
                        deleted_at INTEGER
                    );
                    CREATE TABLE play_queue_generators(
                        id INTEGER PRIMARY KEY, playlist_id INTEGER,
                        metadata_item_id INTEGER, "order" REAL
                    );
                    CREATE TABLE media_items(
                        id INTEGER PRIMARY KEY, metadata_item_id INTEGER, deleted_at INTEGER
                    );
                    CREATE TABLE media_parts(
                        id INTEGER PRIMARY KEY, media_item_id INTEGER, file TEXT,
                        "index" INTEGER, deleted_at INTEGER
                    );
                    """
                )
                plex.executemany(
                    "INSERT INTO metadata_items VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    [
                        (100, 15, "My Playlist", "My Playlist", 2, 1234),
                        (200, 1, "Available Episode", "Available Episode", 1, 1234),
                        (201, 1, "Missing Episode", "Missing Episode", 1, 1234),
                    ],
                )
                plex.executemany(
                    "INSERT INTO play_queue_generators VALUES (?, 100, ?, ?)",
                    [(1, 200, 20.0), (2, 201, 10.0)],
                )
                plex.executemany(
                    "INSERT INTO media_items VALUES (?, ?, NULL)",
                    [(300, 200), (301, 201)],
                )
                plex.executemany(
                    "INSERT INTO media_parts VALUES (?, ?, ?, 0, NULL)",
                    [
                        (400, 300, str(available_path)),
                        (401, 301, str(media_root / "Missing Episode.mkv")),
                    ],
                )

            result = import_plex_playlists(database, plex_path)
            second_result = import_plex_playlists(database, plex_path)

            self.assertEqual((result.playlists, result.items), (1, 2))
            self.assertEqual((result.matched, result.unavailable), (1, 1))
            self.assertEqual(second_result, result)
            with database.connect() as connection:
                playlists = connection.execute("SELECT * FROM playlists").fetchall()
                items = connection.execute(
                    "SELECT * FROM playlist_items ORDER BY position"
                ).fetchall()
            self.assertEqual(len(playlists), 1)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["source_title"], "Missing Episode")
            self.assertIsNone(items[0]["media_id"])
            self.assertEqual(items[1]["source_title"], "Available Episode")
            self.assertIsNotNone(items[1]["media_id"])
            self.assertEqual(available_path.read_bytes(), b"media stays here")


class LoudnessTests(unittest.TestCase):
    def test_predictive_envelope_reduces_gain_before_a_sustained_spike(self):
        samples = []
        for second in range(110):
            level = -30.0 if second < 40 or second > 80 else -14.0
            samples.append(LoudnessSample(second * 1000, level, level))

        segments = detect_spike_segments(samples)
        envelope = build_gain_envelope(samples, segments)

        self.assertEqual(len(segments), 1)
        self.assertLess(segments[0].attack_start_ms, segments[0].loud_start_ms)
        self.assertGreaterEqual(segments[0].reduction_db, 10)
        gains = dict(envelope)
        self.assertLess(gains[35_000], 0)
        self.assertLessEqual(gains[40_000], -10)
        self.assertEqual(envelope[-1][1], 0)

    def test_title_helpers_are_predictable(self):
        self.assertEqual(clean_title("A.Show_Name"), "A Show Name")
        self.assertEqual(episode_identity("A.Show.S01E09.Title"), ("A Show", 1, 9))
        self.assertEqual(episode_title("233-S10E23D06-Double Blind"), "Double Blind")
        self.assertEqual(
            show_title_from_path(Path("NCIS/Files/233-S10E23-Double Blind.mkv"), "233"),
            "NCIS",
        )


if __name__ == "__main__":
    unittest.main()
