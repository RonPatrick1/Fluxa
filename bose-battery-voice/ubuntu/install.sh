#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
unit_path="$script_dir/bose-battery-voice.service"
cli_path="$script_dir/bose_battery_voice.py"
cli_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
cli_link="$cli_dir/bose-battery-voice"

mkdir -p -- "$cli_dir"
ln -sfn -- "$cli_path" "$cli_link"
systemctl --user link "$unit_path"
systemctl --user daemon-reload
systemctl --user enable --now bose-battery-voice.service
systemctl --user --no-pager status bose-battery-voice.service
echo "Ubuntu settings command: $cli_link settings"
