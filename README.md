# Prompt Maxxer

Say it — don’t type it.

Push-to-talk dictation for Windows. Hold a key, speak, release: the text lands
wherever your caret is. Recognition runs on your own machine, so nothing you say
leaves it and there is no per-seat subscription.

A local-first take on [Wispr Flow](https://wisprflow.ai).

---

## Install

For anyone who just wants to use it. Nothing else needs installing first — no
Python, no GPU toolkit.

1. Open the [latest release](https://github.com/tanujarun/prompt-maxxer-releases/releases/latest)
   and download **`Prompt-Maxxer_<version>_x64-setup.exe`**.
2. Run it. It installs for your user account only, so it does not ask for
   administrator rights.
3. **Windows may say "Windows protected your PC".** The installer is not
   code-signed yet, so SmartScreen does not recognise it. Click **More info →
   Run anyway**.

### First launch

The settings window opens by itself the first time:

- Prompt Maxxer checks your hardware and picks the speech model that suits it —
  `large-v3-turbo` on an NVIDIA GPU, a smaller model on machines without one.
- **On an NVIDIA machine** it first downloads NVIDIA's GPU libraries (cuBLAS and
  cuDNN, about 1.4 GB) straight from NVIDIA's official Python packages. They are
  not in the installer: they would push it past the 2 GB an installer can hold,
  and machines without an NVIDIA GPU would download them for nothing. If the
  download fails, that session runs on the CPU and it tries again next launch.
- It then downloads the model once (between about 75 MB and 1.6 GB).
- Progress for both is shown, and neither happens again. The first launch
  needs an internet connection; after that, dictation works offline.
- When the big word says **READY**, hold **Right Ctrl**, speak, and let go.

After that it lives in the tray. **Left-click the tray icon** for settings,
right-click for the menu.

### Updates

Prompt Maxxer keeps itself up to date. It checks for a new version shortly
after launch and every six hours, downloads it quietly in the background, and
installs it the next time the app restarts — so an update never lands
mid-sentence. When one is waiting, the settings footer and the tray menu offer
**Restart to update**; otherwise it installs at the next launch or when you quit
from the tray, with a progress bar for a few seconds.

Every update is signed, and the app refuses one whose signature does not match.

Nothing the app depends on can change underneath it between updates. Each
release pins exact versions of everything: the engine's Python libraries are
built into the installer from a lockfile, the GPU libraries are fetched by exact
version and checked against a SHA-256 hash, and each speech model is fetched at
a fixed revision. A new library version only ever arrives as part of a tested
app update. When an update moves to newer GPU libraries, it downloads the new
set once and deletes the old one.

### Requirements

- Windows 10 or 11, 64-bit, and a microphone.
- An NVIDIA GPU is optional. Without one, dictation runs on the CPU with a
  smaller model: a little less accurate and slower, but usable.
- Disk: about 0.3 GB for the app, 1.7 GB for the GPU libraries on NVIDIA
  machines, plus the model.
- For the first launch and updates, access to `github.com`, `huggingface.co` and
  `files.pythonhosted.org`. A corporate proxy set in Windows is used
  automatically.

### If something is off

| Symptom | What to do |
|---|---|
| Settings shows **OFFLINE** | Click **Start engine**. If it will not stay up, send the log below to whoever maintains the app. |
| Nothing happens when holding the key | Check the key in settings. Elevated (admin) windows cannot receive dictation unless Prompt Maxxer also runs as admin. |
| Antivirus quarantines `prompt-maxxer-engine.exe` | A known false positive for unsigned, bundled Python apps. Restore it and allow it. |
| Wrong microphone | Set `audio.device` in the config file (see below). |
| Stuck downloading GPU libraries or the model | Check that `files.pythonhosted.org` and `huggingface.co` are reachable. The engine log says what failed. |
| Settings says the GPU libraries failed and it is running on the CPU | Same as above; it retries on the next launch. |

To check recognition on its own, without the app or a microphone, run the
engine against any WAV file. It prints the model, whether it used the GPU, how
long it took, and the text:

```powershell
& "$env:LOCALAPPDATA\Prompt Maxxer\engine\prompt-maxxer-engine.exe" --transcribe C:\path\to\speech.wav
```

Where things live:

- Settings: `%APPDATA%\Prompt Maxxer\config.json`
- Engine log: `%APPDATA%\Prompt Maxxer\logs\engine.log`
- GPU libraries: `%LOCALAPPDATA%\Prompt Maxxer\gpu-runtime`
- A downloaded update waiting to install: `%LOCALAPPDATA%\Prompt Maxxer\updates`
- Downloaded models: `%USERPROFILE%\.cache\huggingface\hub`

Uninstall from **Settings → Apps → Installed apps**.

It removes the app, the GPU libraries it downloaded into its own folder, and any
update it had waiting. It deliberately leaves anything shared with the rest of
the machine:

| Left alone | Why |
|---|---|
| A CUDA toolkit install, the NVIDIA driver's libraries, another app's copies of cuBLAS/cuDNN | Prompt Maxxer never installs CUDA system-wide. It downloads NVIDIA's official cuBLAS and cuDNN packages into `%LOCALAPPDATA%\Prompt Maxxer\gpu-runtime` and puts only that private copy on its own library path, so removing it cannot affect any other program. |
| A folder set with `PROMPT_MAXXER_CUDA_DIR` | Chosen by you, possibly shared, so it is never touched. |
| Speech models | The Hugging Face cache is shared with other tools. Delete them from the folder above to reclaim the space. |
| The WebView2 runtime | Part of Windows, used by many applications. |
| Settings and logs | Unless you tick "Delete the application data". |

Even inside its own `gpu-runtime` folder the uninstaller only deletes library
sets the app itself downloaded — each marked by a file it writes when a download
finishes — and leaves the folder in place if anything else is in it.

---

## Architecture

Two processes, split by responsibility rather than by language convenience.

```
┌─ SHELL (Tauri 2 / Rust) ────────┐      ┌─ ENGINE (Python 3.12) ───────────┐
│  tray icon, settings window     │      │  ▸ LL keyboard hook (PTT down/up)│
│  branded overlay pill           │◄────►│  ▸ WASAPI mic stream (kept warm) │
│  tape log, models, theme        │  WS  │  ▸ faster-whisper (CUDA or CPU)  │
│  installer, autostart           │ 8765 │  ▸ cleanup stage (LLM slot)      │
│                                 │      │  ▸ SendInput / clipboard inject  │
└─────────────────────────────────┘      └──────────────────────────────────┘
        cosmetic, disposable                  owns the entire hot path
```

The engine owns hotkey → capture → recognise → clean → inject end to end.
Dictation keeps working if the shell crashes or was never started. The shell
renders the engine's event stream and sends it commands from settings.

In the installed app the engine is a standalone program built with PyInstaller
and shipped inside the installer; the CUDA libraries it needs are downloaded on
first launch (see [First launch](#first-launch)). In a source checkout the shell
runs the engine straight from its virtualenv, with the CUDA libraries pip
installed into it.

### Why these pieces

| Decision | Reason |
|---|---|
| **faster-whisper** over whisper.cpp | CTranslate2 ships prebuilt CUDA wheels. No CUDA toolkit, no `nvcc`, no compile step for the recognition path. |
| **`int8_float16`** | Within noise of `float16` for speed on Ada, at a fraction of the VRAM — leaving the card free for a cleanup LLM. |
| **Tauri over Electron** | The shell adds only a few megabytes, because Windows already ships WebView2. The installer's size is the engine, not the UI. See the memory note below — the RAM win is smaller than usually claimed. |
| **Python owns the hot path** | Keeps the shell disposable, and keeps recognition next to the audio buffer with no IPC hop in the latency budget. |
| **Engine is the source of truth** | The UI can die, restart, or never launch without affecting dictation. |

## Measured latency

RTX 4080 SUPER, `large-v3-turbo`, `int8_float16`, greedy decoding, median of 5
after warm-up:

| Configuration | 3 s utterance | 12.8 s utterance |
|---|---:|---:|
| autodetect language + VAD | 183 ms | 265 ms |
| **language pinned + VAD** (default) | **113 ms** | 191 ms |
| language pinned, no VAD | 109 ms | 169 ms |

Two things fell out of measuring rather than guessing:

* **Pinning the language is worth ~70 ms.** Leaving it on autodetect makes
  Whisper run a whole extra encoder pass just to identify the language. Set
  `asr.language` to `null` if you switch languages often and want it back.
* **The model must be warmed twice.** The first inference still pays CUDA
  autotuning; benchmarking after a single warm pass overstates steady-state
  latency by about 3x.

VAD costs ~5 ms on short audio and earns it back by trimming silence.

## Design

A tape recorder, drawn with Nothing's rules and Teenage Engineering's
instincts. Built on the `nothing-design` skill: monochrome, typographic, flat,
no shadows, blur or gradients in the chrome.

**Colour is an event with two meanings.** Red `#D71921` only ever means
*recording* (and errors, which are also interrupts). TE orange `#FF5F1F` is
identity only: the icon and the wordmark dot. They never share the screen; the
wordmark dot greys out while the red REC state is showing.

**Both themes are first-class.** OLED black with white readouts, or warm
off-white with black ink — following Windows, or forced from settings.

**Type:** Space Grotesk for reading, Space Mono caps for every instrument label
and number, Doto dot-matrix for the one display moment per surface. All three
are bundled from Fontsource, so nothing loads from the network.

**The recording pill is an LCD readout.** Top row: REC lamp, mode
(`REC` / `PROC` / `DONE`), tape counter, dB readout. Main row: a 16-segment
square-ended VU meter with peak hold, then your words in Doto with a blinking
block cursor. Settled words are white; words Whisper may still revise are
dimmed. Long lines hard-clip at the left like a real display.

**The settings window is a faceplate.** The engine state is the hero, one Doto
word (`READY`, `REC`, `WORKING`, `OFFLINE`) beside a voice-band visualizer.
Session readings sit in instrument rows, with latency drawn as a segmented bar
against the 600 ms budget. Takes are kept in a numbered tape log.

**Icon:** an orange TE chassis carrying two tape reels joined by the tape, over
a row of grille dots. Regenerate it with `branding/make_icon.py`, then
`pnpm tauri icon branding/icon.png`.

### Previewing states

Every state can be rendered without an engine and without changing the Windows
theme. Run `pnpm dev` in `shell/`, then open:

```
http://localhost:1420/overlay.html?demo=rec&theme=dark
http://localhost:1420/index.html?demo=ready&theme=light
```

Overlay demos: `rec`, `rec-empty`, `proc`, `done`, `nospeech`, `error`,
`loading`. Settings demos: `ready`, `rec`, `working`, `offline`, `capture`,
`refused`, `confirm`, `menu`, `models`, `startup-blocked`, `update`. The app's own windows
load with no query string, so none of this runs in normal use.

## Settings

Left-click the tray icon, or launch the app again, to open settings. It has two
sections, and an **Auto / Light / Dark** switch that the overlay pill and the
window frame follow immediately.

### Session

- **Push-to-talk key.** Click the keycap, then press the key you want. The key
  is read by the engine's own keyboard hook, not the window, so left and right
  modifiers are distinct, F13–F24 and media keys work, and the press is
  swallowed rather than typed. Keys that would break normal typing if swallowed
  system-wide are refused with the reason: letters, digits, Space, Enter, arrows,
  and the left-hand Ctrl, Shift, Alt and Win. Esc cancels. The choice is saved.
- **Start with Windows.** Off by default. See below.
- **Visualizer.** Sixteen voice bands beside the engine state, low pitches on
  the left, each rising from the bottom as the microphone picks up sound in its
  range, whenever the window is open. The input peak in dB sits beneath it;
  click it to hold the display. The engine analyses the last 64 ms of audio
  about thirty times a second into log-spaced bands from 90 Hz to 7.5 kHz, and
  scales each by overall loudness, so a quiet room stays flat while speech
  lights the bands the voice is in. Levels are only streamed to a window that
  asks for them, and stop while it is minimised.
- **Tape log.** Every take this session, kept by the engine rather than the
  window, so it survives closing settings and includes takes made while it was
  closed. **Clear** asks once more before wiping it. The log lives in memory
  only; dictation is never written to disk, and quitting clears it.

### Start with Windows

Turning it on adds Prompt Maxxer to the per-user startup list — the same one
Windows shows under Settings > Apps > Startup — so it launches at sign-in
straight into the tray, with the engine loading and no window opening. No
administrator rights are involved.

The switch reports what Windows will actually do, not just whether an entry
exists:

- If the entry was **switched off in Windows' Startup apps**, the switch shows
  off and says so. Turning it on here clears that block.
- If the entry points at **a different copy of the app** (a source build versus
  the installed one, say), the switch shows off and says so. Turning it on here
  re-points it at the copy you are running.

It re-checks whenever the settings window comes to the front, so a change made
in Windows shows up without restarting.

### Models

Reads the machine once at startup — GPU and VRAM from `nvidia-smi` (WMI caps
VRAM at 4 GB), CUDA support from CTranslate2, CPU and memory from Windows — and
then:

- **Recommends one model** for this machine, with the reason in a sentence.
  With a CUDA GPU of 3 GB or more that is `large-v3-turbo`; smaller GPUs and
  CPU-only machines step down to distilled or small models, preferring
  English-only models when the dictation language is English. The first launch
  on a machine uses this recommendation automatically.
- **Rates every model** the engine can run for accuracy, speed, size and fit:
  runs well, slow here, won't fit, or English only.
- **Downloads** a model with live progress, one at a time, and **switches**
  between downloaded ones. A switch frees the current model before loading the
  next, so a smaller card never needs room for both, and restores the old one
  if the new one fails to load.

The model name in the footer is also a menu for switching between downloaded
models without leaving the session view, and the tray has a **Models…** item.

VRAM estimates were checked against real use on an RTX 4080 SUPER while
transcribing, including the CUDA context:

| Model | Measured | Shown |
|---|---:|---:|
| large-v3-turbo | 1.30 GB | ≈1.3 GB |
| medium.en | 1.24 GB | ≈1.2 GB |
| small.en | 0.57 GB | ≈0.7 GB |

### One copy at a time

Launching the app while it is already running brings up the running copy's
settings instead of starting a second one (`--models` opens the Models section).
The engine holds its own single-instance lock too: two engines would install
two keyboard hooks, and both would record and type every take.

### Security

The engine's WebSocket accepts connections only from the app's own windows.
Browsers do not apply the same-origin policy to WebSockets, so without an
Origin check any web page could connect to `127.0.0.1:8765`, read dictation as
it happens and send commands. Clients that send no Origin, such as local
tools, are still allowed: a local process could read the keyboard directly
anyway.

## Live transcript

The overlay shows words as you speak them. Whisper has no streaming mode, so
this re-runs it over the whole buffer captured so far, every 420 ms, and shows
the newest result. That is quadratic work in principle, but the constant is
small enough not to matter here:

| Buffer length | Partial pass |
|---|---:|
| 1.3 s | 78 ms |
| 4.4 s | 99 ms |
| 8.7 s | 114 ms |
| 13.1 s | 130 ms |

Even at the longest, a pass costs under a third of the interval, so the card
spends most of its time idle. Partials and the final pass share one lock: they
are the same model on the same GPU stream.

Two decisions make it read as solid rather than noisy:

**Partials are never typed.** They are display-only; the text that lands in your
editor is always the single full-quality pass taken after you release the key.
A wrong guess mid-sentence therefore costs nothing, which is what allows the
partials to be fast and greedy.

**Stable and tentative text are shown differently.** Re-transcribing a growing
buffer makes the tail churn as context arrives — *"wherever you're cursing"*
becomes *"wherever your cursor is"* once the next word lands. Words that
survived the previous pass unchanged render bright; the rest renders dim. That
is the LocalAgreement idea from the whisper-streaming literature, at its
simplest.

The comparison runs on normalised tokens, split on hyphens as well as spaces.
Without that, Whisper flip-flopping between *"push to talk"* and
*"push-to-talk"* reads as three words becoming one, which invalidates the whole
rest of the sentence and makes the entire line flicker between bright and dim
on every pass.

Turn it off with `streaming.enabled` if you would rather keep the GPU quiet.

## Measured memory

Working set, idle in the tray, model loaded:

| Component | Resident |
|---|---:|
| Shell — `prompt-maxxer.exe` + 6 WebView2 processes | 341 MB |
| Engine — model warm | 725 MB |
| **Total** | **~1.07 GB** |

Worth stating plainly, because it contradicts the usual pitch for Tauri: **the
shell is not lightweight in RAM.** WebView2 spawns a full browser process tree
per app, and 341 MB is what that costs. Tauri's real, verified win here is that
the shell itself adds only a few megabytes to the download.

Creating the settings window lazily instead of at startup was worth 78 MB
(419 → 341 MB): a hidden window still holds a live renderer. The remaining
processes belong to the always-visible overlay and cannot be reclaimed without
dropping WebView2 for the pill entirely, which is the obvious next lever if
this matters.

### Whisper hallucinates on silence

Recording a quiet room and running it through the model reliably produced
`"Howdy, howdy."` — stated with full confidence, and VAD did not catch it. In a
dictation app that failure mode is worse than useless, because the text gets
typed into whatever you had focused.

So an RMS gate runs before the model: anything below `audio.min_rms` is dropped
without a forward pass. Speech sits around 0.02–0.2 RMS and room tone under
0.002, so the 0.006 default has a wide margin. It is also strictly cheaper than
the inference it avoids.

---

## Build from source

For working on the app itself. Needs Python 3.12, Node.js with pnpm, Rust, and
Visual Studio Build Tools with the C++ workload.

### Build the installer

```powershell
.\build.ps1
```

This creates the engine's virtualenv if needed, freezes the engine with
PyInstaller, and builds the installer at
`shell\src-tauri\target\release\bundle\nsis\`. Add `-SkipEngine` to reuse an
already-frozen engine when only the shell changed.

Every dependency is installed at the exact version it was tested with:
`engine\requirements.lock` for Python, `shell\pnpm-lock.yaml` for the frontend,
and `shell\src-tauri\Cargo.lock` for Rust. To move a Python library forward,
upgrade it in the virtualenv, run the tests, then regenerate the lockfile as its
header describes.

If the update signing key is in a `signing` folder beside the checkout
(`..\signing\updater.key`), at `%USERPROFILE%\.prompt-maxxer\updater.key`, or in
`TAURI_SIGNING_PRIVATE_KEY`, the installer is signed for updates. Without
it the build still succeeds, but the installer cannot be published as an
update.

### Develop

```powershell
cd engine
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[cuda]"

cd ..\shell
pnpm install
pnpm tauri build
cd ..
.\run.ps1
```

Run from the repository, the shell uses the engine's virtualenv directly, so
engine changes take effect on the next launch without re-freezing. `run.ps1
-Console` runs the engine in the foreground with its log visible.

### How the engine gets started

The shell looks, in order, at `PROMPT_MAXXER_ENGINE_DIR`, the engine virtualenv
in a source checkout, and finally the frozen `engine\prompt-maxxer-engine.exe`
an installed copy carries. If something is already listening on port 8765 it
attaches to that instead of starting another.

The engine runs inside a Win32 **job object**, so it cannot outlive the shell —
including when the shell is force-killed. That matters more than it sounds: an
orphaned engine keeps a global keyboard hook installed, which would swallow the
push-to-talk key system-wide with no interface left to stop it.

Right Ctrl is the default key because, unlike Right Alt, it is not AltGr — so
swallowing it does not break international keyboard layouts.

### Tests

```powershell
cd engine
.\.venv\Scripts\python.exe test_controls.py    # hotkey, tape log, models, origin lock (stop the app first)
.\.venv\Scripts\python.exe test_first_run.py   # first launch on a machine without CUDA
.\.venv\Scripts\python.exe test_spectrum.py    # visualizer bands on tones and speech
.\.venv\Scripts\python.exe test_inject.py      # text injection into a real edit control
.\.venv\Scripts\python.exe test_runtime_cleanup.py  # old GPU library sets are removed, nothing else
.\.venv\Scripts\python.exe test_gpu_runtime.py      # downloads the GPU libraries for real (~1.4 GB)
cd ..\shell\src-tauri
cargo test --lib                               # Start with Windows, update signature checks
```

### Releasing

Installed copies update from
[`tanujarun/prompt-maxxer-releases`](https://github.com/tanujarun/prompt-maxxer-releases),
a public repository that holds only installers. This source repository stays
private: the app downloads updates without signing in to GitHub, which a
private repository would not allow.

1. Raise the version in `shell/src-tauri/tauri.conf.json`,
   `shell/src-tauri/Cargo.toml` and `shell/package.json`. Installed copies only
   move to a higher version.
2. Commit, then run `.\build.ps1`.
3. Run `.\release.ps1 -Notes "What changed"`. It uploads the installer and the
   `latest.json` the app checks, and tags the commit here.

Updates are signed with a minisign key. The public half is in
`tauri.conf.json`; the private half lives outside the repository, in a
`signing` folder beside the checkout or at `%USERPROFILE%\.prompt-maxxer\updater.key`.

> **Back the private key up somewhere safe, such as a password manager.** Every
> installed copy trusts only that key. If it is lost, no future update can be
> signed, and everyone has to download and reinstall the app by hand. If it
> leaks, generate a new one with `pnpm tauri signer generate`, put the new
> public key in `tauri.conf.json`, and ship that release before retiring the
> old key.

### Layout

```
build.ps1          one command from checkout to installer
release.ps1        publish a built installer as an update
run.ps1            launch a source build
engine/
  prompt_maxxer/
    hotkey.py      WH_KEYBOARD_LL hook: enqueue only, never block the callback
    keys.py        key names, and which keys may be the push-to-talk key
    audio.py       WASAPI stream, opened once at boot and never closed
    spectrum.py    voice bands for the visualizer
    asr/           swappable recognisers behind one narrow interface
    cleanup.py     filler/punctuation stage — the LLM slot
    inject.py      SendInput for short text, clipboard paste for long
    ipc.py         event stream and commands between engine and shell
    pipeline.py    the loop everything else serves
    system.py      GPU, VRAM, CPU and memory detection
    models.py      model catalog, fit, recommendation, pinned revisions, downloads
    cuda_runtime.py  first-launch download of the pinned NVIDIA libraries
    cuda_paths.py  makes the CUDA libraries visible to CTranslate2
  packaging/       PyInstaller spec for the standalone engine
  requirements.lock  exact Python dependency versions for builds
shell/
  src/overlay.*    the always-on-top pill
  src/settings.*   the settings window
  src/scope.ts     the voice-band visualizer
  src/models.ts    the Models section
  src/theme-pref.ts  Auto / Light / Dark, shared across windows
  src-tauri/       tray, windows, engine supervision, startup, installer
    src/updater.rs   background update checks, staging, install on restart
    installer-hooks.nsh  installer clean-up of downloaded libraries
  branding/        icon source
```

### Configuration

Everything settings does not expose lives in
`%APPDATA%\Prompt Maxxer\config.json`: microphone (`audio.device`), dictation
language (`asr.language`, `null` to autodetect), the silence gate
(`audio.min_rms`), live transcript timing (`streaming`), and injection
behaviour (`inject`).

## Known constraints

* **Unsigned installer.** SmartScreen warns on first install until the app is
  code-signed. (Updates are signed separately, with the update key, and are
  verified by the app itself.)
* **Installers are public.** Anyone with the link to the releases repository
  can download the app, though not its source.
* **Keyboard keys only.** Mouse side buttons cannot be the push-to-talk key yet;
  that needs a low-level mouse hook.
* **Downloads cannot be cancelled** once started.
* **Elevated windows.** Windows will not deliver keystrokes from an elevated
  window to a non-elevated hook. To dictate into an admin terminal, Prompt
  Maxxer has to run elevated too.
* **Recognition is not tuned to your vocabulary.** Names and jargon are often
  misheard — exactly the gap a personal dictionary fills.
* **The hotkey is swallowed globally.** While Prompt Maxxer runs, the
  push-to-talk key does not reach other applications. Set `hotkey.suppress` to
  `false` if you need it back, at the cost of the key also doing its normal job
  while dictating.
* **Single utterance at a time.** Transcription is serialised so two fast
  utterances cannot interleave their injected text.

## Roadmap

- [ ] Cleanup LLM in the stage that is already stubbed for it
- [ ] Personal dictionary, seeded by corrections
- [ ] Command mode: edit selected text by voice
- [ ] Parakeet TDT backend for English-only (better WER, faster)
- [ ] Code signing, so SmartScreen stops warning
- [ ] Native overlay instead of a WebView2 window, if the ~340 MB shell matters
