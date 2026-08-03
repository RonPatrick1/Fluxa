#!/usr/bin/env bash
set -euo pipefail

unit_path="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/bose-battery-voice.service"
systemctl --user link "$unit_path"
systemctl --user daemon-reload
systemctl --user enable --now bose-battery-voice.service
systemctl --user --no-pager status bose-battery-voice.service
