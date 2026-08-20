from __future__ import annotations

import json
import math
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SEGMENT_PREFIX = "segment-"
PLAYLIST_NAME = "index.m3u8"


class TranscodeError(RuntimeError):
    pass


class TranscodeNotFound(TranscodeError):
    pass


class TranscodeNotReady(TranscodeError):
    pass


@dataclass
class CompatibilitySession:
    id: str
    media_id: int
    directory: Path
    process: subprocess.Popen[bytes]
    start_ms: int
    duration_ms: int
    video_mode: str
    segment_format: str
    subtitle_ordinal: int | None
    leveling_mode: str
    leveling_applied: bool
    log_path: Path
    created_at: float
    last_access: float
    paused: bool = False


class CompatibilityManager:
    """Produces bounded, rolling HLS sessions from source media."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "streams"
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_root = data_dir / "logs" / "transcodes"
        self.log_root.mkdir(parents=True, exist_ok=True)
        self._prune_log_files()
        self._remove_stale_directories()
        self.ffmpeg = shutil.which("ffmpeg")
        self.lock = threading.RLock()
        self.sessions: dict[str, CompatibilitySession] = {}
        self.cleanup_thread = threading.Thread(
            target=self._cleanup_worker,
            name="fluxa-compatibility-cleanup",
            daemon=True,
        )
        self.cleanup_thread.start()

    def start(
        self,
        *,
        media_id: int,
        source: Path,
        start_ms: int,
        duration_ms: int,
        width: int | None,
        height: int | None,
        analysis_status: str | None,
        envelope_json: object,
        segment_format: str,
        subtitle_ordinal: int | None,
        subtitle_kind: str | None,
        compressor_enabled: bool = True,
        compressor_threshold_db: float = -24.0,
        compressor_ratio: float = 8.0,
        compressor_ceiling_db: float = -3.0,
        compressor_attack_ms: float = 15.0,
        compressor_release_ms: float = 750.0,
        compressor_knee: float = 4.0,
    ) -> CompatibilitySession:
        if not self.ffmpeg:
            raise TranscodeError("FFmpeg is not installed")
        start_ms = max(0, int(start_ms))
        segment_format = "fmp4" if segment_format == "fmp4" else "mpegts"
        if duration_ms and start_ms >= duration_ms - 1_000:
            start_ms = 0
        session_id = secrets.token_urlsafe(18)
        directory = self.root / session_id
        directory.mkdir(mode=0o700)
        leveler_path = self._write_leveler_commands(
            directory, start_ms, analysis_status, envelope_json
        )
        process: subprocess.Popen[bytes] | None = None
        selected_log_path: Path | None = None
        mode = ""
        errors: list[str] = []
        for encoder, candidate_mode in (
            ("h264_nvenc", "GPU H.264 + AAC"),
            ("libx264", "software H.264 + AAC"),
        ):
            self._clear_stream_outputs(directory)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            log_path = self.log_root / (
                f"{stamp}-media-{media_id}-{session_id[:8]}-{encoder}.log"
            )
            command = self._command(
                source=source,
                directory=directory,
                start_ms=start_ms,
                width=width,
                height=height,
                encoder=encoder,
                leveler_path=leveler_path,
                segment_format=segment_format,
                subtitle_ordinal=subtitle_ordinal,
                subtitle_kind=subtitle_kind,
                compressor_enabled=compressor_enabled,
                compressor_threshold_db=compressor_threshold_db,
                compressor_ratio=compressor_ratio,
                compressor_ceiling_db=compressor_ceiling_db,
                compressor_attack_ms=compressor_attack_ms,
                compressor_release_ms=compressor_release_ms,
                compressor_knee=compressor_knee,
            )
            try:
                with log_path.open("wb") as log:
                    log.write(
                        (
                            f"Fluxa session={session_id} media={media_id} "
                            f"start_ms={start_ms} encoder={encoder}\n"
                            f"command={' '.join(command)}\n"
                        ).encode("utf-8", errors="replace")
                    )
                    log.flush()
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=log,
                        start_new_session=True,
                    )
            except OSError as exc:
                errors.append(f"{encoder}: {exc}")
                process = None
                continue
            if self._wait_until_ready(process, directory, timeout=15):
                mode = candidate_mode
                selected_log_path = log_path
                break
            self._terminate_process(process)
            errors.append(f"{encoder}: {self._log_tail(log_path)}")
            process = None

        if process is None or not mode or selected_log_path is None:
            self._remove_directory(directory)
            detail = "; ".join(errors) or "no compatible encoder started"
            raise TranscodeError(f"Could not start compatibility stream: {detail}")

        now = time.monotonic()
        session = CompatibilitySession(
            id=session_id,
            media_id=media_id,
            directory=directory,
            process=process,
            start_ms=start_ms,
            duration_ms=duration_ms,
            video_mode=mode,
            segment_format=segment_format,
            subtitle_ordinal=subtitle_ordinal,
            leveling_mode="mapped" if leveler_path is not None else "live",
            leveling_applied=True,
            log_path=selected_log_path,
            created_at=now,
            last_access=now,
        )
        with self.lock:
            self.sessions[session_id] = session
        return session

    def get_file(self, session_id: str, name: str) -> Path:
        session = self._session(session_id)
        segment_suffix = ".m4s" if session.segment_format == "fmp4" else ".ts"
        valid_init = session.segment_format == "fmp4" and name == "init.mp4"
        valid_segment = (
            name.startswith(SEGMENT_PREFIX)
            and name.endswith(segment_suffix)
            and name[len(SEGMENT_PREFIX) : -len(segment_suffix)].isdigit()
        )
        if name != PLAYLIST_NAME and not valid_init and not valid_segment:
            raise TranscodeNotFound("Compatibility stream file not found")
        path = session.directory / name
        if not path.is_file():
            raise TranscodeNotReady("Compatibility stream segment is not ready")
        with self.lock:
            session.last_access = time.monotonic()
        return path

    def control(self, session_id: str, action: str) -> CompatibilitySession | None:
        action = action.casefold()
        if action == "close":
            self.close(session_id)
            return None
        session = self._session(session_id)
        with self.lock:
            session.last_access = time.monotonic()
            if action == "heartbeat":
                return session
            if action == "pause" and not session.paused and session.process.poll() is None:
                try:
                    os.killpg(session.process.pid, signal.SIGSTOP)
                    session.paused = True
                except ProcessLookupError:
                    pass
                return session
            if action == "resume" and session.paused and session.process.poll() is None:
                try:
                    os.killpg(session.process.pid, signal.SIGCONT)
                    session.paused = False
                except ProcessLookupError:
                    pass
                return session
        if action not in {"pause", "resume"}:
            raise TranscodeError("Unknown compatibility stream control")
        return session

    def close(self, session_id: str) -> None:
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return
        self._terminate_process(session.process)
        self._remove_directory(session.directory)

    def active_count(self) -> int:
        with self.lock:
            return len(self.sessions)

    def temporary_bytes(self) -> int:
        with self.lock:
            directories = [session.directory for session in self.sessions.values()]
        total = 0
        for directory in directories:
            try:
                total += sum(
                    path.stat().st_size
                    for path in directory.iterdir()
                    if path.is_file()
                )
            except OSError:
                continue
        return total

    def _session(self, session_id: str) -> CompatibilitySession:
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise TranscodeNotFound("Compatibility stream session not found")
        return session

    def _command(
        self,
        *,
        source: Path,
        directory: Path,
        start_ms: int,
        width: int | None,
        height: int | None,
        encoder: str,
        leveler_path: Path | None,
        segment_format: str,
        subtitle_ordinal: int | None,
        subtitle_kind: str | None,
        compressor_enabled: bool = True,
        compressor_threshold_db: float = -24.0,
        compressor_ratio: float = 8.0,
        compressor_ceiling_db: float = -3.0,
        compressor_attack_ms: float = 15.0,
        compressor_release_ms: float = 750.0,
        compressor_knee: float = 4.0,
    ) -> list[str]:
        bitrate, maximum, buffer = _video_rates(width, height)
        video_filters = (
            "bwdif=mode=send_frame:parity=auto:deint=interlaced,"
            "scale=w='min(1920,iw)':h=-2:force_original_aspect_ratio=decrease,"
            "format=yuv420p"
        )
        if leveler_path:
            audio_filters = [
                f"asendcmd=f={leveler_path}",
                "volume@leveler=1.0:precision=float",
            ]
        else:
            # A 31-frame centered window supplies about 7.5 seconds of live
            # lookahead. maxgain=1 prevents quiet dialogue from being boosted;
            # louder regions are eased toward -20 dB RMS before the limiter.
            audio_filters = [
                "dynaudnorm=f=500:g=31:p=0.90:m=1.0:r=0.10:n=true:b=true"
            ]
        ceiling = math.pow(10.0, compressor_ceiling_db / 20.0)
        if compressor_enabled:
            threshold = math.pow(10.0, compressor_threshold_db / 20.0)
            audio_filters.append(
                "acompressor="
                f"threshold={threshold:.8f}:ratio={compressor_ratio:.2f}:"
                f"attack={compressor_attack_ms:.2f}:"
                f"release={compressor_release_ms:.2f}:makeup=1:"
                f"knee={compressor_knee:.2f}:"
                "detection=rms:link=maximum"
            )
        # Disable alimiter's automatic output normalization; otherwise it
        # raises the compressed signal back toward full scale and partly
        # defeats the user's ceiling setting.
        audio_filters.append(
            f"alimiter=limit={ceiling:.8f}:attack=20:release=250:level=false"
        )
        audio_filter = ",".join(audio_filters)
        command = [
            str(self.ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostats",
            "-stats_period",
            "5",
            "-progress",
            "pipe:2",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-readrate",
            "1.08",
            "-readrate_initial_burst",
            "24",
            "-i",
            str(source),
        ]
        if subtitle_kind == "bitmap" and subtitle_ordinal is not None:
            subtitle_filters = (
                f"[0:v:0]{video_filters}[base];"
                f"[0:s:{subtitle_ordinal}]"
                "scale=w='min(1920,iw)':h=-2:force_original_aspect_ratio=decrease,"
                "format=rgba[sub];"
                "[base][sub]overlay=eof_action=pass:shortest=0,format=yuv420p[outv]"
            )
            command.extend(["-filter_complex", subtitle_filters, "-map", "[outv]"])
        else:
            command.extend(["-map", "0:v:0", "-vf", video_filters])
        command.extend(["-map", "0:a:0?", "-c:v", encoder])
        if encoder == "h264_nvenc":
            command.extend(
                [
                    "-forced-idr",
                    "1",
                    "-preset",
                    "p4",
                    "-tune",
                    "hq",
                    "-profile:v",
                    "high",
                    "-rc",
                    "vbr",
                    "-cq",
                    "22",
                    "-b:v",
                    bitrate,
                    "-maxrate",
                    maximum,
                    "-bufsize",
                    buffer,
                ]
            )
        else:
            command.extend(
                [
                    "-preset",
                    "veryfast",
                    "-profile:v",
                    "high",
                    "-crf",
                    "21",
                    "-maxrate",
                    maximum,
                    "-bufsize",
                    buffer,
                ]
            )
        command.extend(
            [
                "-force_key_frames",
                "expr:gte(t,n_forced*4)",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-af",
                audio_filter,
                "-avoid_negative_ts",
                "make_zero",
                # The live leveler holds several seconds of audio for its
                # lookahead window. Keep the muxer from closing a video
                # segment until the matching audio packets are available.
                "-max_interleave_delta",
                "0",
                "-f",
                "hls",
                "-hls_segment_type",
                segment_format,
                "-hls_time",
                "4",
                "-hls_list_size",
                "24",
                "-hls_delete_threshold",
                "6",
                "-hls_flags",
                "delete_segments+independent_segments+temp_file",
            ]
        )
        if segment_format == "fmp4":
            command.extend(
                [
                    "-hls_fmp4_init_filename",
                    "init.mp4",
                    "-hls_segment_filename",
                    str(directory / "segment-%06d.m4s"),
                ]
            )
        else:
            command.extend(
                ["-hls_segment_filename", str(directory / "segment-%06d.ts")]
            )
        command.append(str(directory / PLAYLIST_NAME))
        return command

    def _write_leveler_commands(
        self,
        directory: Path,
        start_ms: int,
        analysis_status: str | None,
        envelope_json: object,
    ) -> Path | None:
        if analysis_status != "done" or not envelope_json:
            return None
        try:
            envelope = json.loads(str(envelope_json))
            points = sorted((int(point[0]), float(point[1])) for point in envelope)
        except (TypeError, ValueError, json.JSONDecodeError, IndexError):
            return None
        if not points or not any(gain < -0.01 for _stamp, gain in points):
            return None
        current_gain = 0.0
        commands: list[tuple[int, float]] = []
        for stamp, gain in points:
            if stamp <= start_ms:
                current_gain = gain
            else:
                commands.append((stamp - start_ms, gain))
        commands.insert(0, (0, current_gain))
        path = directory / "leveler.txt"
        with path.open("w", encoding="utf-8") as output:
            for stamp, gain_db in commands:
                linear = math.pow(10.0, gain_db / 20.0)
                output.write(
                    f"{stamp / 1000:.3f} volume@leveler volume {linear:.8f};\n"
                )
        os.chmod(path, 0o600)
        return path

    @staticmethod
    def _wait_until_ready(
        process: subprocess.Popen[bytes], directory: Path, timeout: float
    ) -> bool:
        deadline = time.monotonic() + timeout
        playlist = directory / PLAYLIST_NAME
        while time.monotonic() < deadline:
            if playlist.is_file():
                try:
                    content = playlist.read_text(encoding="utf-8")
                    segment_count = content.count("#EXTINF:")
                    if segment_count >= 2 or (
                        segment_count >= 1 and "#EXT-X-ENDLIST" in content
                    ):
                        return True
                except OSError:
                    pass
            if process.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    @staticmethod
    def _clear_stream_outputs(directory: Path) -> None:
        for pattern in ("index.m3u8*", "segment-*"):
            for path in directory.glob(pattern):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _log_tail(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return "FFmpeg exited before creating a stream"
        return text[-800:] or "FFmpeg exited before creating a stream"

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGCONT)
        except ProcessLookupError:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _remove_directory(self, directory: Path) -> None:
        try:
            resolved = directory.resolve()
            root = self.root.resolve()
            if resolved.parent == root:
                shutil.rmtree(resolved, ignore_errors=True)
        except OSError:
            pass

    def _cleanup_worker(self) -> None:
        while True:
            time.sleep(15)
            cutoff = time.monotonic() - 75
            with self.lock:
                expired = [
                    session_id
                    for session_id, session in self.sessions.items()
                    if session.last_access < cutoff
                ]
            for session_id in expired:
                self.close(session_id)

    def _remove_stale_directories(self) -> None:
        """A prior service process cannot own a reusable compatibility session."""
        try:
            children = list(self.root.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir() and child.parent == self.root:
                shutil.rmtree(child, ignore_errors=True)

    def _prune_log_files(self) -> None:
        try:
            logs = sorted(
                self.log_root.glob("*.log"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for path in logs[100:]:
            try:
                path.unlink()
            except OSError:
                pass


def _video_rates(width: int | None, height: int | None) -> tuple[str, str, str]:
    pixels = int(width or 0) * int(height or 0)
    if pixels <= 720 * 576:
        return "1600k", "2400k", "4800k"
    if pixels <= 1280 * 720:
        return "3200k", "4800k", "9600k"
    if pixels <= 1920 * 1080:
        return "6000k", "8000k", "16000k"
    return "10000k", "14000k", "28000k"
