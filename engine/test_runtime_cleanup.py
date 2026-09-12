"""GPU library sets from earlier app versions are removed, and nothing else is.

Builds a fake gpu-runtime folder - the current set, one an older version of the
app downloaded, an abandoned staging folder and an unrelated folder - and checks
what survives. Downloads nothing.

    python test_runtime_cleanup.py
"""

import json
import os
import tempfile
from pathlib import Path

base = Path(tempfile.mkdtemp(prefix="pm-runtime-cleanup-"))
os.environ["LOCALAPPDATA"] = str(base)
os.environ.pop("PROMPT_MAXXER_CUDA_DIR", None)

from prompt_maxxer import cuda_paths, cuda_runtime  # noqa: E402

results = []


def check(name, ok):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")


def fake_set(folder: Path) -> Path:
    bin_dir = folder / "fake" / "bin"
    bin_dir.mkdir(parents=True)
    for name in cuda_runtime.REQUIRED:
        (bin_dir / name).write_bytes(b"")
    (folder / "installed.json").write_text(json.dumps({"tag": folder.name}), encoding="utf-8")
    return folder


root = cuda_paths.runtime_root()
check("the runtime folder is under LOCALAPPDATA", base in root.parents)

current = fake_set(root)
older = fake_set(root.parent / "cublas12.4.5-cudnn9.1.0")
abandoned = root.parent / "cublas12.4.5-cudnn9.1.0.staging"
abandoned.mkdir()
unrelated = root.parent / "notes"
unrelated.mkdir()

cuda_runtime.install()  # the current set is complete, so this only tidies up

check("the current set is kept", (current / "installed.json").is_file())
check("an older version's set is removed", not older.exists())
check("an abandoned staging folder is removed", not abandoned.exists())
check("folders it did not create are left alone", unrelated.exists())

# A folder chosen by hand is never tidied around.
os.environ["PROMPT_MAXXER_CUDA_DIR"] = str(base / "custom" / "runtime")
beside = fake_set(base / "custom" / "something-else")
fake_set(Path(os.environ["PROMPT_MAXXER_CUDA_DIR"]))
cuda_runtime.install()
check("nothing is removed beside a custom folder", beside.exists())

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
