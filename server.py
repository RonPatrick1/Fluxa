#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import html
import hmac
import ipaddress
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from fluxa import __version__
from fluxa.auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    AuthManager,
    AuthenticationError,
)
from fluxa.config import FluxaConfig, load_config
from fluxa.database import Database
from fluxa.library import AUDIO_EXTENSIONS, LibraryScanner, ScanResult
from fluxa.loudness import LoudnessError, analyze_media
from fluxa.media import ProbeError, content_type, probe_media
from fluxa.plex import (
    DEFAULT_PLEX_DATABASE,
    PlexImportError,
    import_plex_playlists,
)
from fluxa.transcode import (
    CompatibilityManager,
    TranscodeError,
    TranscodeNotFound,
    TranscodeNotReady,
)


DIRECT_PLAY_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/style.css": "style.css",
    "/script.js": "script.js",
    "/favicon.svg": "favicon.svg",
    "/vendor/hls.min.js": "vendor/hls.min.js",
}
MEDIA_ROUTE = re.compile(r"^/api/media/(?P<id>\d+)(?:/(?P<action>stream|thumbnail|compatibility|progress|probe|analyze|loudness|compressor-settings))?$")
PLAYLIST_ROUTE = re.compile(r"^/api/playlists/(?P<id>\d+)$")
COMPAT_FILE_ROUTE = re.compile(
    r"^/api/compat/(?P<session>[A-Za-z0-9_-]{20,40})/"
    r"(?P<file>index\.m3u8|init\.mp4|segment-\d{6}\.(?:ts|m4s))$"
)
COMPAT_CONTROL_ROUTE = re.compile(
    r"^/api/compat/(?P<session>[A-Za-z0-9_-]{20,40})/control$"
)
SUBTITLE_ROUTE = re.compile(
    r"^/api/media/(?P<id>\d+)/subtitles/(?P<ordinal>\d+)\.vtt$"
)
CHAPTER_THUMBNAIL_ROUTE = re.compile(
    r"^/api/media/(?P<id>\d+)/chapters/(?P<index>\d+)/thumbnail$"
)


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class RangeNotSatisfiable(ValueError):
    pass


class FluxaApp:
    def __init__(self, config: FluxaConfig) -> None:
        self.config = config
        self.database = Database(config.database_path)
        self.database.initialize()
        self.auth = AuthManager(config.data_dir)
        self.scanner = LibraryScanner(self.database, config)
        self.scanner.sync_libraries()
        self.public_dir = Path(__file__).resolve().parent
        self.scan_lock = threading.Lock()
        self.probe_lock = threading.Lock()
        self.probe_thread: threading.Thread | None = None
        self.probe_completed = 0
        self.analysis_lock = threading.Lock()
        self.analysis_jobs: set[int] = set()
        self.thumbnail_dir = config.data_dir / "thumbnails"
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnail_lock = threading.Lock()
        self.thumbnail_pending: set[int] = set()
        self.thumbnail_failed: set[int] = set()
        self.thumbnail_queue: queue.Queue[int] = queue.Queue()
        self.thumbnail_thread: threading.Thread | None = None
        self.caption_dir = config.data_dir / "captions"
        self.caption_dir.mkdir(parents=True, exist_ok=True)
        self.chapter_thumbnail_dir = config.data_dir / "chapter-thumbnails"
        self.chapter_thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self.chapter_thumbnail_lock = threading.Lock()
        self.chapter_thumbnail_pending: set[tuple[int, int]] = set()
        self.chapter_thumbnail_failed: set[tuple[int, int]] = set()
        self.chapter_thumbnail_queue: queue.Queue[tuple[int, int]] = queue.Queue()
        self.chapter_thumbnail_thread: threading.Thread | None = None
        self.log_dir = config.data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.playback_log_path = self.log_dir / "playback-events.jsonl"
        self.playback_log_lock = threading.Lock()
        self.artwork_backfill_lock = threading.Lock()
        self.artwork_backfill_thread: threading.Thread | None = None
        self.artwork_backfill_wake = threading.Event()
        self.artwork_backfill_completed = 0
        self.compatibility = CompatibilityManager(config.data_dir)
        self.login_lock = threading.Lock()
        self.failed_logins: dict[str, list[float]] = {}
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE media SET probe_status = 'pending' WHERE probe_status = 'probing'"
            )
            connection.execute(
                """
                UPDATE media SET probe_status = 'pending'
                WHERE available = 1 AND media_type = 'video'
                  AND probe_status = 'done'
                  AND (chapters_json IS NULL OR subtitle_streams_json IS NULL)
                """
            )

    def status(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            totals = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN available = 1 THEN 1 ELSE 0 END) AS available,
                       SUM(CASE WHEN media_type = 'video' AND available = 1 THEN 1 ELSE 0 END) AS videos,
                       SUM(CASE WHEN media_type = 'audio' AND available = 1 THEN 1 ELSE 0 END) AS audio,
                       SUM(CASE WHEN probe_status = 'done' AND available = 1 THEN 1 ELSE 0 END) AS probed
                FROM media
                """
            ).fetchone()
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        return {
            "name": "Fluxa",
            "version": __version__,
            "media": {
                "total": int(totals["total"] or 0),
                "available": int(totals["available"] or 0),
                "videos": int(totals["videos"] or 0),
                "audio": int(totals["audio"] or 0),
                "probed": int(totals["probed"] or 0),
            },
            "scanner": {"running": self.scan_lock.locked()},
            "probe": {
                "running": bool(self.probe_thread and self.probe_thread.is_alive()),
                "completed_this_run": self.probe_completed,
            },
            "analysis": {"running": sorted(self.analysis_jobs)},
            "thumbnails": {
                "pending": self.thumbnail_queue.qsize()
                + self.chapter_thumbnail_queue.qsize(),
                "episode_pending": self.thumbnail_queue.qsize(),
                "chapter_pending": self.chapter_thumbnail_queue.qsize(),
                "backfill_running": bool(
                    self.artwork_backfill_thread
                    and self.artwork_backfill_thread.is_alive()
                ),
                "backfill_completed": self.artwork_backfill_completed,
            },
            "compatibility": {
                "active_sessions": self.compatibility.active_count(),
                "temporary_bytes": self.compatibility.temporary_bytes(),
            },
            "tools": {"ffmpeg": bool(ffmpeg), "ffprobe": bool(ffprobe)},
            "logs": {
                "playback": str(self.playback_log_path),
                "transcodes": str(self.log_dir / "transcodes"),
                "service": "journalctl --user -u fluxa",
            },
            "storage": {
                "database_bytes": self.config.database_path.stat().st_size
                if self.config.database_path.exists()
                else 0,
                "media_copied_bytes": 0,
            },
        }

    def libraries(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT l.id, l.name, l.kind, l.root_path, l.enabled,
                       l.last_scan_at, l.last_scan_error,
                       COUNT(CASE WHEN m.available = 1 THEN 1 END) AS item_count,
                       COALESCE(SUM(CASE WHEN m.available = 1 THEN m.size_bytes ELSE 0 END), 0) AS total_bytes
                FROM libraries l
                LEFT JOIN media m ON m.library_id = l.id
                GROUP BY l.id
                ORDER BY l.kind DESC, l.name COLLATE NOCASE
                """
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "kind": row["kind"],
                "enabled": bool(row["enabled"]),
                "item_count": int(row["item_count"]),
                "total_bytes": int(row["total_bytes"]),
                "last_scan_at": row["last_scan_at"],
                "last_scan_error": row["last_scan_error"],
            }
            for row in rows
        ]

    def scan(self) -> list[ScanResult]:
        if not self.scan_lock.acquire(blocking=False):
            raise ApiError(HTTPStatus.CONFLICT, "A library scan is already running")
        try:
            results = self.scanner.scan_all()
            self.start_artwork_backfill()
            return results
        finally:
            self.scan_lock.release()

    def playlists(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT pl.id, pl.title, pl.kind, pl.source, pl.imported_at,
                       COUNT(pi.id) AS item_count,
                       COUNT(CASE WHEN m.available = 1 AND l.enabled = 1 THEN 1 END)
                           AS available_count
                FROM playlists pl
                LEFT JOIN playlist_items pi ON pi.playlist_id = pl.id
                LEFT JOIN media m ON m.id = pi.media_id
                LEFT JOIN libraries l ON l.id = m.library_id
                GROUP BY pl.id
                ORDER BY pl.sort_title COLLATE NOCASE, pl.id
                """
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "title": row["title"],
                "kind": row["kind"],
                "source": row["source"],
                "item_count": int(row["item_count"]),
                "available_count": int(row["available_count"]),
                "imported_at": row["imported_at"],
            }
            for row in rows
        ]

    def playlist_detail(
        self, playlist_id: int, query: dict[str, list[str]]
    ) -> dict[str, Any]:
        search = query.get("q", [""])[0].strip()
        parameters: list[Any] = [playlist_id]
        search_clause = ""
        if search:
            search_clause = (
                "AND (pi.source_title LIKE ? ESCAPE '\\' "
                "OR COALESCE(m.title, '') LIKE ? ESCAPE '\\')"
            )
            term = f"%{_escape_like(search)}%"
            parameters.extend((term, term))
        with self.database.connect() as connection:
            playlist = connection.execute(
                """
                SELECT id, title, kind, source, imported_at,
                       source_item_count, matched_item_count
                FROM playlists WHERE id = ?
                """,
                (playlist_id,),
            ).fetchone()
            if playlist is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Playlist not found")
            rows = connection.execute(
                f"""
                SELECT m.*, l.name AS library_name, l.enabled AS library_enabled,
                       progress.position_ms,
                       progress.duration_ms AS progress_duration_ms,
                       progress.completed,
                       analysis.status AS analysis_status,
                       analysis.spike_segments_json,
                       pi.position AS playlist_position,
                       pi.source_title,
                       pi.source_path
                FROM playlist_items pi
                LEFT JOIN media m ON m.id = pi.media_id
                LEFT JOIN libraries l ON l.id = m.library_id
                LEFT JOIN playback_progress progress ON progress.media_id = m.id
                LEFT JOIN loudness_analyses analysis ON analysis.media_id = m.id
                WHERE pi.playlist_id = ? {search_clause}
                ORDER BY pi.position
                """,
                tuple(parameters),
            ).fetchall()

        items: list[dict[str, Any]] = []
        available_count = 0
        for row in rows:
            playable = bool(
                row["id"] is not None
                and row["available"]
                and row["library_enabled"]
            )
            if playable:
                item = self._public_media(row)
                item["available"] = True
                available_count += 1
            else:
                source_path = Path(row["source_path"] or "")
                extension = source_path.suffix.casefold()
                item = {
                    "id": None,
                    "available": False,
                    "title": row["source_title"],
                    "file_name": source_path.name,
                    "media_type": "audio"
                    if extension in AUDIO_EXTENSIONS
                    else "video",
                    "extension": extension,
                    "duration_ms": 0,
                    "show_title": None,
                    "library_name": "Unavailable Plex item",
                    "progress": {"position_ms": 0, "duration_ms": 0, "completed": False},
                    "analysis": {"status": "unavailable", "spike_count": 0},
                }
            item["playlist_position"] = int(row["playlist_position"])
            items.append(item)
        return {
            "playlist": {
                "id": int(playlist["id"]),
                "title": playlist["title"],
                "kind": playlist["kind"],
                "source": playlist["source"],
                "imported_at": playlist["imported_at"],
            },
            "items": items,
            "total": len(items),
            "available": available_count,
        }

    def start_probing(self) -> bool:
        with self.probe_lock:
            if self.probe_thread and self.probe_thread.is_alive():
                return False
            self.probe_thread = threading.Thread(
                target=self._probe_worker,
                name="fluxa-metadata-probe",
                daemon=True,
            )
            self.probe_thread.start()
            return True

    def _probe_worker(self) -> None:
        while True:
            if self.compatibility.active_count():
                time.sleep(1)
                continue
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT id FROM media
                    WHERE available = 1 AND probe_status = 'pending'
                    ORDER BY CASE media_type WHEN 'video' THEN 0 ELSE 1 END,
                             size_bytes, id
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return
                media_id = int(row["id"])
                connection.execute(
                    "UPDATE media SET probe_status = 'probing' WHERE id = ?",
                    (media_id,),
                )
            try:
                probe_media(self.database, media_id)
                self._queue_known_chapter_thumbnails(media_id)
            except ProbeError:
                pass
            self.probe_completed += 1

    def media_list(self, query: dict[str, list[str]]) -> dict[str, Any]:
        search = query.get("q", [""])[0].strip()
        media_type = query.get("type", [""])[0].strip()
        library_id = _positive_int(query.get("library", [""])[0])
        limit = min(250, max(1, _positive_int(query.get("limit", ["120"])[0]) or 120))
        offset = max(0, _nonnegative_int(query.get("offset", ["0"])[0]) or 0)
        clauses = ["m.available = 1", "l.enabled = 1"]
        parameters: list[Any] = []
        if search:
            clauses.append("(m.title LIKE ? ESCAPE '\\' OR m.relative_path LIKE ? ESCAPE '\\')")
            term = f"%{_escape_like(search)}%"
            parameters.extend((term, term))
        if media_type in {"video", "audio"}:
            clauses.append("m.media_type = ?")
            parameters.append(media_type)
        if library_id is not None:
            clauses.append("m.library_id = ?")
            parameters.append(library_id)
        where = " AND ".join(clauses)
        with self.database.connect() as connection:
            count = connection.execute(
                f"SELECT COUNT(*) AS count FROM media m JOIN libraries l ON l.id = m.library_id WHERE {where}",
                tuple(parameters),
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT m.*, l.name AS library_name,
                       p.position_ms, p.duration_ms AS progress_duration_ms, p.completed,
                       a.status AS analysis_status, a.spike_segments_json
                FROM media m
                JOIN libraries l ON l.id = m.library_id
                LEFT JOIN playback_progress p ON p.media_id = m.id
                LEFT JOIN loudness_analyses a ON a.media_id = m.id
                WHERE {where}
                ORDER BY m.sort_title, m.id
                LIMIT ? OFFSET ?
                """,
                tuple(parameters + [limit, offset]),
            ).fetchall()
        return {
            "items": [self._public_media(row) for row in rows],
            "total": int(count),
            "limit": limit,
            "offset": offset,
        }

    def media_detail(self, media_id: int, ensure_probe: bool = True) -> dict[str, Any]:
        row = self._media_row(media_id)
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "Media item not found")
        navigation_missing = (
            row["media_type"] == "video"
            and (row["chapters_json"] is None or row["subtitle_streams_json"] is None)
        )
        if ensure_probe and (row["probe_status"] == "pending" or navigation_missing):
            try:
                probe_media(self.database, media_id)
            except ProbeError:
                pass
            row = self._media_row(media_id)
            assert row is not None
        item = self._public_media(row, detailed=True)
        item["compressor"] = self.compressor_settings(media_id)
        return item

    def compressor_settings(self, media_id: int | None = None) -> dict[str, Any]:
        with self.database.connect() as connection:
            global_row = connection.execute(
                "SELECT * FROM global_compressor_settings WHERE id = 1"
            ).fetchone()
            video_row = (
                connection.execute(
                    "SELECT * FROM media_compressor_settings WHERE media_id = ?",
                    (media_id,),
                ).fetchone()
                if media_id is not None
                else None
            )
        global_settings = _compressor_row(global_row)
        video_settings = _compressor_row(video_row) if video_row is not None else None
        return {
            "global": global_settings,
            "video": video_settings,
            "effective": video_settings or global_settings,
            "source": "video" if video_settings else "global",
        }

    def update_global_compressor(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.compressor_settings()["global"]
        settings = _compressor_payload(payload, current)
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE global_compressor_settings SET
                    enabled = ?, threshold_db = ?, ratio = ?, ceiling_db = ?,
                    attack_ms = ?, release_ms = ?, knee = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                _compressor_values(settings),
            )
        return self.compressor_settings()

    def update_media_compressor(
        self, media_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self._media_row(media_id) is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "Media item not found")
        if payload.get("inherit_global") is True:
            with self.database.connect() as connection:
                connection.execute(
                    "DELETE FROM media_compressor_settings WHERE media_id = ?",
                    (media_id,),
                )
            return self.compressor_settings(media_id)
        fallback = self.compressor_settings(media_id)["effective"]
        settings = _compressor_payload(payload, fallback)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO media_compressor_settings(
                    media_id, enabled, threshold_db, ratio, ceiling_db,
                    attack_ms, release_ms, knee
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    threshold_db = excluded.threshold_db,
                    ratio = excluded.ratio,
                    ceiling_db = excluded.ceiling_db,
                    attack_ms = excluded.attack_ms,
                    release_ms = excluded.release_ms,
                    knee = excluded.knee,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (media_id, *_compressor_values(settings)),
            )
        return self.compressor_settings(media_id)

    def _media_row(self, media_id: int):
        with self.database.connect() as connection:
            return connection.execute(
                """
                SELECT m.*, l.name AS library_name, l.root_path,
                       p.position_ms, p.duration_ms AS progress_duration_ms, p.completed,
                       a.status AS analysis_status, a.integrated_lufs, a.true_peak_db,
                       a.sample_interval_ms, a.envelope_json, a.spike_segments_json,
                       a.error AS analysis_error, a.analyzed_at
                FROM media m
                JOIN libraries l ON l.id = m.library_id
                LEFT JOIN playback_progress p ON p.media_id = m.id
                LEFT JOIN loudness_analyses a ON a.media_id = m.id
                WHERE m.id = ? AND m.available = 1
                """,
                (media_id,),
            ).fetchone()

    def _public_media(self, row, detailed: bool = False) -> dict[str, Any]:
        duration = row["duration_ms"] or row["progress_duration_ms"] or 0
        position = row["position_ms"] or 0
        item: dict[str, Any] = {
            "id": int(row["id"]),
            "available": True,
            "library_id": int(row["library_id"]),
            "library_name": row["library_name"],
            "title": row["title"],
            "file_name": row["file_name"],
            "relative_path": row["relative_path"],
            "media_type": row["media_type"],
            "extension": row["extension"],
            "size_bytes": int(row["size_bytes"]),
            "duration_ms": int(duration),
            "show_title": row["show_title"],
            "season_number": row["season_number"],
            "episode_number": row["episode_number"],
            "probe_status": row["probe_status"],
            "direct_play_likely": row["extension"] in DIRECT_PLAY_EXTENSIONS,
            "stream_url": f"/api/media/{row['id']}/stream",
            "thumbnail_url": f"/api/media/{row['id']}/thumbnail?v=2"
            if row["media_type"] == "video"
            else None,
            "progress": {
                "position_ms": int(position),
                "duration_ms": int(duration),
                "completed": bool(row["completed"] or 0),
            },
            "analysis": {
                "status": row["analysis_status"] or "pending",
                "spike_count": len(_json_or_default(row["spike_segments_json"], [])),
            },
        }
        if detailed:
            subtitle_streams = _json_or_default(row["subtitle_streams_json"], [])
            for track in subtitle_streams:
                if track.get("kind") == "text":
                    track["url"] = (
                        f"/api/media/{row['id']}/subtitles/{track['ordinal']}.vtt"
                    )
            item["technical"] = {
                "container": row["container"],
                "video_codec": row["video_codec"],
                "width": row["width"],
                "height": row["height"],
                "audio_codec": row["audio_codec"],
                "audio_channels": row["audio_channels"],
                "audio_tracks": row["audio_tracks"],
                "subtitle_tracks": row["subtitle_tracks"],
                "probe_error": row["probe_error"],
            }
            chapters = _json_or_default(row["chapters_json"], [])
            for index, chapter in enumerate(chapters):
                chapter["thumbnail_url"] = (
                    f"/api/media/{row['id']}/chapters/{index}/thumbnail?v=1"
                )
            item["chapters"] = chapters
            item["subtitles"] = subtitle_streams
            item["analysis"].update(
                {
                    "integrated_lufs": row["integrated_lufs"],
                    "true_peak_db": row["true_peak_db"],
                    "sample_interval_ms": row["sample_interval_ms"],
                    "segments": _json_or_default(row["spike_segments_json"], []),
                    "error": row["analysis_error"],
                    "analyzed_at": row["analyzed_at"],
                }
            )
        return item

    def update_progress(self, media_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if self._media_row(media_id) is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "Media item not found")
        position = _required_nonnegative_int(payload.get("position_ms"), "position_ms")
        duration = _required_nonnegative_int(payload.get("duration_ms", 0), "duration_ms")
        completed = bool(payload.get("completed", False))
        if duration and position > duration + 60_000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "position_ms is beyond the media duration")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO playback_progress(media_id, position_ms, duration_ms, completed)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    position_ms = excluded.position_ms,
                    duration_ms = excluded.duration_ms,
                    completed = excluded.completed,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (media_id, position, duration, int(completed)),
            )
        return {"position_ms": position, "duration_ms": duration, "completed": completed}

    def start_analysis(self, media_id: int) -> bool:
        if self._media_row(media_id) is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "Media item not found")
        with self.analysis_lock:
            if media_id in self.analysis_jobs:
                return False
            self.analysis_jobs.add(media_id)
        thread = threading.Thread(
            target=self._analysis_worker,
            args=(media_id,),
            name=f"fluxa-analysis-{media_id}",
            daemon=True,
        )
        thread.start()
        return True

    def _analysis_worker(self, media_id: int) -> None:
        try:
            analyze_media(self.database, media_id)
        except LoudnessError:
            pass
        finally:
            with self.analysis_lock:
                self.analysis_jobs.discard(media_id)

    def resolve_media_path(self, media_id: int) -> Path:
        row = self._media_row(media_id)
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "Media item not found")
        path = Path(row["path"])
        root = Path(row["root_path"])
        try:
            resolved = path.resolve(strict=True)
            root_resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, f"Media file is unavailable: {exc}") from exc
        if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
            raise ApiError(HTTPStatus.FORBIDDEN, "Media path is outside its configured library")
        return resolved

    def start_compatibility(
        self, media_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        row = self._media_row(media_id)
        if row is None or row["media_type"] != "video":
            raise ApiError(HTTPStatus.NOT_FOUND, "Video item not found")
        start_ms = _nonnegative_int(payload.get("start_ms", 0))
        if start_ms is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "start_ms must be non-negative")
        requested_subtitle = payload.get("subtitle_ordinal")
        subtitle_ordinal = (
            _nonnegative_int(requested_subtitle)
            if requested_subtitle is not None
            else None
        )
        if requested_subtitle is not None and subtitle_ordinal is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "subtitle_ordinal must be non-negative")
        compressor = self.compressor_settings(media_id)["effective"]
        subtitle_kind: str | None = None
        if subtitle_ordinal is not None:
            tracks = _json_or_default(row["subtitle_streams_json"], [])
            track = next(
                (entry for entry in tracks if entry.get("ordinal") == subtitle_ordinal),
                None,
            )
            if track is None:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Subtitle track not found")
            subtitle_kind = str(track.get("kind") or "unsupported")
            if subtitle_kind != "bitmap":
                subtitle_ordinal = None
        try:
            session = self.compatibility.start(
                media_id=media_id,
                source=self.resolve_media_path(media_id),
                start_ms=start_ms,
                duration_ms=int(row["duration_ms"] or 0),
                width=row["width"],
                height=row["height"],
                analysis_status=row["analysis_status"],
                envelope_json=row["envelope_json"],
                segment_format=(
                    "fmp4" if payload.get("segment_format") == "fmp4" else "mpegts"
                ),
                subtitle_ordinal=subtitle_ordinal,
                subtitle_kind=subtitle_kind,
                compressor_enabled=compressor["enabled"],
                compressor_threshold_db=compressor["threshold_db"],
                compressor_ratio=compressor["ratio"],
                compressor_ceiling_db=compressor["ceiling_db"],
                compressor_attack_ms=compressor["attack_ms"],
                compressor_release_ms=compressor["release_ms"],
                compressor_knee=compressor["knee"],
            )
        except TranscodeError as exc:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
        return {
            "session_id": session.id,
            "manifest_url": f"/api/compat/{session.id}/index.m3u8",
            "start_ms": session.start_ms,
            "video_mode": session.video_mode,
            "audio_mode": "AAC stereo",
            "segment_format": session.segment_format,
            "leveling_mode": session.leveling_mode,
            "leveling_applied": session.leveling_applied,
            "compressor": {
                **compressor,
            },
            "subtitle_ordinal": session.subtitle_ordinal,
        }

    def control_compatibility(self, session_id: str, action: object) -> dict[str, Any]:
        if not isinstance(action, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "A stream control action is required")
        try:
            session = self.compatibility.control(session_id, action)
        except TranscodeNotFound as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, str(exc)) from exc
        except TranscodeError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        return {
            "status": "closed" if session is None else "active",
            "paused": bool(session and session.paused),
        }

    def record_playback_event(
        self, payload: dict[str, Any], user_agent: str = ""
    ) -> dict[str, Any]:
        event = str(payload.get("event") or "unknown")[:64]
        record: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            "user_agent": user_agent[:300],
        }
        numeric_fields = (
            "media_id",
            "position_ms",
            "buffered_ahead_ms",
            "stall_ms",
            "ready_state",
            "network_state",
        )
        for key in numeric_fields:
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                record[key] = round(value, 3)
        for key in (
            "session_id",
            "hls_type",
            "hls_detail",
            "hls_reason",
            "hls_buffer",
            "fullscreen_reason",
            "fullscreen_target",
        ):
            value = payload.get(key)
            if value is not None:
                record[key] = str(value)[:300]
        for key in ("paused", "hidden"):
            if isinstance(payload.get(key), bool):
                record[key] = payload[key]
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n"
        with self.playback_log_lock:
            try:
                if (
                    self.playback_log_path.is_file()
                    and self.playback_log_path.stat().st_size >= 5 * 1024 * 1024
                ):
                    archive = self.playback_log_path.with_suffix(".jsonl.1")
                    os.replace(self.playback_log_path, archive)
                with self.playback_log_path.open("a", encoding="utf-8") as output:
                    output.write(line)
            except OSError as exc:
                raise ApiError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"Could not write playback diagnostics: {exc}",
                ) from exc
        return {"recorded": True, "event": event}

    def thumbnail(self, media_id: int) -> Path | None:
        row = self._media_row(media_id)
        if row is None or row["media_type"] != "video":
            raise ApiError(HTTPStatus.NOT_FOUND, "Video item not found")
        target = self._thumbnail_path(row)
        if target.is_file():
            return target
        with self.thumbnail_lock:
            if media_id in self.thumbnail_failed:
                raise ApiError(HTTPStatus.NOT_FOUND, "Preview artwork is unavailable")
        self._queue_thumbnail(media_id)
        return None

    def _thumbnail_path(self, row) -> Path:
        return self.thumbnail_dir / f"{int(row['id'])}-{int(row['modified_ns'])}-v2.jpg"

    def _queue_thumbnail(self, media_id: int) -> None:
        with self.thumbnail_lock:
            if media_id in self.thumbnail_pending:
                return
            self.thumbnail_pending.add(media_id)
            self.thumbnail_queue.put(media_id)
            if self.thumbnail_thread is None:
                self.thumbnail_thread = threading.Thread(
                    target=self._thumbnail_worker,
                    name="fluxa-thumbnail-worker",
                    daemon=True,
                )
                self.thumbnail_thread.start()

    def _thumbnail_worker(self) -> None:
        while True:
            media_id = self.thumbnail_queue.get()
            generated = False
            try:
                while self.compatibility.active_count():
                    time.sleep(1)
                generated = self._generate_thumbnail(media_id)
            finally:
                with self.thumbnail_lock:
                    self.thumbnail_pending.discard(media_id)
                    if not generated:
                        self.thumbnail_failed.add(media_id)
                self.thumbnail_queue.task_done()

    def _generate_thumbnail(self, media_id: int) -> bool:
        row = self._media_row(media_id)
        ffmpeg = shutil.which("ffmpeg")
        if row is None or row["media_type"] != "video" or not ffmpeg:
            return False
        target = self._thumbnail_path(row)
        if target.is_file():
            return True
        try:
            source = self.resolve_media_path(media_id)
        except ApiError:
            return False
        duration_seconds = max(0.0, float(row["duration_ms"] or 0) / 1000)
        seek_seconds = (
            min(300.0, max(15.0, duration_seconds * 0.12))
            if duration_seconds
            else 30.0
        )
        temporary = target.with_suffix(f".{threading.get_ident()}.tmp")
        try:
            command = [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-threads",
                    "1",
                    "-ss",
                    f"{seek_seconds:.3f}",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1,"
                    "scale=480:-2:force_original_aspect_ratio=decrease",
                    "-q:v",
                    "4",
                    "-c:v",
                    "mjpeg",
                    "-an",
                    "-sn",
                    "-dn",
                    "-threads",
                    "1",
                    "-f",
                    "image2",
                    str(temporary),
                ]
            nice = shutil.which("nice")
            if nice:
                command = [nice, "-n", "10", *command]
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=90,
                check=False,
            )
            if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size:
                os.replace(temporary, target)
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return False

    def start_artwork_backfill(self) -> bool:
        """Keep preview artwork warm without tying generation to a page view."""
        with self.artwork_backfill_lock:
            if self.artwork_backfill_thread and self.artwork_backfill_thread.is_alive():
                self.artwork_backfill_wake.set()
                return False
            self.artwork_backfill_thread = threading.Thread(
                target=self._artwork_backfill_worker,
                name="fluxa-artwork-backfill",
                daemon=True,
            )
            self.artwork_backfill_thread.start()
            return True

    def _artwork_backfill_worker(self) -> None:
        while True:
            self.artwork_backfill_wake.clear()
            with self.database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT id, modified_ns, chapters_json
                    FROM media
                    WHERE available = 1 AND media_type = 'video'
                    ORDER BY id
                    """
                ).fetchall()
            for row in rows:
                media_id = int(row["id"])
                if not self._thumbnail_path(row).is_file():
                    self._queue_thumbnail(media_id)
                chapters = _json_or_default(row["chapters_json"], [])
                for chapter_index in range(len(chapters)):
                    if not self._chapter_thumbnail_path(row, chapter_index).is_file():
                        self._queue_chapter_thumbnail(media_id, chapter_index)
            self.artwork_backfill_completed += len(rows)
            self.artwork_backfill_wake.wait(300)

    def _queue_known_chapter_thumbnails(self, media_id: int) -> None:
        row = self._media_row(media_id)
        if row is None or row["media_type"] != "video":
            return
        chapters = _json_or_default(row["chapters_json"], [])
        for chapter_index in range(len(chapters)):
            if not self._chapter_thumbnail_path(row, chapter_index).is_file():
                self._queue_chapter_thumbnail(media_id, chapter_index)

    def caption(self, media_id: int, ordinal: int) -> Path:
        row = self._media_row(media_id)
        if row is None or row["media_type"] != "video":
            raise ApiError(HTTPStatus.NOT_FOUND, "Video item not found")
        tracks = _json_or_default(row["subtitle_streams_json"], [])
        track = next(
            (entry for entry in tracks if entry.get("ordinal") == ordinal), None
        )
        if track is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "Caption track not found")
        if track.get("kind") != "text":
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "This image-based caption track is provided through the compatibility stream",
            )
        target = self.caption_dir / (
            f"{media_id}-{int(row['modified_ns'])}-{ordinal}-v1.vtt"
        )
        if target.is_file():
            return target
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "FFmpeg is not installed")
        temporary = target.with_suffix(f".{threading.get_ident()}.tmp")
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(self.resolve_media_path(media_id)),
                    "-map",
                    f"0:s:{ordinal}",
                    "-c:s",
                    "webvtt",
                    "-f",
                    "webvtt",
                    str(temporary),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
            if result.returncode != 0 or not temporary.is_file():
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                raise ApiError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    detail[-500:] or "Caption conversion failed",
                )
            os.replace(temporary, target)
            return target
        except subprocess.TimeoutExpired as exc:
            raise ApiError(
                HTTPStatus.GATEWAY_TIMEOUT, "Caption conversion timed out"
            ) from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def chapter_thumbnail(self, media_id: int, chapter_index: int) -> Path | None:
        row = self._media_row(media_id)
        if row is None or row["media_type"] != "video":
            raise ApiError(HTTPStatus.NOT_FOUND, "Video item not found")
        chapters = _json_or_default(row["chapters_json"], [])
        if chapter_index < 0 or chapter_index >= len(chapters):
            raise ApiError(HTTPStatus.NOT_FOUND, "Chapter not found")
        target = self._chapter_thumbnail_path(row, chapter_index)
        if target.is_file():
            return target
        key = (media_id, chapter_index)
        with self.chapter_thumbnail_lock:
            if key in self.chapter_thumbnail_failed:
                raise ApiError(HTTPStatus.NOT_FOUND, "Chapter preview is unavailable")
        self._queue_chapter_thumbnail(media_id, chapter_index)
        return None

    def _chapter_thumbnail_path(self, row, chapter_index: int) -> Path:
        return self.chapter_thumbnail_dir / (
            f"{int(row['id'])}-{int(row['modified_ns'])}-{chapter_index}-v1.jpg"
        )

    def _queue_chapter_thumbnail(self, media_id: int, chapter_index: int) -> None:
        key = (media_id, chapter_index)
        with self.chapter_thumbnail_lock:
            if key in self.chapter_thumbnail_pending:
                return
            self.chapter_thumbnail_pending.add(key)
            self.chapter_thumbnail_queue.put(key)
            if self.chapter_thumbnail_thread is None:
                self.chapter_thumbnail_thread = threading.Thread(
                    target=self._chapter_thumbnail_worker,
                    name="fluxa-chapter-artwork-worker",
                    daemon=True,
                )
                self.chapter_thumbnail_thread.start()

    def _chapter_thumbnail_worker(self) -> None:
        while True:
            media_id, chapter_index = self.chapter_thumbnail_queue.get()
            generated = False
            try:
                while self.compatibility.active_count():
                    time.sleep(1)
                generated = self._generate_chapter_thumbnail(media_id, chapter_index)
            finally:
                key = (media_id, chapter_index)
                with self.chapter_thumbnail_lock:
                    self.chapter_thumbnail_pending.discard(key)
                    if not generated:
                        self.chapter_thumbnail_failed.add(key)
                self.chapter_thumbnail_queue.task_done()

    def _generate_chapter_thumbnail(self, media_id: int, chapter_index: int) -> bool:
        row = self._media_row(media_id)
        ffmpeg = shutil.which("ffmpeg")
        if row is None or row["media_type"] != "video" or not ffmpeg:
            return False
        chapters = _json_or_default(row["chapters_json"], [])
        if chapter_index < 0 or chapter_index >= len(chapters):
            return False
        target = self._chapter_thumbnail_path(row, chapter_index)
        if target.is_file():
            return True
        chapter = chapters[chapter_index]
        start_ms = int(chapter.get("start_ms") or 0)
        end_ms = int(chapter.get("end_ms") or start_ms)
        offset_ms = min(3_000, max(500, (end_ms - start_ms) // 8))
        seek_seconds = max(0, start_ms + offset_ms) / 1000
        temporary = target.with_suffix(f".{threading.get_ident()}.tmp")
        try:
            command = [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-threads",
                    "1",
                    "-ss",
                    f"{seek_seconds:.3f}",
                    "-i",
                    str(self.resolve_media_path(media_id)),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1,"
                    "scale=320:-2:force_original_aspect_ratio=decrease",
                    "-q:v",
                    "5",
                    "-c:v",
                    "mjpeg",
                    "-an",
                    "-sn",
                    "-dn",
                    "-threads",
                    "1",
                    "-f",
                    "image2",
                    str(temporary),
                ]
            nice = shutil.which("nice")
            if nice:
                command = [nice, "-n", "10", *command]
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size:
                os.replace(temporary, target)
                return True
        except (ApiError, OSError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return False

    def login_allowed(self, client: str) -> tuple[bool, int]:
        now = time.monotonic()
        window = 15 * 60
        with self.login_lock:
            attempts = [stamp for stamp in self.failed_logins.get(client, []) if now - stamp < window]
            self.failed_logins[client] = attempts
            if len(attempts) < 10:
                return True, 0
            retry = max(1, round(window - (now - attempts[0])))
            return False, retry

    def record_failed_login(self, client: str) -> None:
        with self.login_lock:
            self.failed_logins.setdefault(client, []).append(time.monotonic())

    def clear_failed_logins(self, client: str) -> None:
        with self.login_lock:
            self.failed_logins.pop(client, None)


class FluxaHandler(BaseHTTPRequestHandler):
    server_version = f"Fluxa/{__version__}"
    app: FluxaApp

    def do_OPTIONS(self) -> None:
        if not self._origin_allowed():
            self._send_json({"error": "Cross-origin request denied"}, HTTPStatus.FORBIDDEN)
            return
        if not self._require_access():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:
        self._dispatch(head_only=True)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            try:
                self._handle_login()
            except ApiError as exc:
                self._send_json({"error": str(exc)}, exc.status)
            return
        if not self._origin_allowed():
            self._send_json({"error": "Cross-origin request denied"}, HTTPStatus.FORBIDDEN)
            return
        if not self._require_access():
            return
        try:
            if parsed.path == "/api/auth/logout":
                self._handle_logout()
                return
            if parsed.path == "/api/playback-events":
                self._send_json(
                    self.app.record_playback_event(
                        self._read_json(), self.headers.get("User-Agent", "")
                    ),
                    HTTPStatus.ACCEPTED,
                )
                return
            if parsed.path == "/api/scan":
                results = [asdict(result) for result in self.app.scan()]
                if self.app.config.probe_on_start:
                    self.app.start_probing()
                self._send_json({"results": results})
                return
            control_match = COMPAT_CONTROL_ROUTE.match(parsed.path)
            if control_match:
                payload = self._read_json()
                self._send_json(
                    self.app.control_compatibility(
                        control_match.group("session"), payload.get("action")
                    )
                )
                return
            match = MEDIA_ROUTE.match(parsed.path)
            if match and match.group("action") == "compatibility":
                media_id = int(match.group("id"))
                self._send_json(
                    self.app.start_compatibility(media_id, self._read_json()),
                    HTTPStatus.CREATED,
                )
                return
            if match and match.group("action") == "probe":
                media_id = int(match.group("id"))
                probe_media(self.app.database, media_id)
                self._send_json(self.app.media_detail(media_id, ensure_probe=False))
                return
            if match and match.group("action") == "analyze":
                media_id = int(match.group("id"))
                started = self.app.start_analysis(media_id)
                self._send_json(
                    {"media_id": media_id, "status": "running", "started": started},
                    HTTPStatus.ACCEPTED,
                )
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "Endpoint not found")
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except ProbeError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except Exception as exc:
            self._send_json({"error": f"Request failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        if not self._origin_allowed():
            self._send_json({"error": "Cross-origin request denied"}, HTTPStatus.FORBIDDEN)
            return
        if not self._require_access():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/compressor-settings":
                self._send_json(
                    self.app.update_global_compressor(self._read_json())
                )
                return
            match = MEDIA_ROUTE.match(parsed.path)
            if match and match.group("action") == "compressor-settings":
                self._send_json(
                    self.app.update_media_compressor(
                        int(match.group("id")), self._read_json()
                    )
                )
                return
            if not match or match.group("action") != "progress":
                raise ApiError(HTTPStatus.NOT_FOUND, "Endpoint not found")
            payload = self._read_json()
            progress = self.app.update_progress(int(match.group("id")), payload)
            self._send_json(progress)
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except Exception as exc:
            self._send_json({"error": f"Request failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _dispatch(self, head_only: bool) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.svg":
            self._send_static(parsed.path, head_only)
            return
        if parsed.path == "/login":
            self._send_login_page(parsed, head_only=head_only)
            return
        if not self._require_access(head_only=head_only):
            return
        try:
            if parsed.path == "/api/auth/status":
                self._send_json(
                    {
                        "public_proxy": self._is_public_proxy(),
                        "authentication_required": not self._client_is_local(),
                        "authenticated": self._session_is_valid() or self._client_is_local(),
                    },
                    head_only=head_only,
                )
                return
            if parsed.path == "/api/status":
                self._send_json(self.app.status(), head_only=head_only)
                return
            if parsed.path == "/api/compressor-settings":
                self._send_json(
                    self.app.compressor_settings(), head_only=head_only
                )
                return
            if parsed.path == "/api/libraries":
                self._send_json({"libraries": self.app.libraries()}, head_only=head_only)
                return
            if parsed.path == "/api/playlists":
                self._send_json({"playlists": self.app.playlists()}, head_only=head_only)
                return
            playlist_match = PLAYLIST_ROUTE.match(parsed.path)
            if playlist_match:
                self._send_json(
                    self.app.playlist_detail(
                        int(playlist_match.group("id")), parse_qs(parsed.query)
                    ),
                    head_only=head_only,
                )
                return
            if parsed.path == "/api/media":
                self._send_json(
                    self.app.media_list(parse_qs(parsed.query)), head_only=head_only
                )
                return
            subtitle_match = SUBTITLE_ROUTE.match(parsed.path)
            if subtitle_match:
                self._send_caption(
                    int(subtitle_match.group("id")),
                    int(subtitle_match.group("ordinal")),
                    head_only,
                )
                return
            chapter_thumbnail_match = CHAPTER_THUMBNAIL_ROUTE.match(parsed.path)
            if chapter_thumbnail_match:
                self._send_chapter_thumbnail(
                    int(chapter_thumbnail_match.group("id")),
                    int(chapter_thumbnail_match.group("index")),
                    head_only,
                )
                return
            compatibility_match = COMPAT_FILE_ROUTE.match(parsed.path)
            if compatibility_match:
                self._send_compatibility_file(
                    compatibility_match.group("session"),
                    compatibility_match.group("file"),
                    head_only,
                )
                return
            match = MEDIA_ROUTE.match(parsed.path)
            if match:
                media_id = int(match.group("id"))
                action = match.group("action")
                if action == "stream":
                    self._send_media(media_id, head_only)
                elif action == "thumbnail":
                    self._send_thumbnail(media_id, head_only)
                elif action == "loudness":
                    detail = self.app.media_detail(media_id, ensure_probe=False)
                    self._send_json(detail["analysis"], head_only=head_only)
                elif action == "compressor-settings":
                    self._send_json(
                        self.app.compressor_settings(media_id), head_only=head_only
                    )
                elif action is None:
                    self._send_json(self.app.media_detail(media_id), head_only=head_only)
                else:
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "Use POST for this endpoint")
                return
            if parsed.path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "Endpoint not found")
            self._send_static(parsed.path, head_only)
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status, head_only=head_only)
        except Exception as exc:
            self._send_json(
                {"error": f"Request failed: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
                head_only=head_only,
            )

    def _send_media(self, media_id: int, head_only: bool) -> None:
        path = self.app.resolve_media_path(media_id)
        try:
            stat = path.stat()
        except OSError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "Compatibility stream segment expired") from exc
        size = stat.st_size
        range_header = self.headers.get("Range")
        try:
            byte_range = parse_byte_range(range_header, size) if range_header else None
        except RangeNotSatisfiable:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self._common_headers()
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return

        if byte_range:
            start, end = byte_range
            status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
        else:
            start, end = 0, size - 1
            status = HTTPStatus.OK
            length = size
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", content_type(path))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", f'"{stat.st_mtime_ns:x}-{size:x}"')
        self.send_header("Cache-Control", "private, max-age=0, must-revalidate")
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        try:
            with path.open("rb") as media_file:
                media_file.seek(start)
                remaining = length
                while remaining:
                    chunk = media_file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_thumbnail(self, media_id: int, head_only: bool) -> None:
        path = self.app.thumbnail(media_id)
        if path is None:
            self.send_response(HTTPStatus.ACCEPTED)
            self._common_headers()
            self.send_header("Retry-After", "2")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_compatibility_file(
        self, session_id: str, name: str, head_only: bool
    ) -> None:
        try:
            path = self.app.compatibility.get_file(session_id, name)
        except TranscodeNotFound as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, str(exc)) from exc
        except TranscodeNotReady as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, str(exc)) from exc
        stat = path.stat()
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header(
            "Content-Type",
            "application/vnd.apple.mpegurl"
            if name.endswith(".m3u8")
            else (
                "video/mp2t"
                if name.endswith(".ts")
                else "video/mp4"
            ),
        )
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header(
            "Cache-Control",
            "no-store" if name.endswith(".m3u8") else "private, max-age=3600",
        )
        self.end_headers()
        if head_only:
            return
        try:
            with path.open("rb") as stream_file:
                while chunk := stream_file.read(1024 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_caption(self, media_id: int, ordinal: int, head_only: bool) -> None:
        path = self.app.caption(media_id, ordinal)
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_chapter_thumbnail(
        self, media_id: int, chapter_index: int, head_only: bool
    ) -> None:
        path = self.app.chapter_thumbnail(media_id, chapter_index)
        if path is None:
            self.send_response(HTTPStatus.ACCEPTED)
            self._common_headers()
            self.send_header("Retry-After", "2")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_static(self, route: str, head_only: bool) -> None:
        file_name = STATIC_FILES.get(unquote(route))
        if file_name is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "Page not found")
        path = self.app.public_dir / file_name
        payload = path.read_bytes()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(path.suffix, "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; media-src 'self' blob:; connect-src 'self'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:",
        )
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _handle_login(self) -> None:
        if not self.app.auth.configured:
            self._send_login_page(
                urlparse("/login"),
                error="Public login is not configured on the server.",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if not self._origin_allowed(require_on_public_proxy=True):
            self._send_json({"error": "Cross-origin login denied"}, HTTPStatus.FORBIDDEN)
            return
        client = str(self._client_ip())
        allowed, retry_after = self.app.login_allowed(client)
        if not allowed:
            self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self._common_headers()
            self.send_header("Retry-After", str(retry_after))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        form = self._read_form()
        password = form.get("password", [""])[0]
        try:
            valid = self.app.auth.verify_password(password)
        except AuthenticationError:
            valid = False
        if not valid:
            self.app.record_failed_login(client)
            self._send_login_page(
                urlparse("/login"),
                error="That household password was not accepted.",
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        self.app.clear_failed_logins(client)
        token = self.app.auth.issue_session()
        self.send_response(HTTPStatus.SEE_OTHER)
        self._common_headers()
        self.send_header("Location", self._public_prefix() + "/")
        cookie_path = self._public_prefix() or "/"
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={token}; Path={cookie_path}; Max-Age={SESSION_SECONDS}; "
            "Secure; HttpOnly; SameSite=Strict",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_logout(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self._common_headers()
        self.send_header("Location", self._public_prefix() + "/login")
        cookie_path = self._public_prefix() or "/"
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}=; Path={cookie_path}; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_login_page(
        self,
        parsed,
        head_only: bool = False,
        error: str | None = None,
        status: int = HTTPStatus.OK,
    ) -> None:
        if self._session_is_valid() and not error:
            self._send_redirect(self._public_prefix() + "/")
            return
        template = (self.app.public_dir / "login.html").read_text(encoding="utf-8")
        query = parse_qs(parsed.query)
        next_path = query.get("next", [self._public_prefix() + "/"])[0]
        if not next_path.startswith(self._public_prefix() + "/"):
            next_path = self._public_prefix() + "/"
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        page = (
            template.replace("{{PREFIX}}", html.escape(self._public_prefix(), quote=True))
            .replace("{{NEXT}}", html.escape(next_path, quote=True))
            .replace("{{ERROR}}", error_html)
        ).encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if not head_only:
            self.wfile.write(page)

    def _send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self._common_headers()
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _require_access(self, head_only: bool = False) -> bool:
        if self._client_is_local() or self._session_is_valid():
            return True
        if not self.app.auth.configured:
            self._send_json(
                {"error": "Public authentication is not configured"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                head_only=head_only,
            )
            return False
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._send_json(
                {"error": "Authentication required"},
                HTTPStatus.UNAUTHORIZED,
                head_only=head_only,
            )
        else:
            destination = self._public_prefix() + parsed.path
            if parsed.query:
                destination += "?" + parsed.query
            self._send_redirect(
                self._public_prefix() + "/login?next=" + quote(destination, safe="/")
            )
        return False

    def _session_is_valid(self) -> bool:
        if not self.app.auth.configured:
            return False
        raw_cookie = self.headers.get("Cookie", "")
        try:
            cookies = SimpleCookie(raw_cookie)
            morsel = cookies.get(COOKIE_NAME)
            return bool(morsel and self.app.auth.verify_session(morsel.value))
        except (AuthenticationError, ValueError):
            return False

    def _read_form(self) -> dict[str, list[str]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "A form-encoded request is required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length") from exc
        if length <= 0 or length > 16_384:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid login request size")
        return parse_qs(self.rfile.read(length).decode("utf-8", errors="strict"))

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length") from exc
        if length <= 0 or length > 1_048_576:
            raise ApiError(HTTPStatus.BAD_REQUEST, "A small JSON request body is required")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid JSON request body") from exc
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return value

    def _send_json(
        self, payload: object, status: int = HTTPStatus.OK, head_only: bool = False
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        try:
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        # Same-origin referrers provide a safe fallback for browsers that omit
        # Origin on ordinary HTML form submissions. Nothing leaks off-site.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def _client_is_local(self) -> bool:
        if self._is_public_proxy():
            return False
        address = self._client_ip()
        return address.is_loopback or address.is_private or address.is_link_local

    def _client_ip(self) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        value = self.client_address[0]
        if self._is_public_proxy():
            value = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return ipaddress.ip_address("203.0.113.1")
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address

    def _is_public_proxy(self) -> bool:
        try:
            peer = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return peer.is_loopback and self.headers.get("X-Fluxa-Public-Proxy") == "1"

    def _public_prefix(self) -> str:
        if not self._is_public_proxy():
            return ""
        prefix = self.headers.get("X-Forwarded-Prefix", "")
        if re.fullmatch(r"/[A-Za-z0-9_-]+", prefix):
            return prefix
        return ""

    def _origin_allowed(self, require_on_public_proxy: bool = False) -> bool:
        expected_scheme = (
            self.headers.get("X-Forwarded-Proto", "https")
            if self._is_public_proxy()
            else "http"
        )
        expected = f"{expected_scheme}://{self.headers.get('Host', '')}"
        return request_origin_allowed(
            origin=self.headers.get("Origin"),
            referer=self.headers.get("Referer"),
            fetch_site=self.headers.get("Sec-Fetch-Site"),
            expected=expected,
            require_evidence=require_on_public_proxy and self._is_public_proxy(),
        )

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} - {format_string % args}")


def request_origin_allowed(
    *,
    origin: str | None,
    referer: str | None,
    fetch_site: str | None,
    expected: str,
    require_evidence: bool,
) -> bool:
    """Validate same-origin browser evidence without requiring Origin itself."""
    if origin and origin != "null":
        return _same_origin(origin, expected)
    if referer:
        return _same_origin(referer, expected)
    if (fetch_site or "").lower() == "same-origin":
        return True
    return not require_evidence


def _same_origin(candidate: str, expected: str) -> bool:
    candidate_origin = _normalized_origin(candidate)
    expected_origin = _normalized_origin(expected)
    return bool(
        candidate_origin
        and expected_origin
        and hmac.compare_digest(candidate_origin, expected_origin)
    )


def _normalized_origin(value: str) -> str | None:
    try:
        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if scheme not in {"http", "https"} or not hostname:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return f"{scheme}://{hostname}:{port}"


def parse_byte_range(value: str, size: int) -> tuple[int, int]:
    if size <= 0 or not value.startswith("bytes=") or "," in value:
        raise RangeNotSatisfiable("Unsupported byte range")
    specification = value[6:].strip()
    if "-" not in specification:
        raise RangeNotSatisfiable("Invalid byte range")
    start_text, end_text = specification.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise RangeNotSatisfiable("Invalid suffix range")
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start < 0 or start >= size or end < start:
                raise RangeNotSatisfiable("Range is outside the file")
            end = min(end, size - 1)
    except ValueError as exc:
        raise RangeNotSatisfiable("Invalid byte range") from exc
    return start, end


def _json_or_default(value: object, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _bounded_float(
    value: object, minimum: float, maximum: float, default: float
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(minimum, min(maximum, number))


def _compressor_row(row: object) -> dict[str, Any]:
    return {
        "enabled": bool(row["enabled"]),
        "threshold_db": float(row["threshold_db"]),
        "ratio": float(row["ratio"]),
        "ceiling_db": float(row["ceiling_db"]),
        "attack_ms": float(row["attack_ms"]),
        "release_ms": float(row["release_ms"]),
        "knee": float(row["knee"]),
    }


def _compressor_payload(
    payload: dict[str, Any], fallback: dict[str, Any]
) -> dict[str, Any]:
    return {
        "enabled": (
            payload["enabled"]
            if isinstance(payload.get("enabled"), bool)
            else bool(fallback["enabled"])
        ),
        "threshold_db": _bounded_float(
            payload.get("threshold_db"), -36.0, -8.0, fallback["threshold_db"]
        ),
        "ratio": _bounded_float(payload.get("ratio"), 2.0, 16.0, fallback["ratio"]),
        "ceiling_db": _bounded_float(
            payload.get("ceiling_db"), -12.0, -1.0, fallback["ceiling_db"]
        ),
        "attack_ms": _bounded_float(
            payload.get("attack_ms"), 1.0, 200.0, fallback["attack_ms"]
        ),
        "release_ms": _bounded_float(
            payload.get("release_ms"), 50.0, 3000.0, fallback["release_ms"]
        ),
        "knee": _bounded_float(payload.get("knee"), 1.0, 8.0, fallback["knee"]),
    }


def _compressor_values(settings: dict[str, Any]) -> tuple[object, ...]:
    return (
        int(bool(settings["enabled"])),
        settings["threshold_db"],
        settings["ratio"],
        settings["ceiling_db"],
        settings["attack_ms"],
        settings["release_ms"],
        settings["knee"],
    )


def _nonnegative_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _required_nonnegative_int(value: object, name: str) -> int:
    parsed = _nonnegative_int(value)
    if parsed is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be a non-negative integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fluxa local media server")
    parser.add_argument("--config", default=os.environ.get("FLUXA_CONFIG", "fluxa.json"))
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="start the browser and media server")
    subparsers.add_parser("scan", help="scan configured folders without copying media")
    subparsers.add_parser("status", help="print server catalog status")
    probe_parser = subparsers.add_parser("probe", help="read stream metadata for one item")
    probe_parser.add_argument("media_id", type=int)
    analyze_parser = subparsers.add_parser("analyze", help="analyze one item's loudness")
    analyze_parser.add_argument("media_id", type=int)
    subparsers.add_parser(
        "auth-init", help="create public authentication and a temporary password"
    )
    subparsers.add_parser(
        "auth-set-password", help="replace the household password and end old sessions"
    )
    subparsers.add_parser("auth-status", help="show public authentication status")
    plex_parser = subparsers.add_parser(
        "import-plex-playlists",
        help="copy Plex playlist metadata and ordering without copying media",
    )
    plex_parser.add_argument(
        "--plex-db", type=Path, default=DEFAULT_PLEX_DATABASE,
        help="path to Plex's com.plexapp.plugins.library.db",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2
    app = FluxaApp(config)
    command = args.command or "serve"
    if command == "scan":
        for result in app.scan():
            state = f"error={result.error}" if result.error else "ok"
            print(
                f"{result.library}: {result.discovered} found, {result.added} added, "
                f"{result.changed} changed, {result.unavailable} unavailable, "
                f"{result.skipped} skipped ({result.elapsed_seconds:.2f}s, {state})"
            )
        return 0
    if command == "status":
        print(json.dumps(app.status(), indent=2))
        return 0
    if command == "probe":
        try:
            result = probe_media(app.database, args.media_id)
        except ProbeError as exc:
            print(exc)
            return 1
        print(json.dumps(asdict(result), indent=2))
        return 0
    if command == "analyze":
        try:
            result = analyze_media(app.database, args.media_id)
        except LoudnessError as exc:
            print(exc)
            return 1
        print(
            f"Analyzed {len(result.samples)} seconds; detected "
            f"{len(result.segments)} loudness spike segment(s)."
        )
        return 0
    if command == "auth-init":
        try:
            password_path = app.auth.initialize()
        except AuthenticationError as exc:
            print(exc)
            return 1
        print(f"Public authentication initialized. Temporary password: {password_path}")
        return 0
    if command == "auth-set-password":
        password = getpass.getpass("New household password: ")
        confirmation = getpass.getpass("Confirm household password: ")
        if password != confirmation:
            print("Passwords do not match")
            return 1
        try:
            app.auth.set_password(password)
        except AuthenticationError as exc:
            print(exc)
            return 1
        print("Household password changed; existing public sessions are no longer valid.")
        return 0
    if command == "auth-status":
        print(
            json.dumps(
                {
                    "configured": app.auth.configured,
                    "auth_file": str(app.auth.path),
                    "temporary_password_file": str(app.auth.initial_password_path)
                    if app.auth.initial_password_path.is_file()
                    else None,
                },
                indent=2,
            )
        )
        return 0
    if command == "import-plex-playlists":
        try:
            result = import_plex_playlists(app.database, args.plex_db)
        except PlexImportError as exc:
            print(exc)
            return 1
        print(
            f"Imported {result.playlists} Plex playlists with {result.items} items: "
            f"{result.matched} matched, {result.unavailable} unavailable."
        )
        return 0

    if config.scan_on_start:
        def scan_then_probe() -> None:
            app.scan()
            if config.probe_on_start:
                app.start_probing()

        threading.Thread(
            target=scan_then_probe, name="fluxa-startup-scan", daemon=True
        ).start()
    elif config.probe_on_start:
        app.start_probing()
    app.start_artwork_backfill()
    FluxaHandler.app = app
    server = ThreadingHTTPServer((config.host, config.port), FluxaHandler)
    print(f"Fluxa {__version__} listening on http://{config.host}:{config.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
