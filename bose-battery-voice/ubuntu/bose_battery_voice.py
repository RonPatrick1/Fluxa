#!/usr/bin/env python3
"""Announce a Bose battery level after it becomes Ubuntu's active audio output."""

from __future__ import annotations

import argparse
import configparser
import importlib.util
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class Speaker:
    id: str
    name: str
    address: str


ELIZABETH = Speaker("elizabeth", "Elizabeth's Bose", "BC:87:FA:2E:7C:63")
FREDDIE = Speaker("freddie", "Freddie's Bose", "68:F2:1F:93:49:0A")
SPEAKERS = {speaker.id: speaker for speaker in (ELIZABETH, FREDDIE)}

POLL_SECONDS = 2.0
ANNOUNCEMENT_COOLDOWN_SECONDS = 60.0
DEFAULT_SPEECH_TEMPLATE = (
    "{devices} connected to {speaker}. Battery {battery} percent."
)


@dataclass(frozen=True)
class DesktopSettings:
    device_label: str = "Ubuntu desktop"
    speech_template: str = DEFAULT_SPEECH_TEMPLATE
    announcement_volume_percent: int = 45


def config_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "bose-battery-voice" / "settings.ini"


def load_settings(path: Path | None = None) -> DesktopSettings:
    target = path or config_path()
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(target)
    section = parser["announcement"] if parser.has_section("announcement") else {}
    try:
        volume = int(section.get("volume_percent", "45"))
    except ValueError:
        volume = 45
    return DesktopSettings(
        device_label=section.get("device_label", "Ubuntu desktop").strip()
        or "Ubuntu desktop",
        speech_template=section.get(
            "speech_template", DEFAULT_SPEECH_TEMPLATE
        ).strip()
        or DEFAULT_SPEECH_TEMPLATE,
        announcement_volume_percent=max(1, min(volume, 100)),
    )


def save_settings(settings: DesktopSettings, path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser(interpolation=None)
    parser["announcement"] = {
        "device_label": settings.device_label,
        "speech_template": settings.speech_template,
        "volume_percent": str(settings.announcement_volume_percent),
    }
    with target.open("w", encoding="utf-8") as stream:
        parser.write(stream)
    return target


class DesktopBatteryError(RuntimeError):
    pass


class DesktopAnnouncementSkipped(DesktopBatteryError):
    pass


def _load_bose_connection():
    tools_file = Path(__file__).resolve().parents[2] / "bose-flex-tools" / "bose_flex.py"
    if not tools_file.is_file():
        raise DesktopBatteryError(f"Bose protocol helper not found: {tools_file}")
    spec = importlib.util.spec_from_file_location("family_bose_flex", tools_file)
    if spec is None or spec.loader is None:
        raise DesktopBatteryError("Could not load the Bose protocol helper")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.BoseConnection


def run_text(command: Sequence[str], *, timeout: float = 8.0) -> str:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DesktopBatteryError(f"{' '.join(command)} failed: {detail}")
    return completed.stdout


def bluetooth_connected(speaker: Speaker, runner: Callable[..., str] = run_text) -> bool:
    try:
        info = runner(["bluetoothctl", "info", speaker.address])
    except (DesktopBatteryError, subprocess.SubprocessError):
        return False
    return any(line.strip().lower() == "connected: yes" for line in info.splitlines())


def default_sink(runner: Callable[..., str] = run_text) -> str:
    try:
        return runner(["pactl", "get-default-sink"]).strip()
    except (DesktopBatteryError, subprocess.SubprocessError):
        info = runner(["pactl", "info"])
        for line in info.splitlines():
            if line.startswith("Default Sink:"):
                return line.partition(":")[2].strip()
        raise DesktopBatteryError("PipeWire did not report a default audio output")


def sink_matches(sink_name: str, speaker: Speaker) -> bool:
    address_token = speaker.address.replace(":", "_").lower()
    normalized_sink = sink_name.lower().replace("’", "'")
    normalized_name = speaker.name.lower().replace("’", "'")
    return address_token in normalized_sink or normalized_name in normalized_sink


def active_audio_route(speaker: Speaker, runner: Callable[..., str] = run_text) -> bool:
    try:
        return sink_matches(default_sink(runner), speaker)
    except (DesktopBatteryError, subprocess.SubprocessError):
        return False


def read_snapshot(speaker: Speaker) -> tuple[int, tuple[object, ...]]:
    connection = _load_bose_connection()
    try:
        with connection(speaker.address, timeout=5.0) as device:
            level = max(0, min(int(device.get_battery()), 100))
            try:
                sources = tuple(device.get_connected_devices())
            except Exception:
                sources = ()
            return level, sources
    except Exception as error:
        raise DesktopBatteryError(str(error)) from error


def automatic_announcer(sources: Sequence[object]) -> object | None:
    return max(sources, key=lambda source: source.name.casefold(), default=None)


def should_announce_automatically(sources: Sequence[object]) -> bool:
    if len(sources) < 2:
        return True
    current = next(
        (source for source in sources if source.is_current_device),
        None,
    )
    elected = automatic_announcer(sources)
    return current is None or elected is None or current.name.casefold() == elected.name.casefold()


def connected_devices_phrase(
    sources: Sequence[object],
    settings: DesktopSettings | None = None,
) -> str:
    current_settings = settings or load_settings()
    names: list[str] = []
    for source in sources:
        name = current_settings.device_label if source.is_current_device else source.name
        if name and name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
    if not names:
        return current_settings.device_label
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def speak(
    level: int,
    sources: Sequence[object] = (),
    runner: Callable[..., str] = run_text,
    settings: DesktopSettings | None = None,
    speaker: Speaker = ELIZABETH,
) -> None:
    current_settings = settings or load_settings()
    devices = connected_devices_phrase(sources, current_settings)
    sentence = (
        current_settings.speech_template
        .replace("{devices}", devices)
        .replace("{device}", current_settings.device_label)
        .replace("{speaker}", speaker.name)
        .replace("{battery}", str(max(0, min(level, 100))))
    )
    previous_volume: int | None = None
    target_volume = current_settings.announcement_volume_percent
    try:
        volume_text = runner(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"]
        )
        match = re.search(r"/\s*(\d+)%", volume_text)
        if match:
            previous_volume = int(match.group(1))
            if previous_volume < target_volume:
                runner([
                    "pactl",
                    "set-sink-volume",
                    "@DEFAULT_SINK@",
                    f"{target_volume}%",
                ])
    except (DesktopBatteryError, subprocess.SubprocessError):
        previous_volume = None
    try:
        runner(["spd-say", "--wait", sentence], timeout=20.0)
    finally:
        if previous_volume is not None and previous_volume < target_volume:
            try:
                runner([
                    "pactl",
                    "set-sink-volume",
                    "@DEFAULT_SINK@",
                    f"{previous_volume}%",
                ])
            except (DesktopBatteryError, subprocess.SubprocessError):
                pass


def announce_if_active(
    speaker: Speaker,
    *,
    force: bool = False,
    coordinate_helpers: bool = False,
    runner: Callable[..., str] = run_text,
) -> int:
    if not bluetooth_connected(speaker, runner):
        raise DesktopBatteryError(f"{speaker.name} is not connected")
    if not active_audio_route(speaker, runner):
        raise DesktopBatteryError(f"{speaker.name} is not the current audio output")
    level, sources = read_snapshot(speaker)
    if not force and coordinate_helpers and not should_announce_automatically(sources):
        elected = automatic_announcer(sources)
        name = elected.name if elected is not None else "the other connected device"
        raise DesktopAnnouncementSkipped(f"{name} will announce both connected devices")
    if not active_audio_route(speaker, runner):
        raise DesktopBatteryError(f"{speaker.name} stopped being the audio output")
    if not force and not bluetooth_connected(speaker, runner):
        raise DesktopBatteryError(f"{speaker.name} disconnected before the announcement")
    speak(level, sources, runner, speaker=speaker)
    return level


def monitor(speakers: Sequence[Speaker]) -> int:
    # Establish a quiet baseline: starting/restarting the service must not speak.
    connected = {speaker.id: bluetooth_connected(speaker) for speaker in speakers}
    active = {
        speaker.id: connected[speaker.id] and active_audio_route(speaker)
        for speaker in speakers
    }
    last_announced = {speaker.id: 0.0 for speaker in speakers}
    print(
        "Monitoring " + ", ".join(speaker.name for speaker in speakers),
        flush=True,
    )
    while True:
        for speaker in speakers:
            was_connected = connected[speaker.id]
            is_connected = bluetooth_connected(speaker)
            is_active = is_connected and active_audio_route(speaker)
            became_active = is_active and not active[speaker.id]
            connected[speaker.id] = is_connected
            active[speaker.id] = is_active
            if not became_active:
                continue
            if time.monotonic() - last_announced[speaker.id] < ANNOUNCEMENT_COOLDOWN_SECONDS:
                continue
            try:
                level = announce_if_active(
                    speaker,
                    coordinate_helpers=not was_connected,
                )
                last_announced[speaker.id] = time.monotonic()
                print(f"{speaker.name}: announced {level}%", flush=True)
            except DesktopBatteryError as error:
                print(f"{speaker.name}: {error}", file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Announce a Bose battery level only through its active audio route."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    monitor_parser = subparsers.add_parser("monitor", help="watch for new connections")
    monitor_parser.add_argument(
        "--speaker",
        choices=("elizabeth", "freddie", "both"),
        default="elizabeth",
        help="Elizabeth is the default; Freddie retains his onboard announcement",
    )

    once_parser = subparsers.add_parser("once", help="read and announce now")
    once_parser.add_argument("speaker", choices=tuple(SPEAKERS))

    status_parser = subparsers.add_parser("status", help="show connection and route state")
    status_parser.add_argument("speaker", choices=tuple(SPEAKERS), nargs="?")

    settings_parser = subparsers.add_parser(
        "settings",
        help="show or change the Ubuntu announcement settings",
    )
    settings_parser.add_argument("--device-label")
    settings_parser.add_argument("--template")
    settings_parser.add_argument("--volume", type=int, choices=range(1, 101), metavar="1-100")
    return parser


def selected_speakers(value: str) -> tuple[Speaker, ...]:
    if value == "both":
        return (ELIZABETH, FREDDIE)
    return (SPEAKERS[value],)


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "settings":
            current = load_settings()
            updated = DesktopSettings(
                device_label=args.device_label or current.device_label,
                speech_template=args.template or current.speech_template,
                announcement_volume_percent=args.volume or current.announcement_volume_percent,
            )
            if any((args.device_label, args.template, args.volume)):
                save_settings(updated)
            print(f"Settings file: {config_path()}")
            print(f"Device label: {updated.device_label}")
            print(f"Template: {updated.speech_template}")
            print(f"Announcement volume: {updated.announcement_volume_percent}%")
            return 0
        if args.command == "monitor":
            return monitor(selected_speakers(args.speaker))
        if args.command == "once":
            level = announce_if_active(SPEAKERS[args.speaker], force=True)
            print(f"{SPEAKERS[args.speaker].name}: announced {level}%")
            return 0
        speakers = (SPEAKERS[args.speaker],) if args.speaker else tuple(SPEAKERS.values())
        for speaker in speakers:
            connected = bluetooth_connected(speaker)
            active = connected and active_audio_route(speaker)
            print(f"{speaker.name}: connected={connected}, active_output={active}")
        return 0
    except (DesktopBatteryError, KeyboardInterrupt) as error:
        if isinstance(error, KeyboardInterrupt):
            return 130
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
