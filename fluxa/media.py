from __future__ import annotations

import json
import mimetypes
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .database import Database
from .library import episode_title, sort_key


CONTENT_TYPES = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "video/webm",
    ".wav": "audio/wav",
}


class ProbeError(RuntimeError):
    pass


TEXT_SUBTITLE_CODECS = {
    "ass",
    "eia_608",
    "eia_708",
    "mov_text",
    "ssa",
    "subrip",
    "text",
    "webvtt",
}
BITMAP_SUBTITLE_CODECS = {
    "dvd_subtitle",
    "dvb_subtitle",
    "hdmv_pgs_subtitle",
    "xsub",
}


@dataclass(frozen=True)
class Chapter:
    start_ms: int
    end_ms: int
    title: str


@dataclass(frozen=True)
class SubtitleStream:
    ordinal: int
    stream_index: int
    codec: str
    language: str | None
    title: str
    kind: str
    default: bool
    forced: bool
    hearing_impaired: bool


@dataclass(frozen=True)
class ProbeResult:
    duration_ms: int | None
    container: str | None
    video_codec: str | None
    width: int | None
    height: int | None
    audio_codec: str | None
    audio_channels: int | None
    audio_tracks: int
    subtitle_tracks: int
    title: str | None
    show_title: str | None
    season_number: int | None
    episode_number: int | None
    chapters: tuple[Chapter, ...]
    subtitles: tuple[SubtitleStream, ...]


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(
        path.suffix.casefold(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )


def run_ffprobe(path: Path, timeout: float = 60.0) -> ProbeResult:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"ffprobe failed for {path.name}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1:] or ["unknown error"]
        raise ProbeError(f"ffprobe failed for {path.name}: {detail[0]}")
    try:
        payload: dict[str, Any] = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path.name}") from exc

    streams = payload.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    video = videos[0] if videos else {}
    audio = audios[0] if audios else {}
    format_data = payload.get("format", {})
    tags = {str(key).casefold(): value for key, value in format_data.get("tags", {}).items()}
    chapter_items: list[Chapter] = []
    for index, chapter in enumerate(payload.get("chapters", []), start=1):
        start = _float_or_none(chapter.get("start_time"))
        end = _float_or_none(chapter.get("end_time"))
        if start is None or end is None or end <= start:
            continue
        chapter_tags = {
            str(key).casefold(): value
            for key, value in (chapter.get("tags") or {}).items()
        }
        chapter_items.append(
            Chapter(
                start_ms=max(0, round(start * 1000)),
                end_ms=max(0, round(end * 1000)),
                title=_clean_tag(chapter_tags.get("title")) or f"Chapter {index}",
            )
        )
    subtitle_items: list[SubtitleStream] = []
    for ordinal, stream in enumerate(subtitles):
        subtitle_tags = {
            str(key).casefold(): value
            for key, value in (stream.get("tags") or {}).items()
        }
        disposition = stream.get("disposition") or {}
        language = _clean_tag(subtitle_tags.get("language"))
        codec = str(stream.get("codec_name") or "unknown")
        supplied_title = _clean_tag(subtitle_tags.get("title"))
        label = supplied_title or ((language or "Unknown").upper() + " captions")
        if disposition.get("hearing_impaired") and "SDH" not in label.upper():
            label += " (SDH)"
        kind = (
            "text"
            if codec in TEXT_SUBTITLE_CODECS
            else ("bitmap" if codec in BITMAP_SUBTITLE_CODECS else "unsupported")
        )
        subtitle_items.append(
            SubtitleStream(
                ordinal=ordinal,
                stream_index=int(stream.get("index", ordinal)),
                codec=codec,
                language=language,
                title=label,
                kind=kind,
                default=bool(disposition.get("default")),
                forced=bool(disposition.get("forced")),
                hearing_impaired=bool(disposition.get("hearing_impaired")),
            )
        )

    duration = _float_or_none(format_data.get("duration"))
    if duration is None:
        durations = [_float_or_none(stream.get("duration")) for stream in streams]
        finite = [value for value in durations if value is not None]
        duration = max(finite, default=None)

    return ProbeResult(
        duration_ms=round(duration * 1000) if duration is not None else None,
        container=_first_name(format_data.get("format_name")),
        video_codec=video.get("codec_name"),
        width=_int_or_none(video.get("width")),
        height=_int_or_none(video.get("height")),
        audio_codec=audio.get("codec_name"),
        audio_channels=_int_or_none(audio.get("channels")),
        audio_tracks=len(audios),
        subtitle_tracks=len(subtitles),
        title=_clean_tag(tags.get("title")),
        show_title=_clean_tag(tags.get("show") or tags.get("album")),
        season_number=_int_or_none(tags.get("season_number")),
        episode_number=_int_or_none(tags.get("episode_id") or tags.get("episode_sort")),
        chapters=tuple(chapter_items),
        subtitles=tuple(subtitle_items),
    )


def probe_media(database: Database, media_id: int) -> ProbeResult:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT path, title FROM media WHERE id = ? AND available = 1", (media_id,)
        ).fetchone()
    if row is None:
        raise ProbeError("Media item is unavailable")
    path = Path(row["path"])
    try:
        result = run_ffprobe(path)
    except ProbeError as exc:
        with database.connect() as connection:
            connection.execute(
                """
                UPDATE media SET probe_status = 'error', probe_error = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (str(exc), media_id),
            )
        raise

    # MakeMKV commonly applies a disc-level title to every episode on a disc.
    # A recognized episode filename is more specific and should win without
    # altering the source container's metadata.
    effective_title = episode_title(path.stem) or result.title or str(row["title"])
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE media SET
                title = ?, sort_title = ?, probe_status = 'done', probe_error = NULL,
                duration_ms = ?, container = ?, video_codec = ?, width = ?, height = ?,
                audio_codec = ?, audio_channels = ?, audio_tracks = ?, subtitle_tracks = ?,
                chapters_json = ?, subtitle_streams_json = ?,
                show_title = COALESCE(?, show_title),
                season_number = COALESCE(?, season_number),
                episode_number = COALESCE(?, episode_number),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                effective_title,
                sort_key(effective_title),
                result.duration_ms,
                result.container,
                result.video_codec,
                result.width,
                result.height,
                result.audio_codec,
                result.audio_channels,
                result.audio_tracks,
                result.subtitle_tracks,
                json.dumps([asdict(chapter) for chapter in result.chapters], separators=(",", ":")),
                json.dumps([asdict(track) for track in result.subtitles], separators=(",", ":")),
                result.show_title,
                result.season_number,
                result.episode_number,
                media_id,
            ),
        )
    return result


def _first_name(value: object) -> str | None:
    if not value:
        return None
    return str(value).split(",", 1)[0]


def _float_or_none(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _clean_tag(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
