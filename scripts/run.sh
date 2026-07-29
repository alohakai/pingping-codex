#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_dir=${script_dir:h}

cd "$project_dir"
exec "$project_dir/.venv/bin/python" -m feishu_codex
