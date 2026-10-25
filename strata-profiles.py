#!/usr/bin/env python3
"""Run Strata models through `strata` profiles, with your own settings kept in a patch file.

Setup and updates rewrite the run configs in the Strata folder, so settings put
there by hand get lost. This tool keeps them in ~/.config/strata/patches.json
and writes a patched copy of each config to ~/.config/strata/configs/<id>.json;
the profile omarchy-<id> points at that copy and is what `strata runtime` starts.
A profile of that name that points anywhere else is the person's own: it is left alone.

  strata-profiles.py info          JSON snapshot for the bar plugin
  strata-profiles.py details       JSON: strata version, size and memory split per model
  strata-profiles.py status        JSON telemetry snapshot of the running server (`strata status`) plus "cpu"
  strata-profiles.py apply [ID..]  rebuild patched configs and profiles (all by default)
  strata-profiles.py start ID      stop what runs, apply ID, start it and wait until ready
  strata-profiles.py stop          stop every running profile
  strata-profiles.py edit          create the patch file if missing and print its path
  strata-profiles.py update-check  is there a newer Strata? ($STRATA_UPDATE_CHECK, else `strata update --check`)

STRATA_CLI picks the strata command (default `strata`), e.g. a source checkout:
  STRATA_CLI="env STRATA_PYTHON=/path/to/Strata/.venv/bin/python /path/to/Strata/bin/strata"

patches.json maps comma-separated globs on the model id to a patch; matching
patches apply in file order, so put "*" first and specific ids after it:

  {
    "*":       {"profile": {"port": 8095}, "env": {"STRATA_HIP_PROMPT_F16": "1"}},
    "ornith-*": {"args": {"--kv": "q8_0", "--mtp": "1"},
                 "set": {"sampling": {"temperature": 0.6}}}
  }

  args     "--flag": "value" sets or appends it, true adds a bare flag, null removes it
  env      merged into the config's env; null removes a variable
  set      replaces top-level config keys; null removes the key
  profile  serving overrides for `strata profile set` (port, parallel, host, ...)
"""
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "strata"
PATCHES = CONF / "patches.json"
CONFIGS = CONF / "configs"
PROFILES = CONF / "profiles"
# `strata runtime` keeps a record per live instance here; profiles without one are stopped, so polling skips them
RUNTIME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "strata" / "runtime"
os.environ["PATH"] = f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}"
# the strata CLI to use; its own copy of Strata is what `runtime start` serves from
STRATA = shlex.split(os.environ.get("STRATA_CLI") or "strata")
# our profiles carry this prefix, so a profile the person made and named like a model is never touched
PREFIX = "omarchy-"


def profile_name(mid):
    """omarchy-<model id>, in the characters `strata profile` allows ([a-z][a-z0-9_-]{0,63})."""
    return (PREFIX + re.sub(r"[^a-z0-9_-]", "-", mid.lower()))[:64]


def ours(profile, config):
    """Whether an existing profile is one this tool made: it points into our configs folder."""
    return Path(profile.get("source_config", "")).parent == config.parent


def strata(*args, check=True):
    try:
        p = subprocess.run([*STRATA, *args], capture_output=True, text=True)
    except FileNotFoundError:
        p = subprocess.CompletedProcess(STRATA, 127, "", f"{STRATA[0]}: command not found (setting strataCommand)")
    if check and p.returncode != 0:
        sys.exit(f"strata {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    return p


def strata_json(*args):
    out = strata(*args, "--json", check=False).stdout
    try:
        return json.loads(out)
    except ValueError:
        return {}


def models():
    return strata_json("models", "list", "--installed").get("models", [])


def models_or_error():
    """(models, None), or ([], why) when the strata command does not answer: for the plugin to show."""
    p = strata("models", "list", "--installed", "--json", check=False)
    try:
        return json.loads(p.stdout).get("models", []), None
    except ValueError:
        lines = (p.stderr or p.stdout).strip().splitlines()
        return [], lines[-1] if lines else f"no answer from: {' '.join(STRATA)} models list"


def load_patches():
    if not PATCHES.exists():
        return {}
    try:
        return json.loads(PATCHES.read_text())
    except ValueError as e:
        sys.exit(f"{PATCHES}: {e}")


def set_flag(args, flag, value):
    i = args.index(flag) if flag in args else -1
    has_value = i >= 0 and i + 1 < len(args) and not args[i + 1].startswith("--")
    if value is None or value is False:
        if i >= 0:
            del args[i:i + (2 if has_value else 1)]
    elif value is True:
        if i < 0:
            args.append(flag)
    elif i < 0:
        args += [flag, str(value)]
    elif has_value:
        args[i + 1] = str(value)
    else:
        args.insert(i + 1, str(value))


def patch_config(cfg, patch):
    for key in ("args", "args_written"):  # setup keeps both lists; patch them alike
        if isinstance(cfg.get(key), list):
            for flag, value in patch.get("args", {}).items():
                set_flag(cfg[key], flag, value)
    # Strata's own hand-set options (strata_control/user_args.py) go over "args" at every start: a flag there would
    # undo the patch, so it is changed there too
    if isinstance(cfg.get("user_args"), list):
        for flag, value in patch.get("args", {}).items():
            if flag in cfg["user_args"]:
                set_flag(cfg["user_args"], flag, value)
    if patch.get("env"):
        env = cfg.setdefault("env", {})
        for k, v in patch["env"].items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = str(v)
    for k, v in patch.get("set", {}).items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v


def matching(patches, model_id):
    return [p for globs, p in patches.items()
            if any(fnmatch.fnmatch(model_id, g.strip()) for g in globs.split(","))]


def apply(ids=None):
    """Rebuild the patched configs and our profiles; returns the ids whose profile is ours to start."""
    patches = load_patches()
    ready = set()
    CONFIGS.mkdir(parents=True, exist_ok=True)
    for m in models():
        mid = m["id"]
        if ids and mid not in ids:
            continue
        cfg = json.loads(Path(m["source_config"]).read_text())
        overrides = {}
        for p in matching(patches, mid):
            patch_config(cfg, p)
            overrides.update(p.get("profile", {}))
        out = CONFIGS / f"{mid}.json"
        new = json.dumps(cfg, indent=1) + "\n"
        changed = not out.exists() or out.read_text() != new
        if changed:
            out.write_text(new)

        name = profile_name(mid)
        prof = PROFILES / f"{name}.json"
        cur = json.loads(prof.read_text()) if prof.exists() else None
        if cur is not None and not ours(cur, out):
            print(f"{mid}: profile {name} points at {cur.get('source_config')}, not at this tool's copy: "
                  f"left alone (rename or remove that profile to use {mid} here)")
            continue
        if cur is None:
            strata("profile", "create", name, "--from-config", str(out), "--quiet")
        elif cur.get("source_config") != str(out):
            strata("profile", "use-config", name, "--from-config", str(out), "--quiet")
        want = {k: v for k, v in overrides.items() if (cur or {}).get("overrides", {}).get(k) != v}
        if want:
            strata("profile", "set", name, "--quiet",
                   *(f"{k}={json.dumps(v) if isinstance(v, bool) else v}" for k, v in want.items()))
        ready.add(mid)
        print(f"{mid}: {'patched' if changed or want else 'unchanged'} ({len(matching(patches, mid))} patches)")
    return ready


def status(mid):
    name = profile_name(mid)
    if not (PROFILES / f"{name}.json").exists() or not (RUNTIME / f"{name}.json").exists():
        return {}
    return strata_json("runtime", "status", "--profile", name)


def state(mid):
    return status(mid).get("state", "stopped")


def stop():
    for m in models():
        if state(m["id"]) != "stopped":
            print(f"stopping {m['id']} ...", flush=True)
            strata("runtime", "stop", "--profile", profile_name(m["id"]), "--wait", "60")


def start(mid):
    if mid not in {m["id"] for m in models()}:
        sys.exit(f"unknown model {mid}; known: {' '.join(m['id'] for m in models())}")
    if mid not in apply([mid]):
        sys.exit(f"{mid} not started: its profile {profile_name(mid)} is not this tool's (see above)")
    stop()
    print(f"starting {mid}, loading can take a few minutes ...", flush=True)
    p = subprocess.run([*STRATA, "runtime", "start", "--profile", profile_name(mid), "--wait", "600"])
    sys.exit(p.returncode)


def measured(root_pid):
    """VRAM and RAM in GB that the server process and its children (the engine, the picture reader) use now.
    VRAM: ROCm's per-process counters (/sys/class/kfd), else nvidia-smi; None when neither answers."""
    pids, parent = {int(root_pid)}, {}
    for st in Path("/proc").glob("[0-9]*/stat"):
        try:
            f = st.read_text().rsplit(")", 1)[1].split()
            parent[int(st.parent.name)] = int(f[1])
        except (OSError, IndexError, ValueError):
            pass
    changed = True
    while changed:   # every descendant of root_pid
        new = {p for p, pp in parent.items() if pp in pids} - pids
        pids |= new
        changed = bool(new)
    ram = 0
    for p in pids:
        try:
            ram += next(int(l.split()[1]) for l in open(f"/proc/{p}/status") if l.startswith("VmRSS:")) * 1024
        except (OSError, StopIteration):
            pass
    vram = None
    kfd = Path("/sys/class/kfd/kfd/proc")
    if kfd.is_dir():
        vram = sum(int(f.read_text()) for p in pids for f in (kfd / str(p)).glob("vram_*")) or None
    if vram is None:
        try:
            out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=3).stdout
            vram = sum(int(m) for pid, m in (l.split(",") for l in out.splitlines() if l.strip()) if int(pid) in pids) * 2**20 or None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return {"vram_used_gb": round(vram / 2**30, 1) if vram else None, "ram_used_gb": round(ram / 2**30, 1)}


def info():
    found, error = models_or_error()
    out = {"patches": str(PATCHES), "models": [], "error": error}
    for m in found:
        mid = m["id"]
        st = status(mid)
        entry = {"id": mid, "name": m.get("name", mid), "context": m.get("context_tokens"),
                 "status": m.get("component_status", ""), "state": st.get("state", "stopped"), "url": st.get("api_url", ""),
                 "log": str(RUNTIME / f"{profile_name(mid)}.server.log")}
        if st.get("child_pid") and entry["state"] != "stopped":
            entry.update(measured(st["child_pid"]))
        out["models"].append(entry)
    print(json.dumps(out))


def size_gb(path):
    p = Path(path)
    if p.is_file():
        return p.stat().st_size / 2**30
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 2**30


def usable_vram(model, vram, dense_gb):
    """The card as the model's own headroom settings leave it, for Strata's fit."""
    cfg = CONFIGS / f"{model['id']}.json"
    try:
        args = json.loads((cfg if cfg.exists() else Path(model["source_config"])).read_text()).get("args", [])
    except (OSError, ValueError):
        return vram
    def flag(name):
        try:
            return float(args[args.index(name) + 1])
        except (ValueError, IndexError):
            return None
    reserve, cap = flag("--vram-reserve-mib"), flag("--expert-vram")
    if reserve is not None:
        vram = max(0.0, vram - reserve / 1024)
    if cap is not None:
        vram = min(vram, dense_gb + cap)
    return vram


def details():
    """Slower than info, for when the popup opens: version, and per model its size and where it sits in memory.

    The memory split is Strata's own: setup's catalog through `strata launch`'s fit (strata_cli/launch/catalog.py),
    read from the install folder's sources. A model the catalog does not know gets the same formula on the size
    of its GGUF weights, marked as an estimate. Both count a 32K cache; longer contexts need more.
    The headroom set in the config the profile starts (the patched copy) counts too: --vram-reserve-mib makes the
    card smaller, --expert-vram caps the experts' share (the q36 engine fills the card up to it).
    """
    listing = strata_json("models", "list", "--installed")
    version = strata("--version", check=False).stdout.split("(")[0].strip()
    root = listing.get("install_dir", "")
    catalog, rows, vram, ram = None, {}, 0.0, None
    try:
        sys.path.insert(0, root)
        from strata_cli.launch import catalog
        cat = catalog.Catalog(src=root)
        hw = cat.hardware()
        ram, vram = hw.get("ram_gb"), cat.computer().main_vram_gb
        rows = {r.id: r for r in cat.rows(hw) if r.installed}
    except Exception:
        pass  # no catalog in this install: sizes only

    out = {"version": version, "install_dir": root, "ram_gb": ram, "vram_gb": vram, "models": {}}
    for m in listing.get("models", []):
        comps = strata_json("models", "inspect", m["id"]).get("components", {}).get("components", [])
        paths = {c["path"] for c in comps if c.get("path") and Path(c["path"]).exists()}
        weights = sum(size_gb(p) for p in {c["path"] for c in comps if c.get("name") == "native"} & paths)
        d = {"model_gb": round(weights, 1), "disk_gb": round(sum(size_gb(p) for p in paths), 1)}
        r = rows.get(m["id"])
        if catalog and ram:
            f = catalog.fit_on({"memory_gb": r.memory_gb if r else round(weights, 1)}, ram, usable_vram(m, vram, catalog.DENSE_GB))
            d.update(memory_gb=f["memoryGb"], vram_gb=f["vramUseGb"], ram_gb=f["ramUseGb"], spill_gb=f["spillGb"], estimate=not r)
        out["models"][m["id"]] = d
    print(json.dumps(out))


def model_disk():
    """The drive holding the running model's weights (whole disk, e.g. nvme0n1), or None when nothing runs.
    findmnt/lsblk instead of st_dev, which is an anonymous device on btrfs; without them (util-linux) None."""
    for m in models():
        mid = m["id"]
        cfg = CONFIGS / f"{mid}.json"
        if not cfg.exists() or state(mid) == "stopped":
            continue
        args = json.loads(cfg.read_text()).get("args", [])
        weights = args[args.index("--native") + 1] if "--native" in args else None
        if not weights:
            return None
        try:
            src = subprocess.run(["findmnt", "-no", "SOURCE", "-T", weights], capture_output=True, text=True).stdout
            src = src.strip().split("[")[0]
            disk = subprocess.run(["lsblk", "-sno", "NAME,TYPE", src], capture_output=True, text=True).stdout.split("\n")
        except FileNotFoundError:
            return None
        return next((l.split()[0].lstrip("└─├│ ") for l in disk if l.endswith("disk")), None)
    return None


def cpu_sample(seconds=0.25, disk=None):
    """CPU use, package power and temperature over a short window: /proc/stat, RAPL, k10temp/coretemp.
    RAPL's energy_uj is root-only by default; power is None then (README: "CPU power").
    With `disk`, that drive's reads and busy time over the same window (/proc/diskstats) go to "disk"."""
    import time

    def diskstats():
        for line in open("/proc/diskstats"):
            f = line.split()
            if f[2] == disk:
                return int(f[5]) * 512, int(f[12])   # sectors read, ms spent doing I/O
        return None

    def stat():
        f = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        return sum(f), f[3] + f[4]   # all, idle + iowait

    rapl = Path("/sys/class/powercap/intel-rapl:0")

    def energy():
        try:
            return int((rapl / "energy_uj").read_text())
        except (OSError, ValueError):
            return None

    t0, (a0, i0), e0, d0 = time.monotonic(), stat(), energy(), disk and diskstats()
    time.sleep(seconds)
    t1, (a1, i1), e1, d1 = time.monotonic(), stat(), energy(), disk and diskstats()
    power = None
    if e0 is not None and e1 is not None:
        de = e1 - e0
        if de < 0:   # the counter wrapped
            de += int((rapl / "max_energy_range_uj").read_text())
        power = round(de / 1e6 / (t1 - t0), 1)
    temp = None
    for h in Path("/sys/class/hwmon").glob("hwmon*"):
        if (h / "name").read_text().strip() in ("k10temp", "coretemp", "zenpower"):
            try:
                temp = int((h / "temp1_input").read_text()) / 1000
            except (OSError, ValueError):
                pass
            break
    name = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "CPU")
    out = {"name": name, "util": round(100 * (1 - (i1 - i0) / max(1, a1 - a0))), "power": power, "temp": temp}
    if d0 and d1:
        dt = t1 - t0
        model = Path(f"/sys/block/{disk}/device/model")
        out["disk"] = {"name": disk, "model": model.read_text().strip() if model.exists() else disk,
                       "read_mbs": round((d1[0] - d0[0]) / dt / 1e6, 1), "util": min(100, round((d1[1] - d0[1]) / dt / 10))}
    return out


def update_check():
    """$STRATA_UPDATE_CHECK when set (a shell command, e.g. a fork's own check), else `strata update --check` in short.
    The last line says "update available" or "up to date"; the plugin offers the update on the first."""
    cmd = os.environ.get("STRATA_UPDATE_CHECK")
    if cmd:
        sys.exit(subprocess.run(cmd, shell=True).returncode)
    d = strata_json("update", "--check")
    if not d:
        sys.exit("strata update --check gave no answer")
    found = False
    for part, label in (("engine", "engine"), ("control", "terminal")):
        x = d.get(part) or {}
        offered = x.get("offered")
        offered = offered.get("version") if isinstance(offered, dict) else offered
        print(f"{label}: installed {x.get('installed') or '-'}, offered {offered or '-'} ({x.get('status') or '?'})")
        found |= x.get("status") == "available"
    print("update available: strata update" if found else "up to date")


def edit():
    if not PATCHES.exists():
        CONF.mkdir(parents=True, exist_ok=True)
        PATCHES.write_text("{\n}\n")
    print(PATCHES)


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("help", [])
    if cmd == "info": info()
    elif cmd == "details": details()
    elif cmd == "status": print(json.dumps({**(strata_json("status").get("snapshot") or {}), "cpu": cpu_sample(disk=model_disk())}))
    elif cmd == "apply": apply(rest or None)
    elif cmd == "start" and len(rest) == 1: start(rest[0])
    elif cmd == "stop": stop()
    elif cmd == "edit": edit()
    elif cmd == "update-check": update_check()
    else: print(__doc__.strip()); sys.exit(0 if cmd in ("help", "-h", "--help") else 2)
