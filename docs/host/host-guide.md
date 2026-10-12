# Running Canopy on a shared Linux host

For the owner, installing and testing alone. Nothing here needs root. Commands are typed on the
host unless they say "on your laptop". Allow about an hour the first time.

**Who can see what.** Whoever opens Canopy's access link sees every dataset in the configured
data roots, with *your* Unix rights. Keep the link private, and keep the server on `127.0.0.1`
(reached through an SSH tunnel). Files Canopy writes (converted copies, index, logs) are
readable only by you (`file_access = "private"`).

> Not yet tried on Linux by the developer: the steps follow the tools' documentation. If a step
> fails, run `canopy bundle` (section 8) if you can, and send the bundle plus the exact command
> and its last lines of output.

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

A config problem is explained in one line (and recorded in the log folder). Put `state_dir` on a
**local** disk (`df -T <folder>` should not say nfs, gpfs, lustre, cifs).

**Memory.** Your account may have a limit below the machine's memory. Look it up:

```bash
systemctl show user-$(id -u).slice -p MemoryMax 2>/dev/null
cat /sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)/memory.max 2>/dev/null
```

Set `memory.budget_gb` well below that limit (e.g. 70 %). Canopy also reads the limit itself
and logs it at start (`cgroup_limit_gb`).

## 6. Start and stop

Start in the background (the output file holds the access link: it is readable only by you):

```bash
nohup pixi run --frozen canopy serve --config config.toml > canopy.out 2>&1 &
grep -m1 token= canopy.out          # the access link
```

Or as a user service, which also stops conversion workers with the server
(`KillMode=control-group`):

```bash
systemd-run --user --unit canopy --property KillMode=control-group \
  --working-directory "$PWD" "$PWD/.pixi/envs/default/bin/canopy" serve --config "$PWD/config.toml"
journalctl --user -u canopy | grep -m1 token=   # the access link
systemctl --user stop canopy                      # stop
```

Stop a `nohup` server with `kill $(cat <state_dir>/server.lock)` (the server's process id is in
that file).

## 7. Open it from your laptop

On your laptop:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@host.example     # keep this running
```

Then open the link from step 6 in Chrome, Firefox or Safari on the laptop (it starts with
`http://127.0.0.1:8765/?token=`). The browser remembers the token; the address bar drops it.
`canopy link --config config.toml` prints the link again at any time.

## 8. When something goes wrong

1. Note the time and what you did.
2. Collect the diagnostics:

   ```bash
   pixi run --frozen canopy bundle --config config.toml --since-days 2
   ```

   This writes `canopy-bundle-<date>.tar.gz`: logs of the server, workers and browser at
   `strict` privacy (names as tokens, free text dropped), crash locations, the environment and
   summaries of jobs and the index. It refuses to write if it finds a name known on the host.
   **Open it and read `MANIFEST.md` before copying it out.**
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
