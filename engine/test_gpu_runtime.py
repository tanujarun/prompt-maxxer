"""Download the GPU libraries the way an installed copy does, and check them.

Fetches the pinned NVIDIA wheels (about 1.4 GB) into a folder of its own,
verifying their checksums, then checks the unpacked result: every library CUDA
inference needs is there, the two never-loaded ones are not, nothing partial is
left behind, and a second install is a no-op.

Leaves the folder in place and prints its path, so a frozen engine can be
pointed at it with PROMPT_MAXXER_CUDA_DIR. Delete it afterwards.

    python test_gpu_runtime.py [folder]
"""

import os
import sys
import tempfile
import time
from pathlib import Path

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="pm-gpu-runtime-")) / "runtime"
# Must be set before the package is imported: the runtime folder is read then.
os.environ["PROMPT_MAXXER_CUDA_DIR"] = str(target)

from prompt_maxxer import cuda_paths, cuda_runtime  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


progress: list[tuple[int, int]] = []
started = time.monotonic()
root = cuda_runtime.install(lambda done, total: progress.append((done, total)))
elapsed = time.monotonic() - started

check("installs into the configured folder", root == target, str(root))

names = {path.name.lower() for path in root.glob("*/bin/*.dll")}
missing = sorted(cuda_runtime.REQUIRED - names)
check("every library CUDA inference needs is present", not missing, f"missing: {missing}" if missing else f"{len(names)} DLLs")
check("the never-loaded libraries are skipped", not (cuda_runtime.SKIPPED & names))

sizes = sorted({done for done, _ in progress})
check("reports progress as it downloads", len(sizes) >= 10, f"{len(progress)} updates")
check("progress ends at the full download size",
      progress and progress[-1][0] == cuda_runtime.TOTAL_BYTES,
      f"{progress[-1][0] if progress else 0} of {cuda_runtime.TOTAL_BYTES}")

leftovers = list(root.parent.glob("*.part")) + [p for p in [root.with_name(root.name + ".staging")] if p.exists()]
check("leaves no partial files or staging folder", not leftovers, str(leftovers))
check("marks the set as installed", (root / "installed.json").is_file())

bins = {str(p) for p in root.glob("*/bin")}
check("registers the new folders for DLL loading", bins <= set(cuda_paths.register()))

started = time.monotonic()
cuda_runtime.install()
check("a second install is a no-op", time.monotonic() - started < 2)

disk = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
print(f"\n   downloaded {cuda_runtime.TOTAL_BYTES / 1e9:.2f} GB in {elapsed:.0f}s; {disk / 1e9:.2f} GB on disk")
print(f"   RUNTIME_DIR={root}")
print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
