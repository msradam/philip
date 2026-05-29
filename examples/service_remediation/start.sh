#!/usr/bin/env bash
# Run the target container with sshd on host:2222 and nginx (when started) on host:8080.

set -euo pipefail

NAME=ansiburr-target

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker rm -f "$NAME" >/dev/null
fi

docker run -d \
    --name "$NAME" \
    -p 2222:22 \
    -p 8080:80 \
    ansiburr-target:latest >/dev/null

echo "Waiting for sshd..."
for _ in {1..20}; do
    if nc -z localhost 2222 2>/dev/null; then
        echo "Container ready: ssh on localhost:2222, http on localhost:8080"
        exit 0
    fi
    sleep 0.5
done
echo "sshd did not come up in time" >&2
exit 1
