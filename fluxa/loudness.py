from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .database import Database


ANALYZER_VERSION = 1
SAMPLE_PATTERN = re.compile(
    r"\bt:\s*(?P<time>\d+(?:\.\d+)?)\b.*?"
    r"\bM:\s*(?P<momentary>-?(?:inf|\d+(?:\.\d+)?))\s+"
    r"S:\s*(?P<short>-?(?:inf|\d+(?:\.\d+)?))",
    re.IGNORECASE,
)
INTEGRATED_PATTERN = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s+LUFS\s*$")
PEAK_PATTERN = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s+dBFS\s*$")


class LoudnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoudnessSample:
    time_ms: int
    momentary_lufs: float
    short_term_lufs: float


@dataclass(frozen=True)
class SpikeSegment:
    attack_start_ms: int
    loud_start_ms: int
    loud_end_ms: int
    release_end_ms: int
    reduction_db: float
    baseline_lufs: float
    loud_lufs: float


@dataclass(frozen=True)
class LoudnessResult:
    integrated_lufs: float | None
    true_peak_db: float | None
    samples: tuple[LoudnessSample, ...]
    envelope: tuple[tuple[int, float], ...]
    segments: tuple[SpikeSegment, ...]


def analyze_file(path: Path, sample_interval_ms: int = 1000) -> LoudnessResult:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "verbose",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-filter:a",
        "ebur128=peak=true:framelog=verbose",
        "-f",
        "null",
        "-",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        raise LoudnessError(f"Could not start FFmpeg: {exc}") from exc

    samples: list[LoudnessSample] = []
    latest_by_bucket: dict[int, LoudnessSample] = {}
    integrated_lufs: float | None = None
    true_peak_db: float | None = None
    recent_errors: list[str] = []
    assert process.stderr is not None
    for line in process.stderr:
        match = SAMPLE_PATTERN.search(line)
        if match:
            momentary = _finite_float(match.group("momentary"))
            short_term = _finite_float(match.group("short"))
            if momentary is not None and short_term is not None and short_term > -70:
                time_ms = round(float(match.group("time")) * 1000)
                sample = LoudnessSample(time_ms, momentary, short_term)
                bucket = time_ms // sample_interval_ms
                previous = latest_by_bucket.get(bucket)
                if previous is None or sample.short_term_lufs > previous.short_term_lufs:
                    latest_by_bucket[bucket] = sample
            continue
        integrated_match = INTEGRATED_PATTERN.match(line)
        if integrated_match:
            integrated_lufs = float(integrated_match.group(1))
            continue
        peak_match = PEAK_PATTERN.match(line)
        if peak_match:
            true_peak_db = float(peak_match.group(1))
            continue
        if "error" in line.casefold() or "invalid" in line.casefold():
            recent_errors.append(line.strip())
            recent_errors = recent_errors[-3:]

    return_code = process.wait()
    if return_code != 0:
        detail = recent_errors[-1] if recent_errors else f"exit status {return_code}"
        raise LoudnessError(f"FFmpeg could not analyze {path.name}: {detail}")

    samples.extend(latest_by_bucket[key] for key in sorted(latest_by_bucket))
    if not samples:
        raise LoudnessError(f"No usable loudness samples were found in {path.name}")
    segments = detect_spike_segments(samples)
    envelope = build_gain_envelope(samples, segments)
    return LoudnessResult(
        integrated_lufs=integrated_lufs,
        true_peak_db=true_peak_db,
        samples=tuple(samples),
        envelope=tuple(envelope),
        segments=tuple(segments),
    )


def detect_spike_segments(
    samples: list[LoudnessSample] | tuple[LoudnessSample, ...],
    minimum_rise_db: float = 5.0,
    lookahead_ms: int = 8000,
    release_ms: int = 6000,
) -> list[SpikeSegment]:
    if len(samples) < 8:
        return []
    segments: list[SpikeSegment] = []
    index = 0
    while index < len(samples):
        current_time = samples[index].time_ms
        prior = [
            sample.short_term_lufs
            for sample in samples
            if current_time - 45_000 <= sample.time_ms < current_time - 2_000
        ]
        if len(prior) < 8:
            index += 1
            continue
        baseline = statistics.median(prior)
        future = [sample.short_term_lufs for sample in samples[index : index + 8]]
        if len(future) < 4:
            break
        loud_level = statistics.median(future)
        rise = loud_level - baseline
        if rise < minimum_rise_db:
            index += 1
            continue

        threshold = baseline + max(2.5, minimum_rise_db * 0.55)
        end_index = index
        quiet_run = 0
        while end_index + 1 < len(samples):
            end_index += 1
            if samples[end_index].short_term_lufs < threshold:
                quiet_run += 1
                if quiet_run >= 5:
                    end_index -= quiet_run
                    break
            else:
                quiet_run = 0
        end_index = max(index, end_index)
        region_levels = [
            sample.short_term_lufs for sample in samples[index : end_index + 1]
        ]
        loud_level = _percentile(region_levels, 0.85)
        relative_reduction = max(0.0, loud_level - baseline - 3.0)
        ceiling_reduction = max(0.0, loud_level - (-20.0))
        reduction = round(min(12.0, max(relative_reduction, ceiling_reduction)), 2)
        if reduction >= 1.0:
            loud_start = max(0, samples[index].time_ms - 1500)
            loud_end = samples[end_index].time_ms
            segments.append(
                SpikeSegment(
                    attack_start_ms=max(0, loud_start - lookahead_ms),
                    loud_start_ms=loud_start,
                    loud_end_ms=loud_end,
                    release_end_ms=loud_end + release_ms,
                    reduction_db=reduction,
                    baseline_lufs=round(baseline, 2),
                    loud_lufs=round(loud_level, 2),
                )
            )
        index = max(index + 1, end_index + 1)
    return segments


def build_gain_envelope(
    samples: list[LoudnessSample] | tuple[LoudnessSample, ...],
    segments: list[SpikeSegment] | tuple[SpikeSegment, ...],
) -> list[tuple[int, float]]:
    if not samples:
        return []
    last_time = max(
        samples[-1].time_ms,
        max((segment.release_end_ms for segment in segments), default=0),
    )
    envelope: list[tuple[int, float]] = []
    for time_ms in range(0, last_time + 1000, 1000):
        gain = 0.0
        for segment in segments:
            if time_ms < segment.attack_start_ms or time_ms > segment.release_end_ms:
                continue
            if time_ms < segment.loud_start_ms:
                span = max(1, segment.loud_start_ms - segment.attack_start_ms)
                progress = (time_ms - segment.attack_start_ms) / span
                candidate = -segment.reduction_db * progress
            elif time_ms <= segment.loud_end_ms:
                candidate = -segment.reduction_db
            else:
                span = max(1, segment.release_end_ms - segment.loud_end_ms)
                remaining = 1 - ((time_ms - segment.loud_end_ms) / span)
                candidate = -segment.reduction_db * remaining
            gain = min(gain, candidate)
        rounded = round(gain, 2)
        if not envelope or rounded != envelope[-1][1] or rounded != 0:
            envelope.append((time_ms, rounded))
    if envelope[-1][0] != last_time or envelope[-1][1] != 0:
        envelope.append((last_time, 0.0))
    return envelope


def analyze_media(database: Database, media_id: int) -> LoudnessResult:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT path FROM media WHERE id = ? AND available = 1", (media_id,)
        ).fetchone()
        if row is None:
            raise LoudnessError("Media item is unavailable")
        connection.execute(
            """
            INSERT INTO loudness_analyses(media_id, analyzer_version, status, error)
            VALUES (?, ?, 'running', NULL)
            ON CONFLICT(media_id) DO UPDATE SET
                analyzer_version = excluded.analyzer_version,
                status = 'running', error = NULL, updated_at = CURRENT_TIMESTAMP
            """,
            (media_id, ANALYZER_VERSION),
        )
    try:
        result = analyze_file(Path(row["path"]))
    except LoudnessError as exc:
        with database.connect() as connection:
            connection.execute(
                """
                INSERT INTO loudness_analyses(media_id, analyzer_version, status, error)
                VALUES (?, ?, 'error', ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    status = 'error', error = excluded.error,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (media_id, ANALYZER_VERSION, str(exc)),
            )
        raise

    envelope_json = json.dumps(result.envelope, separators=(",", ":"))
    segments_json = json.dumps(
        [segment.__dict__ for segment in result.segments], separators=(",", ":")
    )
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE loudness_analyses SET
                analyzer_version = ?, status = 'done', integrated_lufs = ?,
                true_peak_db = ?, sample_interval_ms = 1000, envelope_json = ?,
                spike_segments_json = ?, error = NULL, analyzed_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE media_id = ?
            """,
            (
                ANALYZER_VERSION,
                result.integrated_lufs,
                result.true_peak_db,
                envelope_json,
                segments_json,
                datetime.now(UTC).isoformat(),
                media_id,
            ),
        )
    return result


def _finite_float(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return -70.0
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]
