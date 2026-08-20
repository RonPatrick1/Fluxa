#!/usr/bin/env bash
set -euo pipefail

systemctl --user disable --now fluxa.service || true
systemctl --user daemon-reload
