#!/usr/bin/env bash
# Verify the exact committed Dockerfile without cache on a bounded builder.
set -euo pipefail
revision=${1:-HEAD}
image=${2:-blast-radius:budget-check}
context=$(mktemp -d)
builder="br-budget-$$"
cleanup() {
    docker buildx rm "$builder" >/dev/null 2>&1 || true
    rm -rf "$context"
}
trap cleanup EXIT
git archive "$revision" | tar -x -C "$context"
size=$(du -sb --apparent-size "$context" | cut -f1)
if ((size > 20000000)); then
    echo "Checkout exceeds 20 MB ($size bytes)" >&2
    exit 1
fi
docker buildx create --name "$builder" --driver docker-container \
    --driver-opt memory=8g,memory-swap=8g,cpu-period=100000,cpu-quota=400000 >/dev/null
docker buildx inspect "$builder" --bootstrap >/dev/null
SECONDS=0
timeout --kill-after=5s 240s docker buildx build --builder "$builder" \
    --platform linux/amd64 --no-cache --progress plain --load -t "$image" "$context"
echo "Clean build and load took ${SECONDS}s for $revision ($size checkout bytes)"
