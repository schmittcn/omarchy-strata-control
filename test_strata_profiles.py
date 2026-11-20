# python3 test_strata_profiles.py: the patch rules on a fresh setup-style config
import importlib.util
import sys

sys.dont_write_bytecode = True   # no __pycache__ in the plugin folder

spec = importlib.util.spec_from_file_location("sp", __file__.replace("test_strata_profiles.py", "strata-profiles.py"))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

cfg = {"args": ["--native", "m.gguf", "--max-context", "65536", "--vision", "--kv", "int8"],
       "args_written": ["--max-context", "65536"], "env": {"A": "1", "DROP": "x"}, "sampling": {"t": 1}}
patches = {"*": {"args": {"--max-context": "262144"}},
           "orn-*, other": {"args": {"--kv": "q8_0", "--mtp": "1", "--vision": None, "--fast": True},
                            "env": {"B": 2, "DROP": None}, "set": {"sampling": None, "vision": {"gpu": False}}}}
for p in sp.matching(patches, "orn-1"):
    sp.patch_config(cfg, p)

assert cfg["args"] == ["--native", "m.gguf", "--max-context", "262144", "--kv", "q8_0", "--mtp", "1", "--fast"], cfg["args"]
assert cfg["args_written"] == ["--max-context", "262144", "--kv", "q8_0", "--mtp", "1", "--fast"]
assert cfg["env"] == {"A": "1", "B": "2"}
assert "sampling" not in cfg and cfg["vision"] == {"gpu": False}
assert len(sp.matching(patches, "coder")) == 1 and len(sp.matching(patches, "other")) == 2
# a flag in Strata's user_args (applied over args at every start) is patched there too; others stay out of it
cfg = {"args": ["--vram-reserve-mib", "3072"], "user_args": ["--adapt-every", "1", "--vram-reserve-mib", "3072"]}
sp.patch_config(cfg, {"args": {"--vram-reserve-mib": "2700", "--kv": "q8_0"}})
assert cfg["user_args"] == ["--adapt-every", "1", "--vram-reserve-mib", "2700"], cfg["user_args"]
assert cfg["args"] == ["--vram-reserve-mib", "2700", "--kv", "q8_0"]

# our profiles: prefixed and valid for `strata profile`; a same-named profile pointing elsewhere is not ours
assert sp.profile_name("swift-iq3_s") == "omarchy-swift-iq3_s"
assert sp.profile_name("Swift.IQ3.UD") == "omarchy-swift-iq3-ud"
out = sp.CONFIGS / "x.json"
assert sp.ours({"source_config": str(sp.CONFIGS / "y.json")}, out)
assert not sp.ours({"source_config": "/home/u/Strata/strata-x.json"}, out)

# the patched copies can hold an api_key: owner-only, also when they replace an older world-readable file
import contextlib, io, json, os, tempfile
from pathlib import Path
os.umask(0o022)
with tempfile.TemporaryDirectory() as d:
    folder = Path(d) / "configs"
    folder.mkdir(mode=0o755)
    sp.private_dir(folder)
    assert folder.stat().st_mode & 0o777 == 0o700
    f = folder / "m.json"
    f.write_text("old")
    f.chmod(0o644)
    sp.write_private(f, '{"api_key": "k"}')
    assert f.stat().st_mode & 0o777 == 0o600 and f.read_text() == '{"api_key": "k"}'
    assert [p.name for p in folder.iterdir()] == ["m.json"]

# image generation runs only on Strata code that hands the prompt to sd-cli in a file, not on its command line
with tempfile.TemporaryDirectory() as d:
    serve = Path(d) / "serve"
    serve.mkdir()
    assert not sp.image_prompts_private(d)                               # no serve/images.py
    assert not sp.image_prompts_private(None)                            # serving copy not found
    (serve / "images.py").write_text('cmd = ["sd-cli", "-p", job["prompt"]]')
    assert not sp.image_prompts_private(d)
    (serve / "images.py").write_text('cmd = ["sd-cli", "--prompt-file", prompt_file]')
    assert sp.image_prompts_private(d)
# the serving copy is the server process's working directory, not the install_dir `models list` reports
real = sp.strata_json
sp.strata_json = lambda *a: {"child_pid": os.getpid()}
assert sp.serving_process("p") == (os.getpid(), os.getcwd())
sp.strata_json = lambda *a: {"state": "stopped", "child_pid": None}
assert sp.serving_process("p") is None
sp.strata_json = real

# /?q= links: the page may fill in the question, not send it (Niko1221/Strata#4221); unreadable counts as sending
with tempfile.TemporaryDirectory() as d:
    web = Path(d) / "serve" / "web"
    web.mkdir(parents=True)
    assert sp.web_sends_links(d)                                         # no page to check
    (web / "app.js").write_text('const startQuestion = new URLSearchParams(location.search).get("q");\n'
                                'loadHealth().then(() => { if (startQuestion) { $("input").value = startQuestion; send(); } });\n'
                                '$("send").onclick = () => send();\n')
    assert sp.web_sends_links(d)
    (web / "app.js").write_text('const startQuestion = new URLSearchParams(location.search).get("q");\n'
                                'loadHealth().then(() => { if (startQuestion) { $("input").value = startQuestion; $("input").focus(); } });\n'
                                '$("send").onclick = () => send();\n')
    assert not sp.web_sends_links(d)
    (web / "app.js").write_text('$("send").onclick = () => send();\n')    # no ?q= shortcut at all
    assert not sp.web_sends_links(d)

# Pillow in the server's Python: 13+ or none; anything else (or unknown) refuses a model with the picture reader on
assert sp.pillow_safe("13.0.0") and sp.pillow_safe("14.1") and sp.pillow_safe("none")
assert not sp.pillow_safe("12.3.0") and not sp.pillow_safe(None) and not sp.pillow_safe("")
exe, version = sp.server_pillow(os.getpid(), os.getcwd())             # asks this very process's Python
assert exe and version and (version == "none" or version[0].isdigit()), (exe, version)
assert sp.server_pillow(2**22 + 12345, "/") == (None, None)            # no such process

with tempfile.TemporaryDirectory() as d:
    (Path(d) / "serve" / "web").mkdir(parents=True)
    (Path(d) / "serve" / "web" / "app.js").write_text("// no ?q= shortcut\n")
    real_pillow = sp.server_pillow
    sp.server_pillow = lambda pid, root: ("/py", "12.3.0")
    assert sp.refusals({"args": []}, 1, d) == []                         # picture reader off: Pillow does not matter
    assert len(sp.refusals({"vision": {"mmproj": "m"}}, 1, d)) == 1
    sp.server_pillow = lambda pid, root: ("/py", "13.0.0")
    assert sp.refusals({"vision": {"mmproj": "m"}}, 1, d) == []
    assert len(sp.refusals({"images": {"exe": "sd-cli"}}, 1, d)) == 1     # no serve/images.py here
    sp.server_pillow = real_pillow

# the server is frozen while it is checked: its whole group (own session, as `strata runtime` starts it)
import signal
import subprocess
import time
srv = subprocess.Popen([sys.executable, "-c", "import socket, subprocess, sys, time\n"
                        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); print(c.pid, flush=True); time.sleep(60)"],
                       start_new_session=True, stdout=subprocess.PIPE, text=True)
child = int(srv.stdout.readline())
state = lambda pid: Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
assert sp.listening(srv.pid) and not sp.listening(os.getpid())
sp.signal_server(srv.pid, signal.SIGSTOP)
for _ in range(50):
    if state(srv.pid) == state(child) == "T":
        break
    time.sleep(0.02)
assert state(srv.pid) == state(child) == "T", (state(srv.pid), state(child))   # the engine stops with the server
sp.signal_server(srv.pid, signal.SIGCONT)
sp.signal_server(srv.pid, signal.SIGKILL)
assert srv.wait(5) == -signal.SIGKILL
sp.signal_server(srv.pid, signal.SIGKILL)                                # gone already: no error

# the strata command: a checkout folder (0.2.16 terminal/ layout or older), and an old command after the layout move
import tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as d:
    new, old = Path(d) / "new", Path(d) / "old"
    for base in (new / "terminal", old):
        (base / "bin").mkdir(parents=True); (base / "bin" / "strata").write_text("")
        (base / ".venv" / "bin").mkdir(parents=True); (base / ".venv" / "bin" / "python").write_text("")
    t = new / "terminal"
    assert sp.resolve_cli(str(new)) == ["env", f"STRATA_PYTHON={t}/.venv/bin/python", f"{t}/bin/strata"]
    assert sp.resolve_cli(str(old)) == ["env", f"STRATA_PYTHON={old}/.venv/bin/python", f"{old}/bin/strata"]
    # a pre-0.2.16 command on a checkout that moved: followed into terminal/; one that still exists is untouched
    assert sp.resolve_cli(f"env STRATA_PYTHON={new}/.venv/bin/python {new}/bin/strata") == \
        ["env", f"STRATA_PYTHON={t}/.venv/bin/python", f"{t}/bin/strata"]
    assert sp.resolve_cli(f"env STRATA_PYTHON={old}/.venv/bin/python {old}/bin/strata") == \
        ["env", f"STRATA_PYTHON={old}/.venv/bin/python", f"{old}/bin/strata"]
assert sp.resolve_cli(None) == ["strata"] and sp.resolve_cli("strata") == ["strata"]

# patches.json mistakes are named instead of doing nothing (or crashing in patch_config)
assert sp.patch_problems(patches) == [] and sp.patch_problems({}) == []
assert sp.patch_problems({"*": {"profile": {"port": 8095}, "env": {"A": None, "B": 1}, "set": {"vision": None}}}) == []
bad = sp.patch_problems({"*": {"arg": {}}, "a": {"args": ["--x"]}, "b": {"args": {"max-context": "1", "--y": [1]}},
                         "c": {"profile": {"prot": 1}}, "d": {"env": {"E": True}}, "e": [], "f": {"set": 3}})
assert len(bad) == 8, bad
assert sp.patch_problems([]) and 'unknown' in bad[0] and '"prot"' in bad[4], bad

# clean: our profiles stopped and removed with their configs and logs; someone else's same-named profile and patches.json stay
with tempfile.TemporaryDirectory() as t:
    t = Path(t)
    sp.PROFILES, sp.CONFIGS, sp.RUNTIME, sp.PATCHES = t / "profiles", t / "configs", t / "runtime", t / "patches.json"
    for d in (sp.PROFILES, sp.CONFIGS, sp.RUNTIME):
        d.mkdir()
    sp.PATCHES.write_text("{}")
    (sp.CONFIGS / "a.json").write_text("{}")
    (sp.PROFILES / "omarchy-a.json").write_text(json.dumps({"source_config": str(sp.CONFIGS / "a.json")}))
    (sp.PROFILES / "omarchy-mine.json").write_text(json.dumps({"source_config": "/elsewhere/mine.json"}))
    (sp.PROFILES / "other.json").write_text("{}")
    for f in ("omarchy-a.json", "omarchy-a.server.log", "omarchy-a.host.log", "omarchy-mine.server.log"):
        (sp.RUNTIME / f).write_text("")
    stopped = []
    real_strata, sp.strata = sp.strata, lambda *a, **k: stopped.append(a)
    with contextlib.redirect_stdout(io.StringIO()):
        sp.clean()
    sp.strata = real_strata
    assert stopped == [("runtime", "stop", "--profile", "omarchy-a", "--wait", "60")], stopped
    assert sorted(p.name for p in sp.PROFILES.iterdir()) == ["omarchy-mine.json", "other.json"]
    assert sorted(p.name for p in sp.RUNTIME.iterdir()) == ["omarchy-a.json", "omarchy-mine.server.log"]
    assert not sp.CONFIGS.exists() and sp.PATCHES.exists()

# idle: the latest of boot, request events and finished requests; never while a request runs or with nothing known
snap = {"instance": {"boot_time": 1000}, "work": {"active": 0, "queued": 0},
        "activity": {"events": [{"at": 1500}, {"at": 1600}]}, "recent": [{"time": 1650, "duration_s": 50}]}
assert sp.idle_seconds(snap, 2000) == 300
assert sp.idle_seconds({"instance": {"boot_time": 1000}}, 1600) == 600            # booted, never asked
assert sp.idle_seconds({**snap, "work": {"active": 1}}, 9999) is None
assert sp.idle_seconds({**snap, "work": {"queued": 2}}, 9999) is None
assert sp.idle_seconds({}, 9999) is None

# the model list's mark: every name the matching patches set, once, the most specific entry (last in the file) first
assert sp.yours(patches, "orn-1") == ["--kv", "--mtp", "--vision", "--fast", "B", "DROP", "sampling", "vision", "--max-context"]
assert sp.yours(patches, "coder") == ["--max-context"] and sp.yours({}, "x") == []

# idle_stop: stops the ready model past the limit; never a busy one, one under the limit, or another server's snapshot
def run_idle(minutes, snap, inst="i1"):
    stopped, real = [], (sp.models, sp.status, sp.strata_json, sp.strata, sp.time.time)
    sp.models = lambda: [{"id": "a"}, {"id": "b"}]
    sp.status = lambda mid: {"state": "ready", "instance_id": inst} if mid == "b" else {}
    sp.strata_json = lambda *a: {"snapshot": snap}
    sp.strata = lambda *a, **k: stopped.append(a)
    sp.time.time = lambda: 10_000
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            sp.idle_stop(minutes)
    finally:
        sp.models, sp.status, sp.strata_json, sp.strata, sp.time.time = real
    return stopped
quiet = {"instance": {"id": "i1", "boot_time": 10_000 - 31 * 60}, "work": {}}
assert run_idle(30, quiet) == [("runtime", "stop", "--profile", "omarchy-b", "--wait", "60")]
assert run_idle(45, quiet) == []                                                   # not idle long enough
assert run_idle(30, {**quiet, "work": {"active": 1}}) == []                        # a request runs
assert run_idle(30, quiet, inst="other") == []                                     # the snapshot is another server's
print("ok")
