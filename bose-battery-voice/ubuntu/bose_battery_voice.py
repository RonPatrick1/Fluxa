#!/usr/bin/env python3
"""Announce a Bose battery level after it becomes Ubuntu's active audio output."""

from __future__ import annotations

import argparse
import importlib.util
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
LOCAL_DEVICE_LABEL = "Ubuntu desktop"


class DesktopBatteryError(RuntimeError):
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


def connected_devices_phrase(sources: Sequence[object]) -> str:
    names: list[str] = []
    for source in sources:
        name = LOCAL_DEVICE_LABEL if source.is_current_device else source.name
        if name and name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
    if not names:
        return LOCAL_DEVICE_LABEL
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def speak(
    level: int,
    sources: Sequence[object] = (),
    runner: Callable[..., str] = run_text,
) -> None:
    devices = connected_devices_phrase(sources)
    runner(
        [
            "spd-say",
            "--wait",
            f"{devices} connected to Elizabeth's Bose. Battery {level} percent.",
        ],
        timeout=20.0,
    )


def announce_if_active(
    speaker: Speaker,
    *,
    force: bool = False,
    runner: Callable[..., str] = run_text,
) -> int:
    if not bluetooth_connected(speaker, runner):
        raise DesktopBatteryError(f"{speaker.name} is not connected")
    if not active_audio_route(speaker, runner):
        raise DesktopBatteryError(f"{speaker.name} is not the current audio output")
    level, sources = read_snapshot(speaker)
    if not active_audio_route(speaker, runner):
        raise DesktopBatteryError(f"{speaker.name} stopped being the audio output")
    if not force and not bluetooth_connected(speaker, runner):
        raise DesktopBatteryError(f"{speaker.name} disconnected before the announcement")
    speak(level, sources, runner)
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
                level = announce_if_active(speaker)
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
    return parser


def selected_speakers(value: str) -> tuple[Speaker, ...]:
    if value == "both":
        return (ELIZABETH, FREDDIE)
    return (SPEAKERS[value],)


def main() -> int:
    args = build_parser().parse_args()
    try:
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
