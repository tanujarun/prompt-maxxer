"""Download the NVIDIA GPU libraries on first launch.

The installed app does not carry cuBLAS and cuDNN. At ~1.9 GB they would push
the installer past the 2 GiB its format can hold, and every teammate without an
NVIDIA GPU would download them for nothing. So machines with a CUDA GPU fetch
them once, on first launch - from the same official NVIDIA wheels a source
checkout installs with pip, pinned by version and verified by SHA-256.

A download unpacks into a staging folder that is renamed into place only once
every file is in, so an interrupted download never leaves a half-installed set
that looks complete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import cuda_paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Wheel:
    name: str
    url: str
    size: int
    sha256: str


#: The exact wheels a source checkout's virtualenv is built and tested with.
WHEELS = (
    Wheel(
        "nvidia-cublas-cu12 12.9.2.10",
        "https://files.pythonhosted.org/packages/20/e2/fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
        553162896,
        "623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
    ),
    Wheel(
        "nvidia-cudnn-cu12 9.25.1.1",
        "https://files.pythonhosted.org/packages/0b/ee/b5699f1960e358ec995bb72f71c2ec06c550fd0c8280525796d6646c0299/nvidia_cudnn_cu12-9.25.1.1-py3-none-win_amd64.whl",
        732338891,
        "debb5f5901ae6071f34d0a2b256acecc33dc3277f1fd5a11f8249f921db8a40d",
    ),
    Wheel(
        "nvidia-cuda-nvrtc-cu12 12.9.86",
        "https://files.pythonhosted.org/packages/52/de/823919be3b9d0ccbf1f784035423c5f18f4267fb0123558d58b813c6ec86/nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl",
        76408187,
        "72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349",
    ),
)

TOTAL_BYTES = sum(wheel.size for wheel in WHEELS)

#: Every library CUDA inference needs. All must be present to use the GPU.
REQUIRED = frozenset(
    {
        "cublas64_12.dll",
        "cublaslt64_12.dll",
        "nvrtc64_120_0.dll",
        "cudnn64_9.dll",
        "cudnn_ops64_9.dll",
        "cudnn_cnn64_9.dll",
        "cudnn_graph64_9.dll",
        "cudnn_heuristic64_9.dll",
        "cudnn_engines_precompiled64_9.dll",
        "cudnn_engines_runtime_compiled64_9.dll",
    }
)

#: In the wheels but never loaded. A scan of every binary found cudnn_adv (the
#: recurrent-network and attention APIs) referenced only by cuDNN's own loader
#: and never called by CTranslate2, and the .alt runtime compiler referenced by
#: nothing at all. Skipping them saves ~340 MB of disk.
SKIPPED = frozenset({"cudnn_adv64_9.dll", "nvrtc64_120_0.alt.dll"})

ProgressCallback = Callable[[int, int], None]


def available() -> bool:
    """Whether every library CUDA inference needs can already be found."""
    names: set[str] = set()
    for directory in cuda_paths.register():
        names.update(path.name.lower() for path in Path(directory).glob("*.dll"))
    return REQUIRED <= names


def _complete(root: Path) -> bool:
    if not (root / "installed.json").is_file():
        return False
    names = {path.name.lower() for path in root.glob("*/bin/*.dll")}
    return REQUIRED <= names


def install(on_progress: ProgressCallback | None = None) -> Path:
    """Download, verify and unpack the pinned wheels. Returns the install folder."""
    root = cuda_paths.runtime_root()
    if _complete(root):
        cuda_paths.register()
        _remove_superseded(root)
        return root

    root.parent.mkdir(parents=True, exist_ok=True)
    staging = root.with_name(root.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()

    finished_bytes = 0

    def report(received: int) -> None:
        if on_progress is not None:
            on_progress(finished_bytes + received, TOTAL_BYTES)

    try:
        for wheel in WHEELS:
            log.info("downloading %s (%.0f MB)", wheel.name, wheel.size / 1e6)
            archive = _download(wheel, root.parent, report)
            try:
                _unpack(archive, staging)
            finally:
                archive.unlink(missing_ok=True)
            finished_bytes += wheel.size

        (staging / "installed.json").write_text(
            json.dumps({"tag": cuda_paths.RUNTIME_TAG, "wheels": [w.name for w in WHEELS]}, indent=2),
            encoding="utf-8",
        )
        shutil.rmtree(root, ignore_errors=True)
        staging.rename(root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    cuda_paths.register()
    log.info("GPU libraries installed in %s", root)
    _remove_superseded(root)
    return root


def _remove_superseded(root: Path) -> None:
    """Delete GPU library sets that an earlier version of the app downloaded.

    An app update that moves to newer cuBLAS or cuDNN downloads the new set
    beside the old one, which is over a gigabyte of dead weight once the new set
    is in. Only folders this module creates are touched, and never beside a
    folder someone pointed the app at themselves.
    """
    if os.environ.get("PROMPT_MAXXER_CUDA_DIR"):
        return
    try:
        siblings = [path for path in root.parent.iterdir() if path != root and path.is_dir()]
    except OSError:
        return
    for sibling in siblings:
        if (sibling / "installed.json").is_file() or sibling.name.endswith(".staging"):
            log.info("removing superseded GPU libraries in %s", sibling)
            shutil.rmtree(sibling, ignore_errors=True)


def _download(wheel: Wheel, folder: Path, on_bytes: Callable[[int], None]) -> Path:
    target = folder / (wheel.url.rsplit("/", 1)[-1] + ".part")
    digest = hashlib.sha256()
    received = 0
    last_report = 0.0
    request = urllib.request.Request(wheel.url, headers={"User-Agent": "PromptMaxxer"})
    try:
        # urllib picks up the Windows system proxy, which matters on
        # corporate networks.
        with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last_report >= 0.25:
                    last_report = now
                    on_bytes(received)
        on_bytes(received)
        if received != wheel.size or digest.hexdigest() != wheel.sha256:
            raise RuntimeError(f"{wheel.name} failed verification ({received} of {wheel.size} bytes)")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _unpack(archive: Path, destination: Path) -> None:
    """Copy the wheel's nvidia/<package>/bin/*.dll files, and nothing else."""
    with zipfile.ZipFile(archive) as wheel:
        for member in wheel.infolist():
            parts = member.filename.split("/")
            if (
                len(parts) != 4
                or parts[0] != "nvidia"
                or parts[2] != "bin"
                or not parts[3].lower().endswith(".dll")
                or parts[3].lower() in SKIPPED
            ):
                continue
            target = destination / parts[1] / "bin" / parts[3]
            target.parent.mkdir(parents=True, exist_ok=True)
            with wheel.open(member) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out, 1 << 20)
