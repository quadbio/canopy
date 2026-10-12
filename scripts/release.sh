#!/usr/bin/env bash
# A release archive for a host: the source at HEAD, the built frontend and the build record
# (backend/canopy/_build.json), so the host needs neither Node.js nor the npm registry.
#
#   pixi run -e dev release            -> dist-release/canopy-<version>-<commit>.tar.gz
#
# The working tree must be clean (the archive is exactly HEAD plus the built frontend).
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
if [ -n "$(git status --porcelain)" ]; then
  echo "release: the working tree has changes; commit them first" >&2
  exit 1
fi
commit=$(git rev-parse HEAD)
short=${commit:0:10}
version=$(python -c 'import canopy; print(canopy.__version__)')
name="canopy-$version-$short"
out="$repo/dist-release"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

(cd frontend && pnpm install --frozen-lockfile >/dev/null && pnpm build >/dev/null)
mkdir -p "$work/$name"
git archive HEAD | tar -x -C "$work/$name"
cp -R frontend/dist "$work/$name/frontend/dist"
python - "$work/$name/backend/canopy/_build.json" "$commit" <<'PY'
import json, sys, datetime
json.dump({"commit": sys.argv[2], "built": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")},
          open(sys.argv[1], "w"))
PY
mkdir -p "$out"
tar -C "$work" -czf "$out/$name.tar.gz" "$name"
( cd "$out" && shasum -a 256 "$name.tar.gz" > "$name.tar.gz.sha256" )
echo "wrote $out/$name.tar.gz ($(du -h "$out/$name.tar.gz" | cut -f1))"
echo "copy it with its .sha256 file; on the host follow docs/host/host-guide.md"
