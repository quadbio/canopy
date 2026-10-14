# Running Canopy on a shared Linux host

For the owner, installing and testing alone. Nothing here needs root. Commands are typed on the
host unless they say "on your laptop". Allow about an hour the first time.

**Who can see what.** Whoever opens Canopy's access link sees every dataset in the configured
data roots, with *your* Unix rights. Keep the link private, and keep the server on `127.0.0.1`
(reached through an SSH tunnel). Files Canopy writes (converted copies, index, logs) are
readable only by you (`file_access = "private"`).

> Tried by the developer on a fresh Ubuntu 24.04 (x86-64) as a normal user with a memory limit,
> following these steps (`scripts/linux-trial.sh`); a real shared host can still differ (proxy,
> network disks, limits on your user slice). If a step fails, run `canopy bundle` (section 8) if
> you can, and send the bundle plus the exact command and its last lines of output.

## 1. What you need

- Linux x86-64, a shell account, and about **4 GB** for the software environment (put it on a
  scratch or project disk if your home quota is small) plus room for converted copies.
- Internet access to **conda-forge** and **PyPI** from the host, directly or through the
  institute proxy. Without any internet see section 9.
- The release archive `canopy-<version>-<commit>.tar.gz` and its `.sha256` file (made on the Mac
  with `pixi run -e dev release`). It contains the built web page, so no Node.js is needed.

## 2. Install pixi (no root)

```bash
export PIXI_HOME=/scratch/$USER/pixi              # where pixi itself lives
export PIXI_CACHE_DIR=/scratch/$USER/pixi-cache   # downloaded packages (several GB)
# behind a proxy (ask IT for the address):
# export HTTPS_PROXY=http://proxy.example:3128 HTTP_PROXY=$HTTPS_PROXY
curl -fsSL https://pixi.sh/install.sh | bash
```

Open a new shell (or `source ~/.bashrc`) and check `pixi --version`. Put the `export` lines in
`~/.bashrc` so later shells have them.

## 3. Unpack the release

```bash
mkdir -p /scratch/$USER/canopy && cd /scratch/$USER/canopy
sha256sum -c canopy-*.tar.gz.sha256       # must say OK
tar -xzf canopy-*.tar.gz
cd canopy-*/
```

## 4. Install the environment

```bash
pixi install --locked          # the runtime only: no developer tools, no Node.js
pixi run --frozen canopy --version
```

`--locked` installs exactly the versions tested on the Mac (the lock file covers Linux too).
`--frozen` (used from now on) never tries to resolve or download again at start.

## 5. Configure

```bash
cp config.host.example.toml config.toml
nano config.toml               # edit the lines marked EDIT
pixi run --frozen canopy link --config config.toml   # checks the config; prints the link
```

**What is listed.** `.h5ad` files and unpacked `.zarr` folders in the data roots. A
`.zarr.tar.gz` archive is not read: unpack it next to itself first (`tar -xzf x.zarr.tar.gz`).
The roots are scanned at start and every hour; after adding files, click **Refresh** in the
file browser.

A config problem is explained in one line (and recorded in the log folder). Put `state_dir` on a
**local** disk (`df -T <folder>` should not say nfs, gpfs, lustre, cifs). It is small (the index
and the job table); on a network disk, its writes wait behind conversions reading from the same
disk (first host test: the job list stalled 1–2 s while a conversion started). The data and the
cache can stay on the network disk.

**Conversions.** On a large host, more can run at once and each can use more memory, so a short
conversion does not wait behind a long one (first host test: a 17 s conversion waited 48 min).
The memory budget holds the cache *and* the running conversions; each conversion reserves
1.5 × `memory_gb` plus what it reads whole (a few GB for a 2 M-cell file), and may grow
0.5 × `memory_gb` + 1 GB beyond that before it is stopped. So:

- `budget_gb` ≥ `cache_gb` + `max_running` × (1.5 × `memory_gb` + 4), and
- your account's memory limit ≥ `budget_gb` + `max_running` × (0.5 × `memory_gb` + 1).

For example `budget_gb = 128`, `cache_gb = 32`, `max_running = 3`, `memory_gb = 16`
(32 + 3 × 28 = 116 ≤ 128; the limit must allow 128 + 27 = 155 GB). Canopy warns at start
(`memory_budget_above_limit`) when the second line does not hold.

**Memory.** Your account may have a memory limit below the machine's memory (Linux "cgroups",
often set for all your sessions together). Look it up:

```bash
systemctl show user-$(id -u).slice -p MemoryMax 2>/dev/null   # the limit for all your sessions
cat /proc/self/cgroup                                          # where your shell sits
```

The first line prints `MemoryMax=infinity` when there is no limit. The second shows a path like
`0::/user.slice/user-1000.slice/session-3.scope`; a limit can sit on any part of it. Set
`memory.budget_gb` well below the smallest limit you find (e.g. 70 %). Canopy also reads the
limits itself (all parts of that path, the memory your other sessions use included) and logs
them at start and every minute (`cgroup_limit_gb`, `cgroup_headroom_gb`).

**For the first test, copy the output of both commands into your notes for the developer**
(it holds no data names: a user id number and a session number). It tells whether the limits
Canopy logs match what the host sets.

## 6. Start and stop

First check the installation (it changes nothing but creating the folders the config names):

```bash
pixi run --frozen canopy selfcheck --config config.toml
```

Each line says `ok`, `WARN` or `FAIL`, in plain words: absolute folders in the config (§11);
the cache, state and log folders writable, private to you, their file system and free space,
file locks where the index lives; the memory budget and conversions against the account's limit
(§5); each data root readable, with the speed of a test read (it reads up to 256 MB of one file
there; `--read-mb 0` skips that); the port; the index and unused converted copies (§13). Fix
what says `FAIL` before starting. Its results are logged as codes and numbers, so the next
bundle carries them.

Start it in a `tmux` session, so it keeps running after you log out:

```bash
chmod 700 .                              # this folder: only you can read it
tmux new -s canopy                       # a terminal that stays when you disconnect
pixi run --frozen canopy serve --config config.toml
```

The access link is printed in that terminal (only you can see it). Detach with `Ctrl-b d`;
reattach later with `tmux attach -t canopy`; stop the server there with `Ctrl-C`.

The server prints the link only to a terminal. Started any other way (output into a file, a
service's journal), it prints no token, because others may read those; print the link with
`pixi run --frozen canopy link --config config.toml` instead.

Optionally, as a user service, which also stops conversion workers with the server
(`KillMode=control-group`):

```bash
systemd-run --user --unit canopy --property KillMode=control-group \
  --working-directory "$PWD" "$PWD/.pixi/envs/default/bin/canopy" serve --config "$PWD/config.toml"
systemctl --user stop canopy                      # stop
```

A server started without a terminal stops with `kill $(cat <state_dir>/server.lock)` (the
server's process id is in that file).

## 7. Open it from your laptop

On your laptop:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@host.example     # keep this running
```

Then open the link from step 6 in Chrome, Firefox or Safari on the laptop (it starts with
`http://127.0.0.1:8765/?token=`). The browser remembers the token; the address bar drops it.
Two Canopy servers on the same laptop address (e.g. ports 8765 and 8766) each keep their own
cookie; an old link's cookie never blocks a new link.
`canopy link --config config.toml` prints the link again at any time.

## 8. When something goes wrong

1. Note the time and what you did.
2. Collect the diagnostics:

   ```bash
   pixi run --frozen canopy bundle --config config.toml --since-days 2
   ```

   This writes `canopy-bundle-<date>.tar.gz`: logs of the server, workers and browser at
   `strict` privacy whatever privacy the server ran with (names as tokens; free text, also
   libraries' own messages, dropped), crash locations, the environment and summaries of jobs and
   the index. It refuses to write if it finds a file, folder, column or element name known on
   the host. Gene names, bare version numbers (`0.8.0`) and names shorter than 4 characters are
   not searched for: read it. **Open it and read `MANIFEST.md` before copying it out.**

   If it refuses ("not written: a name known on this host was found …"), add `--show-names`
   to see which names matched, on your terminal only. Do not send the names. Tell the developer
   what kind of value matched (a version number, a column name, a word like `gene`) and in
   which file of the bundle. If a match is harmless (one of your names is also an ordinary word
   the bundle uses), you may let it through after reading where it appears:
   `--accept-name NAME` (repeatable); `MANIFEST.md` then says how many names were let through,
   never which.
3. A page that hangs: `kill -USR1 $(cat <state_dir>/server.lock)` writes the server's stacks to
   the log folder; then make the bundle.
4. The developer may name tokens like `h_fb26d42600`. On the host,
   `canopy token find h_fb26d42600 --config config.toml` tells you which file or column it is;
   answer only what the data policy allows.

## 9. A host without internet

On a machine with internet, in the unpacked release:

```bash
pixi exec pixi-pack --platform linux-64 --environment default   # writes environment.tar
```

Copy `environment.tar` with the release to the host, then there:

```bash
pixi exec pixi-unpack environment.tar        # or: the pixi-unpack binary from its releases page
source activate.sh
canopy --version
```

Use `canopy …` directly instead of `pixi run --frozen canopy …` in the steps above.

## 10. First test: checklist (tick and send back with the bundle)

- [ ] Release checksum OK; `pixi install --locked` finished (time: ___ min)
- [ ] `canopy link` accepted the config; `state_dir` on a local disk (file system: ___)
- [ ] Account memory limit: ___ GB; `budget_gb` set to ___
- [ ] Server started; the link opens the file browser through the SSH tunnel
- [ ] Example datasets (copied from the Mac) open: embedding, gene colouring, spatial view
- [ ] One conversion of an example finished
- [ ] Browsing a real share folder lists datasets (no data opened yet, unless the policy allows)
- [ ] `canopy bundle` written, `MANIFEST.md` read, no names seen in it
- [ ] Problems seen (time, what, what was on screen): ___

## 11. Moving to a new release

The state (access token, index), cache and logs live in the folders named in `config.toml`, not
in the release folder, so a new release keeps them, and the link stays the same: **as long as
`cache_dir`, `state_dir` and `[logging] dir` are absolute paths** (the template's are). A
relative path is resolved against the config's own folder, so in the new release folder it
would start empty: a new access link, an empty index, every conversion again. Check before you
start the new release:

```bash
grep -E '^(cache_dir|state_dir|dir) ' canopy-<new>/config.toml   # each must start with /
```

```bash
cd /scratch/$USER/canopy
sha256sum -c canopy-<new>.tar.gz.sha256          # must say OK
tar -xzf canopy-<new>.tar.gz
cp canopy-<old>/config.toml canopy-<new>/        # relative paths in it resolve against its folder
cd canopy-<new>/ && chmod 700 .
pixi install --locked
pixi run --frozen canopy --version               # shows the new commit
```

Stop the old server (`Ctrl-C` in its tmux window), then start the new one there, as in section
6. Started as a user service instead: `systemctl --user stop canopy`, then the `systemd-run`
line of section 6 again **from the new folder** (it names the folder and the program).

Keep the old folder until the new one works; going back is starting the old one again. That
holds while the new release has not converted anything in a newer format: a release note says
so when a release changes the format of converted copies (then the old release ignores them and
converts again).

## 12. Third test: checklist (linked views, lasso, bundle)

Install the new release as in section 11 (check the absolute paths first). Note the times of
what you do, then make the bundle (section 8) and send it with this list.

- [ ] Upgrade: the access link stayed the same; datasets listed as before (no new indexing)
- [ ] A dataset already in the index (e.g. a MERSCOPE region) converts (*Convert for full
      speed*); a conversion that fails: the message ___
- [ ] Linked spatial views: tick ___ regions (as many as you would use, up to 20) → *Open
      linked spatial views*; time until all show cells: ___ s; panning smooth? ___
- [ ] The same group with an image shown in each region vs without (Image: none): difference? ___
- [ ] Frames on the large external display (2560 × 1440): pan and zoom the 1.7 M-cell dataset
      for a minute: smooth? ___
- [ ] Lasso on the 1.7 M-cell dataset: draw around a cluster; *Plot against: Lasso selection*;
      time until the plot shows: ___ s
- [ ] `canopy bundle` written without `--accept-name`? If not: what kind of value matched, in
      which file (not the name): ___
- [ ] Anything that looked wrong or was slow (time, what, what was on screen): ___

## 13. Cleaning up old converted copies

A converted copy belongs to one version of a dataset. When a dataset changes, or is **moved or
renamed** (its path is part of its version), the next scan indexes it as a new version, and it
is converted again when you ask; the copy of the old version stays in the cache folder, unused.
So do the leftovers of conversions that failed or were stopped. `canopy cache prune` removes
both: every converted copy whose version the index no longer knows, and conversion leftovers.

1. Let the server see the change first: wait for the hourly rescan (`[catalog] rescan_minutes`),
   or press *Refresh* at the top of the page, so that the index has the new versions.
2. Stop the server (`Ctrl-C` in its tmux window, or `systemctl --user stop canopy`): the command
   refuses while a server uses the cache.
3. See what would go, with sizes, then remove it. Always give `--config config.toml`: without
   it the command refuses (older releases looked at an empty `cache` folder in the current
   directory and said "would free 0.00 GB"). The dry run first names the cache folder and the
   index it uses, then lists every converted copy: `keep` with the dataset that uses it, or
   `would remove`. The dataset names are shown on your terminal only.

   ```bash
   cd /scratch/$USER/canopy/canopy-<release>
   pixi run --frozen canopy cache prune --config config.toml --dry-run
   pixi run --frozen canopy cache prune --config config.toml
   ```

4. Start the server again (section 6).

Only copies of versions the index no longer has are removed: a dataset that is still where it
was keeps its copy. A data root removed from `config.toml` takes its datasets out of the index at
the next scan, so their copies go too; put the root back before pruning if you want to keep them.
