#!/usr/bin/env python3
"""Control paired Bose SoundLink speakers without the Bose app.

This uses Bose's BMAP protocol over Bluetooth RFCOMM. It does not contact
Bose, download firmware, or install updates.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
from dataclasses import dataclass


MAX_NAME_BYTES = 31
DEFAULT_CHANNELS = (1, 2, 8)
MAC_PATTERN = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class BoseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Packet:
    block: int
    function: int
    operator: int
    payload: bytes


@dataclass(frozen=True)
class VoicePromptConfig:
    enabled: bool
    language: int
    supported_languages: int
    default_language: bool
    user_can_toggle: bool
    battery_level_supported: bool
    battery_level_enabled: bool

    @classmethod
    def from_payload(cls, payload: bytes) -> "VoicePromptConfig":
        if len(payload) not in (5, 7):
            raise BoseError(
                f"Unexpected voice-prompt reply length: {len(payload)} bytes"
            )
        flags = payload[0]
        battery_supported = len(payload) == 7 and bool(payload[5] & 0x01)
        return cls(
            enabled=bool((flags >> 5) & 0x01),
            language=flags & 0x1F,
            supported_languages=int.from_bytes(payload[1:5], "big"),
            default_language=bool((flags >> 6) & 0x01),
            user_can_toggle=bool((flags >> 7) & 0x01),
            battery_level_supported=battery_supported,
            battery_level_enabled=(
                battery_supported and bool(payload[6] & 0x01)
            ),
        )


@dataclass(frozen=True)
class FirmwareUpdateStatus:
    protocol_version: str
    state: int
    bytes_written: int
    staged_version: str

    @property
    def state_name(self) -> str:
        return {
            0: "Error",
            1: "Idle",
            2: "Ready for data transfer",
            3: "Ready for validation",
            4: "Ready to run update",
            5: "Validation pending",
            6: "Peripheral update pending",
        }.get(self.state, f"Unknown ({self.state})")


class BoseConnection:
    def __init__(self, address: str, timeout: float = 3.0) -> None:
        if not MAC_PATTERN.fullmatch(address):
            raise BoseError(f"Invalid Bluetooth address: {address}")
        self.address = address.upper()
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.channel: int | None = None
        self.bmap_version = "unknown"

    def __enter__(self) -> "BoseConnection":
        errors: list[str] = []
        for channel in DEFAULT_CHANNELS:
            candidate = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            )
            candidate.settimeout(self.timeout)
            try:
                candidate.connect((self.address, channel))
                self.sock = candidate
                self.channel = channel
                self._send(Packet(0x00, 0x01, 0x01, b""))
                hello = self._receive_expected(0x00, 0x01, 0x03)
                self.bmap_version = hello.payload.decode("ascii", errors="replace")
                return self
            except (OSError, BoseError) as exc:
                errors.append(f"channel {channel}: {exc}")
                candidate.close()
                self.sock = None
                self.channel = None
        raise BoseError(
            f"Could not open the Bose control service on {self.address} ("
            + "; ".join(errors)
            + ")"
        )

    def __exit__(self, *_: object) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _recv_exactly(self, length: int) -> bytes:
        if self.sock is None:
            raise BoseError("Bluetooth connection is not open")
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.sock.recv(length - len(chunks))
            if not chunk:
                raise BoseError("Speaker closed the Bluetooth control connection")
            chunks.extend(chunk)
        return bytes(chunks)

    def _receive(self) -> Packet:
        header = self._recv_exactly(4)
        payload = self._recv_exactly(header[3])
        return Packet(header[0], header[1], header[2] & 0x0F, payload)

    def _receive_expected(self, block: int, function: int, operator: int) -> Packet:
        for _ in range(8):
            packet = self._receive()
            if (
                packet.block == block
                and packet.function == function
                and packet.operator == operator
            ):
                return packet
            if packet.operator == 0x04:
                raise BoseError(
                    f"Speaker returned protocol error {packet.payload.hex()}"
                )
        raise BoseError("Did not receive the expected reply from the speaker")

    def _send(self, packet: Packet) -> None:
        if self.sock is None:
            raise BoseError("Bluetooth connection is not open")
        if len(packet.payload) > 255:
            raise BoseError("BMAP payload is too long")
        header = bytes(
            (packet.block, packet.function, packet.operator, len(packet.payload))
        )
        self.sock.sendall(header + packet.payload)

    def get_name(self) -> str:
        self._send(Packet(0x01, 0x02, 0x01, b""))
        reply = self._receive_expected(0x01, 0x02, 0x03)
        if not reply.payload:
            raise BoseError("Speaker returned an empty name reply")
        return reply.payload[1:].decode("utf-8")

    def set_name(self, name: str) -> str:
        encoded = name.encode("utf-8")
        if not encoded:
            raise BoseError("Name cannot be empty")
        if len(encoded) > MAX_NAME_BYTES:
            raise BoseError(
                f"Name is {len(encoded)} bytes; Bose allows at most {MAX_NAME_BYTES}"
            )
        self._send(Packet(0x01, 0x02, 0x02, encoded))
        reply = self._receive_expected(0x01, 0x02, 0x03)
        if not reply.payload:
            raise BoseError("Speaker did not confirm its new name")
        confirmed = reply.payload[1:].decode("utf-8")
        if confirmed != name:
            raise BoseError(
                f"Speaker confirmed {confirmed!r} instead of requested name {name!r}"
            )
        return confirmed

    def get_battery(self) -> int:
        self._send(Packet(0x02, 0x02, 0x01, b""))
        reply = self._receive_expected(0x02, 0x02, 0x03)
        if not reply.payload:
            raise BoseError("Speaker returned an empty battery reply")
        return reply.payload[0]

    def get_voice_prompts(self) -> VoicePromptConfig:
        self._send(Packet(0x01, 0x03, 0x01, b""))
        reply = self._receive_expected(0x01, 0x03, 0x03)
        return VoicePromptConfig.from_payload(reply.payload)

    def set_battery_prompt(
        self, enabled: bool, *, force_unsupported: bool = False
    ) -> VoicePromptConfig:
        current = self.get_voice_prompts()
        if not current.battery_level_supported and not force_unsupported:
            raise BoseError(
                "Speaker firmware reports battery voice prompts as unsupported; "
                "use --force only to test whether it still accepts the old setting"
            )

        # This matches Bose's SettingsVoicePromptsSetGetPacket. Preserve the
        # global voice-prompt state and language, and append only the requested
        # battery-level state.
        flags = (int(current.enabled) << 5) | current.language
        payload = bytes((flags, int(enabled)))
        self._send(Packet(0x01, 0x03, 0x02, payload))
        reply = self._receive_expected(0x01, 0x03, 0x03)
        return VoicePromptConfig.from_payload(reply.payload)

    def get_firmware_update_status(self) -> FirmwareUpdateStatus:
        # These are the same read-only GETs made by Bose's app before it starts
        # an update. They do not initialize, transfer, validate, or run one.
        self._send(Packet(0x03, 0x00, 0x01, b""))
        info = self._receive_expected(0x03, 0x00, 0x03)

        self._send(Packet(0x03, 0x01, 0x01, b""))
        state = self._receive_expected(0x03, 0x01, 0x03)
        if not state.payload:
            raise BoseError("Speaker returned an empty firmware-update state")

        self._send(Packet(0x03, 0x04, 0x01, b""))
        sync = self._receive_expected(0x03, 0x04, 0x03)
        if len(sync.payload) < 5:
            raise BoseError("Speaker returned an invalid firmware synchronization reply")

        return FirmwareUpdateStatus(
            protocol_version=info.payload.decode("utf-8", errors="replace"),
            state=state.payload[0],
            bytes_written=int.from_bytes(sync.payload[1:5], "big"),
            staged_version=sync.payload[5:].decode("utf-8", errors="replace"),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control or inspect a paired Bose SoundLink without the Bose app."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("info", "show the stored name and battery percentage"),
        ("battery", "show the battery percentage"),
        ("voice-prompts", "show voice- and battery-prompt capabilities"),
        ("firmware-status", "show read-only firmware transfer state"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("address", help="speaker Bluetooth MAC address")

    rename = subparsers.add_parser("rename", help="change the speaker's stored name")
    rename.add_argument("address", help="speaker Bluetooth MAC address")
    rename.add_argument("name", help="new speaker name (31 UTF-8 bytes maximum)")

    battery_prompt = subparsers.add_parser(
        "battery-prompt", help="enable or disable the spoken battery level"
    )
    battery_prompt.add_argument("address", help="speaker Bluetooth MAC address")
    battery_prompt.add_argument("state", choices=("on", "off"))
    battery_prompt.add_argument(
        "--force",
        action="store_true",
        help="send the old setting even if the firmware reports it unsupported",
    )
    return parser


def print_voice_prompts(config: VoicePromptConfig) -> None:
    print(f"Voice prompts enabled: {config.enabled}")
    print(f"Voice language ID: {config.language}")
    print(f"Voice prompt toggle supported: {config.user_can_toggle}")
    print(f"Battery prompt supported: {config.battery_level_supported}")
    print(f"Battery prompt enabled: {config.battery_level_enabled}")


def print_firmware_status(status: FirmwareUpdateStatus) -> None:
    print(f"Firmware update protocol: {status.protocol_version}")
    print(f"Transfer state: {status.state_name}")
    print(f"Staged bytes: {status.bytes_written}")
    print(f"Staged version: {status.staged_version}")


def main() -> int:
    args = build_parser().parse_args()
    try:
        with BoseConnection(args.address) as speaker:
            if args.command == "rename":
                old_name = speaker.get_name()
                new_name = speaker.set_name(args.name)
                print(f"{old_name} -> {new_name}")
            elif args.command == "battery":
                print(f"{speaker.get_battery()}%")
            elif args.command == "voice-prompts":
                print_voice_prompts(speaker.get_voice_prompts())
            elif args.command == "firmware-status":
                print_firmware_status(speaker.get_firmware_update_status())
            elif args.command == "battery-prompt":
                requested = args.state == "on"
                result = speaker.set_battery_prompt(
                    requested, force_unsupported=args.force
                )
                print_voice_prompts(result)
                if result.battery_level_enabled != requested:
                    raise BoseError(
                        "Speaker rejected the requested battery-prompt state"
                    )
            else:
                print(f"Name: {speaker.get_name()}")
                print(f"Battery: {speaker.get_battery()}%")
                print(f"BMAP: {speaker.bmap_version} (RFCOMM channel {speaker.channel})")
    except (BoseError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
