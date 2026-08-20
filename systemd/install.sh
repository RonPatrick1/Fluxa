#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
unit_path="$script_dir/fluxa.service"
project_dir="$(cd -- "$script_dir/.." && pwd)"

cmake -S "$project_dir" -B "$project_dir/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$project_dir/build" --parallel

systemctl --user link "$unit_path"
systemctl --user daemon-reload
systemctl --user enable --now fluxa.service
systemctl --user --no-pager status fluxa.service
