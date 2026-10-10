#!/usr/bin/env bash
# Start the demo on this Mac: builds the frontend, unpacks the example SpatialData archives
# (once), and runs `dbx serve` on the example datasets. Stop with Ctrl+C.
#   URL:   http://127.0.0.1:${DBX_DEMO_PORT:-8765}
#   Logs:  .cache/demo/logs/        Index + cache: .cache/demo/cache/
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${DBX_DEMO_PORT:-8765}
demo="$repo/.cache/demo"
examples="$repo/../example_datasets"
extracted="$repo/.cache/examples"
mkdir -p "$demo" "$extracted"

if [ ! -d "$examples" ]; then
  echo "The example datasets are missing: expected $examples" >&2
  exit 1
fi
if curl -s -o /dev/null "http://127.0.0.1:$port/api/health"; then
  echo "Port $port is already in use (another dbx serve?). Stop it, or run: DBX_DEMO_PORT=8766 pixi run -e dev demo" >&2
  exit 1
fi

echo "1/3 Unpacking the example SpatialData archives (first run only)…"
python - "$examples" "$extracted" <<'PY'
import sys, tarfile
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
for archive in sorted(src.glob("*.zarr.tar.gz")):
    target = dst / archive.name.removesuffix(".tar.gz")
    if target.exists():
        continue
    print(f"    {archive.name}")
    with tarfile.open(archive) as tar:
        tar.extractall(dst, filter="data")
PY

echo "2/3 Building the frontend…"
(cd "$repo/frontend" && pnpm build >/dev/null)

cat > "$demo/config.toml" <<CONF
# Written by scripts/demo.sh
cache_dir = "cache"

[[data_roots]]
name = "examples"
path = "$examples"

[[data_roots]]
name = "extracted"
path = "$extracted"

[server]
host = "127.0.0.1"
port = $port

[logging]
dir = "logs"
level = "info"
privacy = "standard"
memory_sample_interval = 30
CONF

echo "3/3 Starting the server."
echo
echo "    Open      http://127.0.0.1:$port   (Chrome, Firefox or Safari)"
echo "    Logs      $demo/logs/"
echo "    Version   $(dbx --version)"
echo "    Stop      Ctrl+C"
echo
exec dbx serve --config "$demo/config.toml"
