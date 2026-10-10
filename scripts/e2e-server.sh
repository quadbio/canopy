#!/usr/bin/env bash
# Server for the Playwright tests: `dbx serve` on the example datasets with a throw-away cache
# and log folder (printed on start). The example SpatialData archives must be extracted into
# .cache/examples (the backend tests do that).
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${DBX_E2E_PORT:-8791}
tmp=${DBX_E2E_DIR:-$(mktemp -d)}
mkdir -p "$tmp"
cat > "$tmp/config.toml" <<CONF
cache_dir = "cache"
[[data_roots]]
name = "examples"
path = "$repo/../example_datasets"
[[data_roots]]
name = "extracted"
path = "$repo/.cache/examples"
[server]
port = $port
[logging]
dir = "logs"
memory_sample_interval = 0
CONF
echo "e2e server: logs in $tmp/logs" >&2
exec dbx serve --config "$tmp/config.toml"
