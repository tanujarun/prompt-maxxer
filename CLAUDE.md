# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Prompt Maxxer is a local-first, push-to-talk dictation app for **Windows** (hold a key, speak, release; text is typed at the caret). `README.md` is the full reference (architecture, settings, models, updates, tests, releasing); `../START-HERE.md` covers the surrounding dev folder. This repo is `source/` inside that folder — siblings `../signing/`, `../releases/`, `../reference/` are outside git.

The app targets Windows (Win32 keyboard hook, WASAPI, SendInput, NSIS, PowerShell scripts). Build/run/test commands below are PowerShell and cannot run on a Linux host; code reading and editing can.

## Commands

All run from this directory on Windows (PowerShell). The build environment (`engine\.venv`, `shell\node_modules`, `src-tauri\target`, frozen engine) is not kept on disk; the first `.\build.ps1` recreates it (multi-GB download).

```powershell
.\build.ps1                 # venv + lockfile deps + PyInstaller engine + signed NSIS installer
.\build.ps1 -SkipEngine     # reuse the frozen engine when only the shell changed
.\run.ps1                   # launch the dev build (shell uses engine\.venv directly)
.\run.ps1 -Console          # engine in foreground with its log
.\release.ps1 -Notes "..."  # publish built installer + latest.json (needs signed-in gh CLI)

cd shell; pnpm dev          # Vite on :1420, for previewing UI states (see below)
cd shell; pnpm build        # tsc + vite build

# engine tests are plain scripts, not pytest; run one at a time from engine\
cd engine
.\.venv\Scripts\python.exe test_controls.py          # hotkey, tape log, models, origin lock — quit the app first (uses port 8765)
.\.venv\Scripts\python.exe test_first_run.py
.\.venv\Scripts\python.exe test_spectrum.py
.\.venv\Scripts\python.exe test_inject.py            # injects into a real edit control
.\.venv\Scripts\python.exe test_runtime_cleanup.py
.\.venv\Scripts\python.exe test_gpu_runtime.py       # really downloads ~1.4 GB of GPU libs
cd ..\shell\src-tauri; cargo test --lib              # autostart + updater signature checks

# transcribe a WAV with no app/mic
& "$env:LOCALAPPDATA\Prompt Maxxer\engine\prompt-maxxer-engine.exe" --transcribe C:\path\speech.wav
```

No linter is configured. `engine/bench.py`, `sweep.py`, `fake_engine.py`, `demo_shot.py` are dev utilities.

Previewing UI without an engine: with `pnpm dev` running, open `http://localhost:1420/overlay.html?demo=rec&theme=dark` or `index.html?demo=ready&theme=light` (demo names are listed in README "Previewing states"; handled in `shell/src/dev.ts`).

## Architecture

Two processes joined by a WebSocket on `127.0.0.1:8765`:

- **Engine** (`engine/prompt_maxxer/`, Python 3.12) owns the whole hot path: `hotkey.py` (WH_KEYBOARD_LL hook — enqueue only, never block the callback) → `audio.py` (WASAPI stream opened once, kept warm) → `pipeline.py` (the central loop) → `asr/` (faster-whisper behind a narrow swappable interface) → `cleanup.py` (stub for a future LLM) → `inject.py` (SendInput for short text, clipboard paste for long). `streaming.py` produces the overlay's live partials by re-running Whisper over the growing buffer; partials are display-only and **never typed** — only the single final pass after key release is injected. `models.py` is the model catalog/fit/recommendation/download logic; `system.py` detects GPU/VRAM/CPU; `cuda_runtime.py` + `cuda_paths.py` handle the first-launch download and registration of NVIDIA cuBLAS/cuDNN.
- **Shell** (`shell/`, Tauri 2 / Rust + Vite TypeScript) is cosmetic and disposable: tray, settings window (created lazily), always-on-top overlay pill, updater, autostart, installer. `src-tauri/src/engine.rs` supervises the engine (looks for `PROMPT_MAXXER_ENGINE_DIR`, then the source-checkout venv, then the frozen `engine\prompt-maxxer-engine.exe`; attaches to an existing listener on 8765; runs it in a Win32 job object so a dead shell can't leave an orphan engine holding a global keyboard hook). `src/engine.ts` is the frontend WebSocket client.
- **The engine is the source of truth.** Dictation must keep working if the shell crashes or never starts. `ipc.py` (`EventBus`) fans events out to all windows and takes commands from settings; every new connection gets a full snapshot (windows come and go), and sends are fire-and-forget so nothing can stall dictation. The WebSocket enforces an **Origin allow-list** (`ALLOWED_ORIGINS`) so web pages can't connect; keep it intact when adding windows or dev origins.
- Single-instance is enforced in both layers (shell brings up the running copy's settings; the engine holds its own lock). A source build and an installed copy count as the same app — quit one before running the other.
- Config lives at `%APPDATA%\Prompt Maxxer\config.json` (schema/defaults in `engine/prompt_maxxer/config.py`); dictation text is never written to disk, the tape log is in-memory only.
- Whisper hallucinates on silence, so an RMS gate (`audio.min_rms`) drops quiet audio before the model runs. Don't remove it.

## Rules specific to this project

- **Nothing floats.** Every dependency is pinned: `engine/requirements.lock`, `shell/pnpm-lock.yaml`, `src-tauri/Cargo.lock`, model revisions (`REVISIONS` in `models.py`), NVIDIA wheel URL/size/SHA-256 (`WHEELS` in `cuda_runtime.py`). Changing the GPU libraries means bumping `WHEELS` **and** `RUNTIME_TAG` in `cuda_paths.py` together. Changing a model pin re-downloads that model.
- **Version bumps** for a release must be made in three places: `shell/src-tauri/tauri.conf.json`, `shell/src-tauri/Cargo.toml`, `shell/package.json`. Installed copies only update to a higher version. Test updates end-to-end (local update server with a lower-version build) before publishing.
- **Signing key** (`../signing/updater.key`): never print, copy into the repo, or upload. If lost, installed copies can never be updated. `build.ps1` finds it beside the checkout; without it the build works but the installer can't ship as an update.
- **Uninstall must stay scoped** to what the app downloaded itself (GPU library sets marked by `installed.json`, staged updates) — never shared CUDA, the Hugging Face cache, or WebView2 (`installer-hooks.nsh`).
- **Repos:** this repo (`tanujarun/prompt-maxxer`) is public and must stay so, because installed copies fetch updates from its GitHub releases without signing in. Everything committed is world-readable: no keys, tokens, personal email, machine paths or dictated text. `tanujarun/prompt-maxxer-releases` is the old update location that 0.1.0 looks at; it only forwards to the 0.1.1 installer here and is due to be deleted. Commits use the repo's local git identity (GitHub noreply address), never the personal email.
- Don't build, commit, push, release or delete anything unless asked; ask before outward-facing actions.

## Gotchas

- Windows PowerShell 5.1 turns native-tool stderr into terminating errors under `$ErrorActionPreference = "Stop"`; the scripts use an `Invoke-Native` helper that judges by exit code — use it for new native calls.
- `pnpm tauri build -- --locked` doesn't work (pnpm swallows `--`); `build.ps1` runs `cargo fetch --locked` instead.
- NSIS fails ("error mmapping file") past 2 GiB, which is why GPU libraries are a first-launch download; `build.ps1` has a size guard.
- CTranslate2 loads cuBLAS/cuDNN lazily, so `cuda_paths.py` registers folders with both `os.add_dll_directory` and a PATH prepend.
- `engine/pyproject.toml` needs `[tool.setuptools.packages.find] include = ["prompt_maxxer*"]` because `packaging/` sits beside the package.
- Hugging Face downloads on Windows don't use symlinks; progress measures the whole repo folder with Xet disabled.
- Changing the push-to-talk key is read by the engine's hook, not the window; keys that would break typing if swallowed are refused in `keys.py`. Right Ctrl is the default because it isn't AltGr.
- UI follows the `nothing-design` skill (monochrome, flat, no shadows/gradients). Red `#D71921` means recording/errors only; orange `#FF5F1F` is brand identity only — never on screen together. Fonts (Space Grotesk, Space Mono, Doto) are bundled, not loaded from the network. Regenerate the icon with `shell/branding/make_icon.py` then `pnpm tauri icon branding/icon.png`.
