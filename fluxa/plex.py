from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .database import Database
from .library import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, sort_key


DEFAULT_PLEX_DATABASE = Path(
    "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/"
    "Plug-in Support/Databases/com.plexapp.plugins.library.db"
)


class PlexImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlexPlaylistImport:
    playlists: int
    items: int
    matched: int
    unavailable: int


def import_plex_playlists(
    database: Database, plex_database: Path = DEFAULT_PLEX_DATABASE
) -> PlexPlaylistImport:
    """Copy Plex playlist metadata without modifying Plex or source media."""
    plex_database = plex_database.expanduser().resolve()
    if not plex_database.is_file():
        raise PlexImportError(f"Plex library database not found: {plex_database}")

    uri = f"file:{quote(plex_database.as_posix(), safe='/')}?mode=ro"
    try:
        plex = sqlite3.connect(uri, uri=True, timeout=30)
        plex.row_factory = sqlite3.Row
        plex.execute("PRAGMA query_only = ON")
        plex.execute("BEGIN")
        playlists = plex.execute(
            """
            SELECT id, title, title_sort, media_item_count, updated_at
            FROM metadata_items
            WHERE metadata_type = 15 AND deleted_at IS NULL
            ORDER BY title_sort COLLATE NOCASE, id
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise PlexImportError(f"Could not read Plex playlists: {exc}") from exc

    exact_paths, canonical_paths, media_types = _fluxa_media_paths(database)
    imported: list[tuple[sqlite3.Row, list[dict[str, object]]]] = []
    matched_total = 0
    try:
        for playlist in playlists:
            source_items = plex.execute(
                """
                SELECT g.id AS generator_id, g.metadata_item_id, g.[order] AS source_order,
                       item.title AS item_title, item.metadata_type,
                       mp.file AS source_path
                FROM play_queue_generators g
                LEFT JOIN metadata_items item ON item.id = g.metadata_item_id
                LEFT JOIN media_parts mp ON mp.id = (
                    SELECT mp2.id
                    FROM media_items mi2
                    JOIN media_parts mp2 ON mp2.media_item_id = mi2.id
                    WHERE mi2.metadata_item_id = g.metadata_item_id
                      AND mi2.deleted_at IS NULL
                      AND mp2.deleted_at IS NULL
                    ORDER BY mi2.id, mp2.[index], mp2.id
                    LIMIT 1
                )
                WHERE g.playlist_id = ?
                ORDER BY g.[order], g.id
                """,
                (playlist["id"],),
            ).fetchall()
            items: list[dict[str, object]] = []
            for position, source in enumerate(source_items):
                source_path = source["source_path"]
                media_id = _match_media_id(source_path, exact_paths, canonical_paths)
                if media_id is not None:
                    matched_total += 1
                title = source["item_title"] or _title_from_path(source_path)
                items.append(
                    {
                        "position": position,
                        "media_id": media_id,
                        "source_item_id": str(source["generator_id"]),
                        "source_order": source["source_order"],
                        "source_path": source_path,
                        "source_title": title,
                        "kind": _item_kind(source_path, media_id, media_types),
                    }
                )
            imported.append((playlist, items))
    except sqlite3.Error as exc:
        raise PlexImportError(f"Could not read Plex playlist items: {exc}") from exc
    finally:
        plex.close()

    source_ids = [str(playlist["id"]) for playlist, _items in imported]
    with database.connect() as connection:
        for playlist, items in imported:
            kinds = {str(item["kind"]) for item in items if item["kind"]}
            kind = kinds.pop() if len(kinds) == 1 else "mixed"
            source_id = str(playlist["id"])
            connection.execute(
                """
                INSERT INTO playlists(
                    source, source_id, title, sort_title, kind,
                    source_item_count, matched_item_count, source_updated_at
                ) VALUES ('plex', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_id) DO UPDATE SET
                    title = excluded.title,
                    sort_title = excluded.sort_title,
                    kind = excluded.kind,
                    source_item_count = excluded.source_item_count,
                    matched_item_count = excluded.matched_item_count,
                    source_updated_at = excluded.source_updated_at,
                    imported_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    source_id,
                    playlist["title"] or f"Plex playlist {source_id}",
                    playlist["title_sort"]
                    or sort_key(playlist["title"] or f"Plex playlist {source_id}"),
                    kind,
                    len(items),
                    sum(item["media_id"] is not None for item in items),
                    playlist["updated_at"],
                ),
            )
            row = connection.execute(
                "SELECT id FROM playlists WHERE source = 'plex' AND source_id = ?",
                (source_id,),
            ).fetchone()
            assert row is not None
            playlist_id = int(row["id"])
            connection.execute(
                "DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,)
            )
            connection.executemany(
                """
                INSERT INTO playlist_items(
                    playlist_id, position, media_id, source_item_id,
                    source_order, source_path, source_title
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        playlist_id,
                        item["position"],
                        item["media_id"],
                        item["source_item_id"],
                        item["source_order"],
                        item["source_path"],
                        item["source_title"],
                    )
                    for item in items
                ],
            )
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            connection.execute(
                f"DELETE FROM playlists WHERE source = 'plex' AND source_id NOT IN ({placeholders})",
                tuple(source_ids),
            )
        else:
            connection.execute("DELETE FROM playlists WHERE source = 'plex'")

    item_total = sum(len(items) for _playlist, items in imported)
    return PlexPlaylistImport(
        playlists=len(imported),
        items=item_total,
        matched=matched_total,
        unavailable=item_total - matched_total,
    )


def _fluxa_media_paths(
    database: Database,
) -> tuple[dict[str, int], dict[str, int], dict[int, str]]:
    exact: dict[str, int] = {}
    canonical: dict[str, int] = {}
    media_types: dict[int, str] = {}
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, path, media_type FROM media WHERE available = 1 ORDER BY id"
        ).fetchall()
    for row in rows:
        media_id = int(row["id"])
        path = str(row["path"])
        exact[path] = media_id
        canonical.setdefault(os.path.realpath(path), media_id)
        media_types[media_id] = str(row["media_type"])
    return exact, canonical, media_types


def _match_media_id(
    source_path: object,
    exact_paths: dict[str, int],
    canonical_paths: dict[str, int],
) -> int | None:
    if not source_path:
        return None
    value = str(source_path)
    return exact_paths.get(value) or canonical_paths.get(os.path.realpath(value))


def _title_from_path(source_path: object) -> str:
    if source_path:
        return Path(str(source_path)).stem
    return "Unavailable Plex item"


def _item_kind(
    source_path: object, media_id: int | None, media_types: dict[int, str]
) -> str | None:
    if media_id is not None:
        return media_types.get(media_id)
    extension = Path(str(source_path or "")).suffix.casefold()
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    return None
