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
import os, tempfile
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
    assert sp.risks({"args": []}, 1, d) == []                         # picture reader off: Pillow does not matter
    assert len(sp.risks({"vision": {"mmproj": "m"}}, 1, d)) == 1
    sp.server_pillow = lambda pid, root: ("/py", "13.0.0")
    assert sp.risks({"vision": {"mmproj": "m"}}, 1, d) == []
    assert len(sp.risks({"images": {"exe": "sd-cli"}}, 1, d)) == 1     # no serve/images.py here
    sp.server_pillow = real_pillow

print("ok")
