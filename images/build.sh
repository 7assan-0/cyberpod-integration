#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KALI_TAG="${CYBERPOD_KALI_IMAGE:-cyberpod/kali-desktop:dev}"
TARGET_TAG="${CYBERPOD_TARGET_IMAGE:-cyberpod/hydra-target:dev}"

if ! command -v docker >/dev/null 2>&1; then
  echo "BLOCKED: docker is not installed on this host" >&2
  echo "Static image definitions are under images/; live OCI builds need a Linux Docker worker." >&2
  exit 77
fi

docker build -t "$TARGET_TAG" "$ROOT/images/hydra-target"
docker build -t "$KALI_TAG" "$ROOT/images/kali-desktop"
echo "BUILT $TARGET_TAG"
echo "BUILT $KALI_TAG"
