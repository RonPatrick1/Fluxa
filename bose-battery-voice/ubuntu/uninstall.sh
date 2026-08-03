#!/usr/bin/env bash
set -euo pipefail

systemctl --user disable --now bose-battery-voice.service || true
systemctl --user disable bose-battery-voice.service || true
systemctl --user daemon-reload
