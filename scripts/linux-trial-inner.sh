#!/usr/bin/env bash
# Inside the Linux trial container (scripts/linux-trial.sh), as the normal user "lab": the host
# guide's steps (docs/host/host-guide.md) on a fresh Ubuntu, each check recorded as PASS or
# FAIL in /out/summary.txt. The run goes on after a failed check where it can, so one trial
# shows every problem.
set -uo pipefail
umask 022 # a usual login default: Canopy must make its own files private regardless
export PIXI_HOME=/scratch/lab/pixi PIXI_CACHE_DIR=/pixi-cache
export PATH=$PIXI_HOME/bin:$PATH
summary=/out/summary.txt
: >"$summary"
fails=0
note() { echo "$*" | tee -a "$summary"; }
check() { # check "what" command...: PASS/FAIL with the command's last lines on failure
  local what=$1
  shift
  local t0=$SECONDS out
  if out=$("$@" 2>&1); then
    note "PASS  $what ($((SECONDS - t0)) s)"
  else
    fails=$((fails + 1))
    note "FAIL  $what ($((SECONDS - t0)) s)"
    echo "$out" | tail -25 | sed 's/^/      /' | tee -a "$summary"
  fi
  echo "$out" >>/out/details.log
}
api() { curl -s -o /dev/null -w "%{http_code}" "$@"; }
mode() { stat -c %a "$1"; }

note "Linux trial $(date -u +%FT%TZ) · $(uname -m) · $(. /etc/os-release && echo "$PRETTY_NAME") · user $(id -un)"
note "memory limit $(cat /sys/fs/cgroup/memory.max 2>/dev/null) bytes · cgroup $(cat /proc/self/cgroup)"

# --- host guide 2: pixi without root
check "install pixi (no root)" bash -c 'curl -fsSL https://pixi.sh/install.sh | bash'
check "pixi runs" pixi --version

# --- 3: unpack the release, checksum first
mkdir -p /scratch/lab/canopy && cd /scratch/lab/canopy || exit 1
cp /release/* .
check "checksum OK (sha256sum -c)" sha256sum -c ./*.tar.gz.sha256
tar -xzf ./*.tar.gz
ln -s /examples example_datasets # where the tests look for the example data
cd canopy-*/ || exit 1
note "release $(basename "$PWD")"

# --- 4: the runtime environment, exactly as locked
check "pixi install --locked (runtime, linux-64)" pixi install --locked
check "canopy --version" pixi run --frozen canopy --version

# --- the backend tests on Linux, with the example datasets required
check "pixi install --locked -e dev" pixi install --locked -e dev
check "backend tests on Linux (examples required)" env CANOPY_REQUIRE_EXAMPLES=1 pixi run --frozen -e dev pytest -q -p no:cacheprovider tests/backend

# --- 5: configure from the host example (paths, a data root, memory within the limit)
run=/scratch/lab/canopy/run
mkdir -p "$run"
sed -e "s#/scratch/YOUR_USER/canopy#$run#g" \
  -e 's#^name = "lab".*#name = "examples"#' \
  -e 's#^path = "/path/to/the/lab/share/projects".*#path = "/examples"#' \
  -e 's#^budget_gb = .*#budget_gb = 2.25#' -e 's#^cache_gb = .*#cache_gb = 1.5#' \
  -e 's#^memory_gb = .*#memory_gb = 0.5#' -e 's#^memory_sample_interval = .*#memory_sample_interval = 2#' \
  config.host.example.toml >config.toml
check "canopy link (config valid, prints the link)" bash -c 'pixi run --frozen canopy link --config config.toml | grep -q "token="'
pixi run --frozen canopy selfcheck --config config.toml --read-mb 64 >/out/selfcheck.txt 2>&1
check "canopy selfcheck: no failed check" bash -c 'tail -1 /out/selfcheck.txt | grep -q " 0 failed"'
check "canopy selfcheck: sees the 4 GB container limit" grep -q "account's limit 4 GB" /out/selfcheck.txt

# --- 6: start without a terminal (as under nohup or a service): no token in the output
pixi run --frozen canopy serve --config config.toml >serve.out 2>&1 &
for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:8765/api/health >/dev/null && break
  sleep 1
done
pid=$(cat "$run/state/server.lock" 2>/dev/null || true) # the server's own process id (as the guide says)
check "server.lock holds the process id" test -n "$pid"
check "server answers /api/health" curl -sf http://127.0.0.1:8765/api/health
check "no token in output that is not a terminal" bash -c '! grep -q "token=" serve.out && grep -q "canopy link" serve.out'
tok=$(cat "$run/state/access_token")
auth=(-H "Authorization: Bearer $tok")
check "a request without the token gets 401" test "$(api http://127.0.0.1:8765/api/catalog/roots)" = 401
check "a request with the token gets 200" test "$(api "${auth[@]}" http://127.0.0.1:8765/api/catalog/roots)" = 200
check "the page is served" bash -c "curl -s -H 'Authorization: Bearer $tok' http://127.0.0.1:8765/ | grep -q '<title>Canopy</title>'"

# --- private files whatever the umask (file_access = "private")
check "token file is 600" test "$(mode "$run/state/access_token")" = 600
check "state, cache and log folders are 700" bash -c "test \$(stat -c %a $run/state) = 700 && test \$(stat -c %a $run/cache) = 700 && test \$(stat -c %a $run/logs) = 700"
check "log files are 600" bash -c "for f in $run/logs/*.jsonl; do test \$(stat -c %a \$f) = 600 || { echo \$f \$(stat -c %a \$f); exit 1; }; done"

# --- memory: the container's limit is read, and a budget above it is warned about
sleep 3 # a memory sample or two
check "cgroup v2 limit read at start (cgroup_limit_gb ≈ 4)" bash -c "grep -ho '\"cgroup_limit_gb\": *[0-9.]*' $run/logs/canopy-server-*.jsonl | head -1 | grep -Eq ': *(3\.9|4\.[01]|4$)'"
grep -ho '"cgroup_[a-z_]*":[^,]*' "$run"/logs/canopy-server-*.jsonl | sort | uniq | head -8 | sed 's/^/      /' | tee -a "$summary"
check "no budget warning: budget 2.25 GB + a worker's overshoot (1.25 GB) under a 4 GB limit" bash -c "! grep -q memory_budget_above_limit $run/logs/canopy-server-*.jsonl"

# --- the data: the catalogue finds the examples; a dataset opens; a plot summary is computed
# the first scan may still run: ask again for up to 30 s
for _ in $(seq 1 30); do
  ids=$(curl -s "${auth[@]}" "http://127.0.0.1:8765/api/catalog/search?q=dissociated")
  case "$ids" in *'"id"'*) break ;; esac
  sleep 1
done
id=$(echo "$ids" | pixi run --frozen python -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])' 2>/dev/null)
check "catalogue lists dissociated.h5ad" test -n "$id"
check "dataset meta" test "$(api "${auth[@]}" "http://127.0.0.1:8765/api/datasets/$id/meta")" = 200
col=$(curl -s "${auth[@]}" "http://127.0.0.1:8765/api/datasets/$id/meta" | pixi run --frozen python -c 'import json,sys; m=json.load(sys.stdin); print(next(c["name"] for c in m["obs"] if c["kind"]=="categorical" and c["categories"] and len(c["categories"])<=100))' 2>/dev/null)
check "a categorical column ($col)" test "$(api "${auth[@]}" "http://127.0.0.1:8765/api/datasets/$id/obs?column=$col")" = 200
check "an embedding (X_umap)" test "$(api "${auth[@]}" "http://127.0.0.1:8765/api/datasets/$id/obsm?key=X_umap")" = 200
check "a gene (index 0)" test "$(api "${auth[@]}" "http://127.0.0.1:8765/api/datasets/$id/gene?i=0")" = 200
check "violin summary ($col × gene 0)" test "$(api "${auth[@]}" "http://127.0.0.1:8765/api/datasets/$id/summary?kind=violin&x=obs:$col&y=gene:0")" = 200

# --- a conversion in a worker process: it finishes; its store and files are private (umask)
job=$(curl -s "${auth[@]}" -H 'content-type: application/json' -d '{}' "http://127.0.0.1:8765/api/datasets/$id/convert" | pixi run --frozen python -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)
state=""
for _ in $(seq 1 180); do
  state=$(curl -s "${auth[@]}" "http://127.0.0.1:8765/api/jobs/$job" | pixi run --frozen python -c 'import json,sys; print(json.load(sys.stdin)["state"])' 2>/dev/null)
  case "$state" in done | failed | cancelled) break ;; esac
  sleep 1
done
check "a conversion finishes (job $job: $state)" test "$state" = done
check "the converted store is private (700/600)" bash -c "d=\$(ls -d $run/cache/*/c0 | head -1) && test \$(stat -c %a \$d) = 700 && test \$(stat -c %a \$d/manifest.json) = 600"
check "the member link may not cancel it" test "$(api -X POST -H 'content-type: application/json' -d '{}' -H "authorization: Bearer $(cat "$run/state/member_token" 2>/dev/null || pixi run --frozen canopy link --member --config config.toml | sed 's/.*token=//')" "http://127.0.0.1:8765/api/jobs/$job/cancel")" = 403

# --- 8: diagnostics: a bundle that passes its own check, and a hang dump
kill -USR1 "$pid"
sleep 2
check "SIGUSR1 writes the stacks" bash -c "test -s $run/logs/stacks-$pid.txt"
check "canopy bundle" pixi run --frozen canopy bundle --config config.toml --since-days 1 --out /out/bundle.tar.gz
check "bundle has the manifest and server log" bash -c 'tar -tzf /out/bundle.tar.gz | grep -q MANIFEST.md && tar -tzf /out/bundle.tar.gz | grep -q "logs/canopy-server"'
check "bundle file is 600" test "$(mode /out/bundle.tar.gz)" = 600

# --- stop: the server ends cleanly
kill "$pid"
for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
check "server stopped on SIGTERM" bash -c "test -n '$pid' && ! kill -0 $pid 2>/dev/null"
check "shutdown logged" bash -c "grep -q '\"shutdown\"' $run/logs/canopy-server-*.jsonl"

# --- a budget above the limit is said at start
sed -i 's#^budget_gb = .*#budget_gb = 8#; s#^cache_gb = .*#cache_gb = 1#; s#^port = .*#port = 8766#' config.toml
pixi run --frozen canopy serve --config config.toml >serve2.out 2>&1 &
for _ in $(seq 1 120); do curl -sf http://127.0.0.1:8766/api/health >/dev/null && break; sleep 1; done
pid2=$(cat "$run/state/server.lock")
check "budget 8 GB under a 4 GB limit is warned about" bash -c "grep -q memory_budget_above_limit $run/logs/canopy-server-*.jsonl"
check "canopy selfcheck fails on that budget" bash -c '! pixi run --frozen canopy selfcheck --config config.toml --read-mb 0 >/out/selfcheck2.txt 2>&1 && grep -q "FAIL  budget 8 GB" /out/selfcheck2.txt'
kill "$pid2"
for _ in $(seq 1 30); do kill -0 "$pid2" 2>/dev/null || break; sleep 1; done

cp serve.out serve2.out /out/ 2>/dev/null
note ""
note "$([ "$fails" = 0 ] && echo "ALL CHECKS PASSED" || echo "$fails CHECK(S) FAILED")"
exit "$fails"
