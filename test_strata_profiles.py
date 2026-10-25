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

print("ok")
