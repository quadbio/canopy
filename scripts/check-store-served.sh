#!/usr/bin/env bash
# Browser-library read check against a real `dbx serve`: writes the reference store into a
# temporary cache, starts the server, and runs frontend/scripts/read-store.mjs on its store URL.
set -euo pipefail
tmp=$(mktemp -d)
pid=""
cleanup() { [ -n "$pid" ] && kill "$pid" 2>/dev/null; rm -rf "$tmp"; }
trap cleanup EXIT

version=0123456789abcdef
dbx contract example "$tmp/cache/$version/c0" >/dev/null
mkdir "$tmp/data"
port=$(python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
cat > "$tmp/config.toml" <<CONF
cache_dir = "cache"
[[data_roots]]
name = "data"
path = "data"
[server]
port = $port
[logging]
dir = "logs"
memory_sample_interval = 0
CONF
dbx serve --config "$tmp/config.toml" >"$tmp/serve.out" 2>&1 &
pid=$!
for _ in $(seq 1 100); do
  curl -sf "http://127.0.0.1:$port/api/health" >/dev/null && break
  sleep 0.1
done
node frontend/scripts/read-store.mjs "http://127.0.0.1:$port/api/stores/$version/c0" || {
  echo "--- server output"; tail -20 "$tmp/serve.out"; exit 1;
}
