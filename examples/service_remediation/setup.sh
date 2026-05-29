#!/usr/bin/env bash
# Generate an SSH keypair for the demo and build the target image.
# Idempotent: existing key is reused, image is rebuilt on each run.

set -euo pipefail
cd "$(dirname "$0")"

KEY=.demo_key

if [[ ! -f $KEY ]]; then
    ssh-keygen -t ed25519 -N "" -C "ansiburr-demo" -f "$KEY" >/dev/null
    echo "Generated $KEY"
else
    echo "Reusing $KEY"
fi

docker build \
    --build-arg SSH_PUBLIC_KEY="$(cat ${KEY}.pub)" \
    -t ansiburr-target:latest \
    .
