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

# image generation starts only on a Strata that hands the prompt to sd-cli in a file, not on its command line
with tempfile.TemporaryDirectory() as d:
    serve = Path(d) / "serve"
    serve.mkdir()
    images_on = {"images": {"exe": "sd-cli"}}
    assert sp.image_start_refused({"args": []}, d) is None                  # no image generation: never refused
    assert sp.image_start_refused({"images": None}, d) is None
    assert sp.image_start_refused(images_on, d)                             # no serve/images.py: refused
    (serve / "images.py").write_text('cmd = ["sd-cli", "-p", job["prompt"]]')
    assert "command line" in sp.image_start_refused(images_on, d)
    (serve / "images.py").write_text('cmd = ["sd-cli", "--prompt-file", prompt_file]')
    assert sp.image_start_refused(images_on, d) is None

print("ok")
