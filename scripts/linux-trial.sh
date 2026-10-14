#!/usr/bin/env bash
# Linux trial of a release, on the Mac: a fresh Ubuntu 24.04 container (x86-64, as the host;
# emulated with Rosetta under Colima), a memory limit, a normal user. Inside, the host guide's
# steps run (scripts/linux-trial-inner.sh) and every check is recorded.
#
#   bash scripts/linux-trial.sh [dist-release/canopy-<version>-<commit>.tar.gz]
#
# Without an argument the newest archive in dist-release/ is used (make one with
# `pixi run -e dev release`). Results: .cache/linux-trial/<time>/ (summary.txt, the run's
# output, the bundle). Needs Docker (e.g. `colima start --vm-type vz --vz-rosetta`) and network
# access to conda-forge and PyPI. MEMORY (default 4g) is the container's memory limit.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
archive=${1:-$(ls -t "$repo"/dist-release/canopy-*.tar.gz 2>/dev/null | head -1)}
[ -n "$archive" ] && [ -f "$archive" ] || { echo "linux-trial: no release archive (pixi run -e dev release)" >&2; exit 1; }
archive=$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")
[ -f "$archive.sha256" ] || { echo "linux-trial: $archive.sha256 is missing" >&2; exit 1; }
examples="$repo/../example_datasets"
[ -d "$examples" ] || { echo "linux-trial: $examples is missing" >&2; exit 1; }
out="$repo/.cache/linux-trial/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$out"
echo "linux-trial: $(basename "$archive") -> $out"

# A volume keeps pixi's download cache between trials (the environment itself is new each time).
# (no -e here: a failed check must still print the summary below)
set +e
docker run --rm --platform linux/amd64 --memory "${MEMORY:-4g}" --cpus 4 \
  -v "$archive:/release/$(basename "$archive"):ro" \
  -v "$archive.sha256:/release/$(basename "$archive").sha256:ro" \
  -v "$examples:/examples:ro" \
  -v "$repo/scripts/linux-trial-inner.sh:/trial/inner.sh:ro" \
  -v "$out:/out" \
  -v canopy-trial-pixi-cache:/pixi-cache \
  ubuntu:24.04 bash -euo pipefail -c '
    apt-get update -qq && apt-get install -y -qq curl ca-certificates procps >/dev/null
    useradd -m -u 1000 lab 2>/dev/null || useradd -m lab
    mkdir -p /scratch/lab && chown lab /scratch/lab /out /pixi-cache
    runuser -u lab -- bash /trial/inner.sh
  ' 2>&1 | tee "$out/run.log"
status=${PIPESTATUS[0]}
echo
cat "$out/summary.txt" 2>/dev/null || echo "linux-trial: no summary (the container stopped early)"
# the trial's bundle as a report, as for a host bundle
if [ -f "$out/bundle.tar.gz" ]; then
  (cd "$repo" && pixi run -e dev canopy analyse "$out/bundle.tar.gz" --out "$out/report.md") || echo "linux-trial: analyse failed"
fi
exit "$status"
