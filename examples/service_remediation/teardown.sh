#!/usr/bin/env bash
set -euo pipefail
docker rm -f ansiburr-target 2>/dev/null || true
echo "Container removed."
