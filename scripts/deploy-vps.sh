#!/bin/sh
# F4 deploy gate: VPS deploys only from known trees, then proves it.
#
#   sh scripts/deploy-vps.sh            # clean tree required
#   sh scripts/deploy-vps.sh --allow-dirty   # stamps dirty=true, still visible
#
# Steps: dirt check -> pull main -> build (sha/dirty/utc args) ->
# up calculation+analysis -> smoke /health + /dispatch-ticket on both planes.
set -eu

ALLOW_DIRTY=0
for arg in "$@"; do
  case "$arg" in
    --allow-dirty) ALLOW_DIRTY=1 ;;
    -h|--help)
      echo "usage: sh scripts/deploy-vps.sh [--allow-dirty]"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

DIRTY="(unknown)"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    DIRTY="true"
  else
    DIRTY="false"
  fi
fi
if [ "$DIRTY" = "true" ] && [ "$ALLOW_DIRTY" -eq 0 ]; then
  echo "refusing: working tree is dirty (git status non-empty)." >&2
  echo "commit first, or re-run with --allow-dirty (stamps dirty=true)." >&2
  exit 1
fi

SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "deploy sha=$SHA dirty=$DIRTY built=$UTC"

git pull --ff-only origin main

export GIT_SHA="$SHA" GIT_DIRTY="$DIRTY" BUILD_UTC="$UTC"
docker compose --profile substrates --profile analysis build calculation analysis
docker compose --profile substrates --profile analysis up -d calculation analysis
sleep 8

fail=0
for plane in "calculation:8041" "analysis:8042"; do
  name="${plane%%:*}"; port="${plane##*:}"
  if ! curl -sf "http://127.0.0.1:${port}/health" >/dev/null; then
    echo "SMOKE FAIL: $name /health" >&2; fail=1; continue
  fi
  if curl -sf -X POST "http://127.0.0.1:${port}/dispatch-ticket" \
      -H 'Content-Type: application/json' \
      -d "{\"symbol\":\"SOLUSDT\"}" | grep -q '"batches"'; then
    echo "SMOKE OK: $name ticket shape good"
  else
    echo "SMOKE FAIL: $name /dispatch-ticket shape" >&2; fail=1
  fi
done

if [ "$fail" -ne 0 ]; then echo "DEPLOY BAD sha=$SHA" >&2; exit 1; fi
echo "DEPLOY_OK sha=$SHA dirty=$DIRTY built=$UTC"
