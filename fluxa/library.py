from __future__ import annotations

import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import FluxaConfig, LibraryConfig
from .database import Database


VIDEO_EXTENSIONS = {
    ".avi",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
}
AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".ape",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
IGNORED_DIRECTORIES = {
    "$recycle.bin",
    ".appledouble",
    ".snapshot",
    ".snapshots",
    "@eadir",
    "system volume information",
}
EPISODE_PATTERN = re.compile(
    r"(?P<show>.*?)[ ._-]+s(?P<season>\d{1,2})e(?P<episode>\d{1,3})",
    re.IGNORECASE,
)
DISC_EPISODE_PATTERN = re.compile(
    r"^\d{1,4}[ ._-]+s(?P<season>\d{1,2})d\d{1,2}[ ._-]+",
    re.IGNORECASE,
)
GENERIC_SHOW_FOLDERS = {
    "files",
    "media",
    "episodes",
    "video",
    "videos",
}


@dataclass(frozen=True)
class ScanResult:
    library: str
    discovered: int
    added: int
    changed: int
    unavailable: int
    skipped: int
    elapsed_seconds: float
    error: str | None = None


def clean_title(stem: str) -> str:
    title = re.sub(r"[._]+", " ", stem)
    title = re.sub(r"\s+", " ", title).strip(" -")
    return title or stem


def sort_key(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    normalized = re.sub(r"^(the|an|a)\s+", "", normalized, flags=re.IGNORECASE)
    return normalized.casefold()


def episode_identity(stem: str) -> tuple[str | None, int | None, int | None]:
    match = EPISODE_PATTERN.search(stem)
    if not match:
        disc_match = DISC_EPISODE_PATTERN.search(stem)
        if disc_match:
            return None, int(disc_match.group("season")), None
        return None, None, None
    show = clean_title(match.group("show"))
    return show or None, int(match.group("season")), int(match.group("episode"))


def episode_title(stem: str) -> str | None:
    match = EPISODE_PATTERN.search(stem)
    if not match:
        match = DISC_EPISODE_PATTERN.search(stem)
        if not match:
            return None
    remainder = stem[match.end() :]
    remainder = re.sub(r"^[ ._-]*(?:d\d{1,3})?[ ._-]*", "", remainder, flags=re.IGNORECASE)
    cleaned = clean_title(remainder)
    return cleaned if remainder.strip(" ._-") else None


def show_title_from_path(relative: Path, filename_show: str | None) -> str | None:
    if filename_show and not filename_show.isdecimal():
        return filename_show
    for parent in reversed(relative.parts[:-1]):
        cleaned = clean_title(parent)
        folded = cleaned.casefold()
        if (
            folded not in GENERIC_SHOW_FOLDERS
            and not re.fullmatch(r"season\s*\d+", folded)
            and not re.fullmatch(r"(?:disc|disk)\s*\d+", folded)
        ):
            return cleaned
    return filename_show


class LibraryScanner:
    def __init__(self, database: Database, config: FluxaConfig) -> None:
        self.database = database
        self.config = config

    def sync_libraries(self) -> None:
        configured = {str(library.path): library for library in self.config.libraries}
        with self.database.connect() as connection:
            for library in self.config.libraries:
                connection.execute(
                    """
                    INSERT INTO libraries(name, kind, root_path, enabled)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(root_path) DO UPDATE SET
                        name = excluded.name,
                        kind = excluded.kind,
                        enabled = 1
                    """,
                    (library.name, library.kind, str(library.path)),
                )
            if configured:
                placeholders = ",".join("?" for _ in configured)
                connection.execute(
                    f"UPDATE libraries SET enabled = 0 WHERE root_path NOT IN ({placeholders})",
                    tuple(configured),
                )

    def scan_all(self) -> list[ScanResult]:
        self.sync_libraries()
        return [self.scan_library(library) for library in self.config.libraries]

    def scan_library(self, library: LibraryConfig) -> ScanResult:
        started = time.monotonic()
        scan_token = f"{time.time_ns()}-{os.getpid()}"
        discovered = added = changed = skipped = 0
        root = library.path

        if not root.is_dir():
            error = f"Library folder is unavailable: {root}"
            self._record_scan_error(root, error)
            return ScanResult(
                library.name, 0, 0, 0, 0, 0, time.monotonic() - started, error
            )

        try:
            root_resolved = root.resolve(strict=True)
            with self.database.connect() as connection:
                library_row = connection.execute(
                    "SELECT id FROM libraries WHERE root_path = ?", (str(root),)
                ).fetchone()
                if library_row is None:
                    raise RuntimeError(f"Library is not registered: {root}")
                library_id = int(library_row["id"])

                for base, directories, files in os.walk(root, followlinks=False):
                    directories[:] = [
                        name
                        for name in directories
                        if not name.startswith(".")
                        and name.casefold() not in IGNORED_DIRECTORIES
                    ]
                    for file_name in files:
                        path = Path(base) / file_name
                        extension = path.suffix.casefold()
                        media_type = self._media_type(extension, library.kind)
                        if media_type is None:
                            continue
                        try:
                            if path.is_symlink():
                                resolved = path.resolve(strict=True)
                                if not resolved.is_relative_to(root_resolved):
                                    skipped += 1
                                    continue
                            stat = path.stat()
                            relative = str(path.relative_to(root))
                        except (OSError, ValueError):
                            skipped += 1
                            continue

                        discovered += 1
                        existing = connection.execute(
                            "SELECT id, size_bytes, modified_ns FROM media WHERE path = ?",
                            (str(path),),
                        ).fetchone()
                        is_changed = bool(
                            existing
                            and (
                                int(existing["size_bytes"]) != stat.st_size
                                or int(existing["modified_ns"]) != stat.st_mtime_ns
                            )
                        )
                        title = clean_title(path.stem)
                        show, season, episode = episode_identity(path.stem)
                        show = show_title_from_path(Path(relative), show)
                        parsed_episode_title = episode_title(path.stem)
                        if parsed_episode_title:
                            title = parsed_episode_title
                        connection.execute(
                            """
                            INSERT INTO media(
                                library_id, path, relative_path, file_name, title,
                                sort_title, media_type, extension, size_bytes,
                                modified_ns, available, last_seen_scan, show_title,
                                season_number, episode_number
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                            ON CONFLICT(path) DO UPDATE SET
                                library_id = excluded.library_id,
                                relative_path = excluded.relative_path,
                                file_name = excluded.file_name,
                                title = CASE
                                    WHEN ? = 1 THEN excluded.title
                                    WHEN media.probe_status = 'done' AND
                                         media.size_bytes = excluded.size_bytes AND
                                         media.modified_ns = excluded.modified_ns AND
                                         media.title NOT GLOB '[0-9][0-9][0-9]-S*'
                                    THEN media.title ELSE excluded.title END,
                                sort_title = CASE
                                    WHEN ? = 1 THEN excluded.sort_title
                                    WHEN media.probe_status = 'done' AND
                                         media.size_bytes = excluded.size_bytes AND
                                         media.modified_ns = excluded.modified_ns AND
                                         media.title NOT GLOB '[0-9][0-9][0-9]-S*'
                                    THEN media.sort_title ELSE excluded.sort_title END,
                                media_type = excluded.media_type,
                                extension = excluded.extension,
                                size_bytes = excluded.size_bytes,
                                modified_ns = excluded.modified_ns,
                                available = 1,
                                last_seen_scan = excluded.last_seen_scan,
                                probe_status = CASE
                                    WHEN media.size_bytes != excluded.size_bytes OR
                                         media.modified_ns != excluded.modified_ns
                                    THEN 'pending' ELSE media.probe_status END,
                                probe_error = CASE
                                    WHEN media.size_bytes != excluded.size_bytes OR
                                         media.modified_ns != excluded.modified_ns
                                    THEN NULL ELSE media.probe_error END,
                                show_title = CASE
                                    WHEN media.show_title IS NULL OR
                                         media.show_title NOT GLOB '*[^0-9]*'
                                    THEN excluded.show_title ELSE media.show_title END,
                                season_number = COALESCE(media.season_number, excluded.season_number),
                                episode_number = COALESCE(media.episode_number, excluded.episode_number),
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (
                                library_id,
                                str(path),
                                relative,
                                file_name,
                                title,
                                sort_key(title),
                                media_type,
                                extension,
                                stat.st_size,
                                stat.st_mtime_ns,
                                scan_token,
                                show,
                                season,
                                episode,
                                int(parsed_episode_title is not None),
                                int(parsed_episode_title is not None),
                            ),
                        )
                        if existing is None:
                            added += 1
                        elif is_changed:
                            changed += 1
                            connection.execute(
                                "DELETE FROM loudness_analyses WHERE media_id = ?",
                                (int(existing["id"]),),
                            )

                unavailable = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM media
                    WHERE library_id = ? AND available = 1 AND last_seen_scan != ?
                    """,
                    (library_id, scan_token),
                ).fetchone()["count"]
                connection.execute(
                    """
                    UPDATE media SET available = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE library_id = ? AND last_seen_scan != ?
                    """,
                    (library_id, scan_token),
                )
                connection.execute(
                    """
                    UPDATE libraries
                    SET last_scan_at = CURRENT_TIMESTAMP, last_scan_error = NULL
                    WHERE id = ?
                    """,
                    (library_id,),
                )
        except (OSError, RuntimeError) as exc:
            error = str(exc)
            self._record_scan_error(root, error)
            return ScanResult(
                library.name,
                discovered,
                added,
                changed,
                0,
                skipped,
                time.monotonic() - started,
                error,
            )

        return ScanResult(
            library.name,
            discovered,
            added,
            changed,
            int(unavailable),
            skipped,
            time.monotonic() - started,
        )

    def _record_scan_error(self, root: Path, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE libraries SET last_scan_error = ? WHERE root_path = ?",
                (error, str(root)),
            )

    @staticmethod
    def _media_type(extension: str, kind: str) -> str | None:
        if extension in VIDEO_EXTENSIONS and kind == "video":
            return "video"
        if extension in AUDIO_EXTENSIONS and kind == "music":
            return "audio"
        return None
