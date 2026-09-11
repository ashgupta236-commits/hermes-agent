#!/usr/bin/env bash
# Build the isolated verifier runtime. Idempotent; safe to run on a clean checkout.
#
# In an environment whose egress goes through a TLS-inspecting proxy, point COGOS_PROXY_CA at that
# proxy's CA bundle so pip can verify PyPI. On a normal network, leave it unset.
#   COGOS_PROXY_CA=/root/.ccr/ca-bundle.crt cogos/verification/runtime/build.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TAG="${COGOS_VERIFIER_IMAGE:-cogos-verifier:1}"
CA="${COGOS_PROXY_CA:-}"

cleanup() { rm -f "$HERE/proxy-ca.crt"; }
trap cleanup EXIT
if [ -n "$CA" ] && [ -f "$CA" ]; then
  cp "$CA" "$HERE/proxy-ca.crt"
  echo "using proxy CA from $CA"
fi

docker build -t "$TAG" "$HERE"
echo "image id: $(docker image inspect "$TAG" --format '{{.Id}}')"
docker run --rm --network none --user 65534:65534 --read-only --cap-drop ALL \
  --tmpfs /scratch:rw,size=8m -e TMPDIR=/scratch -e HOME=/scratch "$TAG" \
  python -m pytest --version
