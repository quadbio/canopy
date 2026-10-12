#!/usr/bin/env bash
# Linux smoke test of a release, in a throw-away container (Docker or Podman):
#   bash scripts/linux-smoke.sh dist-release/canopy-<version>-<commit>.tar.gz
# Installs the locked runtime environment for linux-64, runs the backend checks against it and
# starts the server once (health, access token, page). Needs network access to conda-forge and
# PyPI from the container.
set -euo pipefail
archive=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
engine=$(command -v docker || command -v podman || true)
[ -n "$engine" ] || { echo "linux-smoke: Docker or Podman is needed" >&2; exit 1; }
"$engine" run --rm -v "$archive:/release.tar.gz:ro" ubuntu:24.04 bash -euo pipefail -c '
  apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null
  curl -fsSL https://pixi.sh/install.sh | bash >/dev/null
  export PATH=$HOME/.pixi/bin:$PATH
  mkdir /w && cd /w && tar -xzf /release.tar.gz && cd canopy-*
  pixi install --locked
  pixi run --frozen canopy --version
  pixi install --locked -e dev >/dev/null
  pixi run --frozen -e dev pytest -q tests/backend
  mkdir -p run && printf "cache_dir = \"run/cache\"\nstate_dir = \"run/state\"\n[logging]\ndir = \"run/logs\"\n[server]\nport = 8765\n" > c.toml
  (pixi run --frozen canopy serve --config c.toml > serve.out 2>&1 &)
  for i in $(seq 1 60); do curl -sf http://127.0.0.1:8765/api/health >/dev/null && break; sleep 1; done
  test "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/api/catalog/roots)" = 401
  tok=$(cat run/state/access_token)
  test "$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $tok" http://127.0.0.1:8765/api/catalog/roots)" = 200
  curl -s -b "canopy_token=$tok" http://127.0.0.1:8765/ | grep -q "<title>Canopy</title>"
  pixi run --frozen canopy bundle --config c.toml --out run/b.tar.gz
  echo "linux-smoke: OK"
'
