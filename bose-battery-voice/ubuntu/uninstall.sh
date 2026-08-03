#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cli_path="$script_dir/bose_battery_voice.py"
cli_link="${XDG_BIN_HOME:-$HOME/.local/bin}/bose-battery-voice"

systemctl --user disable --now bose-battery-voice.service || true
systemctl --user disable bose-battery-voice.service || true
systemctl --user daemon-reload
if [[ -L "$cli_link" && "$(readlink -f -- "$cli_link")" == "$cli_path" ]]; then
    rm -- "$cli_link"
fi
