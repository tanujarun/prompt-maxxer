"""Make the CUDA libraries visible to CTranslate2 on Windows.

The libraries come from one of two places:

* A source checkout: the nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels in the
  virtualenv, under site-packages/nvidia/*/bin.
* An installed copy: the same wheels' DLLs, downloaded on first launch into
  %LOCALAPPDATA%/Prompt Maxxer/gpu-runtime (see cuda_runtime.py). They are not
  bundled: at ~1.9 GB they would push the installer past what its format can
  hold, and burden every machine without an NVIDIA GPU.

Unlike PyTorch, CTranslate2 does not register these directories itself, so
without help it fails at first inference with "Library cublas64_12.dll is not
found or cannot be loaded". Both registrations below are needed, and for
different reasons:

* os.add_dll_directory covers DLLs resolved as dependencies of Python
  extension modules, which since 3.8 no longer search PATH.
* Prepending to PATH covers CTranslate2 loading cuBLAS and cuDNN lazily
  through its own LoadLibrary call at first use, which follows the standard
  Windows search order and ignores add_dll_directory entirely.

Runs at import, before ctranslate2 is loaded, and again after a download, so
the new libraries are found before a model first touches the GPU.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: Names the pinned library set. A different set downloads into a new folder
#: rather than mixing versions with an old one.
RUNTIME_TAG = "cublas12.9.2-cudnn9.25.1"

_registered: list[str] = []


def runtime_root() -> Path:
    """Where downloaded GPU libraries live."""
    override = os.environ.get("PROMPT_MAXXER_CUDA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Prompt Maxxer" / "gpu-runtime" / RUNTIME_TAG


def _library_roots() -> list[Path]:
    entries = list(sys.path)
    # A frozen build (PyInstaller) unpacks bundled packages under _MEIPASS,
    # which is not always on sys.path.
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        entries.insert(0, bundle)
    roots = [Path(entry) / "nvidia" for entry in entries if entry]
    roots.append(runtime_root())
    return roots


def register() -> list[str]:
    """Add every CUDA library directory found to the DLL search path.

    Safe to call repeatedly: only directories not already registered are added.
    Returns every directory registered so far.
    """
    if sys.platform != "win32":
        return _registered

    new: list[str] = []
    for root in _library_roots():
        if not root.is_dir():
            continue
        for bin_dir in sorted(root.glob("*/bin")):
            path = str(bin_dir)
            if path in _registered or path in new or not any(bin_dir.glob("*.dll")):
                continue
            new.append(path)
            try:
                os.add_dll_directory(path)
            except OSError:
                log.debug("could not add_dll_directory %s", path)

    if new:
        os.environ["PATH"] = os.pathsep.join(new) + os.pathsep + os.environ.get("PATH", "")
        _registered.extend(new)
        log.debug("registered %d CUDA DLL directories", len(new))

    return _registered
