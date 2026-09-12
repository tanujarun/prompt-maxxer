# PyInstaller spec for the standalone engine shipped inside the installer.
#
# Build from the engine folder, with the engine's virtualenv active:
#     python -m PyInstaller --noconfirm --distpath dist --workpath build packaging/prompt-maxxer-engine.spec
#
# Output: dist/prompt-maxxer-engine/prompt-maxxer-engine.exe plus its _internal
# folder. The Tauri bundle config copies that whole folder into the installer.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ENGINE = Path(SPECPATH).parent

datas, binaries, hiddenimports = [], [], []

# Packages that carry native libraries or data files PyInstaller cannot find by
# following imports alone: the Silero VAD model, CTranslate2's DLLs, PortAudio,
# the Hugging Face transfer backend.
for package in (
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "tokenizers",
    "av",
    "soxr",
    "_sounddevice_data",
    "websockets",
    "huggingface_hub",
    "hf_xet",
):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception as exc:  # optional packages may be absent
        print(f"spec: skipping {package}: {exc}")
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Libraries that read their own installed version at import time.
for dist in ("huggingface_hub", "tqdm", "tokenizers", "faster_whisper", "ctranslate2",
             "numpy", "filelock", "packaging", "pyyaml", "requests", "httpx", "hf_xet"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

hiddenimports += collect_submodules("prompt_maxxer") + ["sounddevice", "winreg"]

# No NVIDIA libraries in the bundle. At ~1.9 GB they would push the installer
# past the 2 GiB an NSIS installer can hold, and burden every machine without an
# NVIDIA GPU. Installed copies download them on first launch instead - see
# prompt_maxxer/cuda_runtime.py.

a = Analysis(
    [str(ENGINE / "packaging" / "entry.py")],
    pathex=[str(ENGINE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # PIL is only in the build environment for screenshot tooling.
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyInstaller", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="prompt-maxxer-engine",
    console=True,  # the app starts it with no window; a console helps anyone running it by hand
    upx=False,
    icon=str(ENGINE.parent / "shell" / "src-tauri" / "icons" / "icon.ico"),
)

coll = COLLECT(exe, a.binaries, a.datas, name="prompt-maxxer-engine", upx=False)
