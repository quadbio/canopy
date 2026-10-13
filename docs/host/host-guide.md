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

A config problem is explained in one line (and recorded in the log folder). Put `state_dir` on a
**local** disk (`df -T <folder>` should not say nfs, gpfs, lustre, cifs).

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
   the host. Gene names, version numbers and names shorter than 4 characters are not searched
   for: read it. **Open it and read `MANIFEST.md` before copying it out.**

   If it refuses ("not written: a name known on this host was found …"), add `--show-names`
   to see which names matched, on your terminal only. Do not send the names. Tell the developer
   what kind of value matched (a version number, a column name, a word like `gene`) and in
   which file of the bundle.
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
