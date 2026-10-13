#!/usr/bin/env bash
# Server for the Playwright tests: `canopy serve` on the example datasets with a throw-away cache
# and log folder (printed on start). The example SpatialData archives must be extracted into
# .cache/examples (the backend tests do that).
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${CANOPY_E2E_PORT:-8791}
tmp=${CANOPY_E2E_DIR:-$repo/.cache/e2e}  # one reused folder, not a new temp folder per run
rm -rf "$tmp/cache" "$tmp/logs"
mkdir -p "$tmp"
generated="$repo/.cache/generated"
if [ ! -f "$generated/example_with_layers.h5ad" ] || [ "$repo/backend/canopy/synth/demo.py" -nt "$generated/example_with_layers.h5ad" ]; then
  mkdir -p "$generated"
  part="$(mktemp "$generated/.example-XXXXXX")"  # own name: demo and e2e may run at once
  canopy synth demo "$part" >/dev/null && mv "$part" "$generated/example_with_layers.h5ad"
fi
cat > "$tmp/config.toml" <<CONF
cache_dir = "cache"
[[data_roots]]
name = "examples"
path = "$repo/../example_datasets"
[[data_roots]]
name = "extracted"
path = "$repo/.cache/examples"
[[data_roots]]
name = "generated"
path = "$generated"
$( [ "${CANOPY_E2E_LARGE:-0}" = "1" ] && printf '[[data_roots]]\nname = "synthetic"\npath = "%s"\n' "$repo/.cache/demo-large" )
[server]
port = $port
[logging]
dir = "logs"
memory_sample_interval = 0
[jobs]
memory_gb = 1.0  # the example conversions need well under 1 GB; keeps the memory guard satisfied
CONF
echo "e2e server: logs in $tmp/logs" >&2
# a fixed access token, which playwright.config.ts gives the browser as a cookie
export CANOPY_TOKEN=${CANOPY_TOKEN:-e2e-token}
exec canopy serve --config "$tmp/config.toml"
