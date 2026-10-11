#!/usr/bin/env bash
# Server for the Playwright tests: `dbx serve` on the example datasets with a throw-away cache
# and log folder (printed on start). The example SpatialData archives must be extracted into
# .cache/examples (the backend tests do that).
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${DBX_E2E_PORT:-8791}
tmp=${DBX_E2E_DIR:-$repo/.cache/e2e}  # one reused folder, not a new temp folder per run
rm -rf "$tmp/cache" "$tmp/logs"
mkdir -p "$tmp"
generated="$repo/.cache/generated"
if [ ! -f "$generated/example_with_layers.h5ad" ] || [ "$repo/backend/dbx/synth/demo.py" -nt "$generated/example_with_layers.h5ad" ]; then
  mkdir -p "$generated"
  dbx synth demo "$repo/.cache/generated-part.h5ad" >/dev/null
  mv "$repo/.cache/generated-part.h5ad" "$generated/example_with_layers.h5ad"
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
[server]
port = $port
[logging]
dir = "logs"
memory_sample_interval = 0
CONF
echo "e2e server: logs in $tmp/logs" >&2
exec dbx serve --config "$tmp/config.toml"
