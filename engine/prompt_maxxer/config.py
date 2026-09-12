"""User-facing configuration, persisted to %APPDATA%/Prompt Maxxer/config.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import ClassVar


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Prompt Maxxer"


# Earlier names of the app. A config found under one of these is copied (not
# moved) into the current folder the first time the renamed app starts, so
# settings survive the rename and the old copy stays as a fallback.
LEGACY_DIR_NAMES = ("Sotto",)


def _migrate_legacy_config(target: Path) -> None:
    if target.exists():
        return
    for name in LEGACY_DIR_NAMES:
        legacy = target.parent.parent / name / "config.json"
        if legacy.is_file():
            import shutil

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)
            return


@dataclass
class AudioConfig:
    # Whisper wants 16 kHz mono; we resample if the device refuses that rate.
    sample_rate: int = 16000
    # 20 ms blocks keep the level meter smooth without waking the CPU too often.
    block_ms: int = 20
    device: int | str | None = None
    # Utterances shorter than this are almost always an accidental key tap.
    min_utterance_ms: int = 250
    # Whisper hallucinates confident text out of near-silence - an empty
    # room reliably produced "Howdy, howdy." - and VAD does not always
    # catch it. Anything quieter than this never reaches the model.
    # Normal speech sits around 0.02-0.2 RMS; room tone is under 0.002.
    min_rms: float = 0.006
    max_utterance_s: int = 120


@dataclass
class HotkeyConfig:
    # VK_RCONTROL. Right Ctrl is the safest default: unlike Right Alt it is not
    # AltGr, so suppressing it does not break international keyboard layouts.
    vk_code: int = 0xA3
    label: str = "Right Ctrl"
    # Swallow the key so the host app never sees it while you are dictating.
    suppress: bool = True


@dataclass
class AsrConfig:
    engine: str = "faster-whisper"
    model: str = "large-v3-turbo"
    device: str = "cuda"
    # int8_float16 is ~1.5 GB of VRAM on Ada with negligible quality loss,
    # leaving the rest of the card free for a cleanup LLM later.
    compute_type: str = "int8_float16"
    # Pinning the language skips Whisper's detect_language pass, which is a
    # whole extra encode - measured at ~70 ms of the ~180 ms budget for a
    # 3 s utterance. Set to None to autodetect if you switch languages often.
    language: str | None = "en"
    # Greedy decoding. For dictation-length audio a beam buys almost no
    # accuracy and costs real latency.
    beam_size: int = 1
    vad_filter: bool = True


@dataclass
class StreamingConfig:
    """Live partial transcripts shown in the overlay while you speak.

    Whisper is not a streaming model. This re-runs it over the whole buffer
    captured so far, every `interval_ms`, and shows the newest result. That is
    affordable here only because the model is fast: a 3 s buffer costs ~110 ms
    on this GPU, so even at 3 passes a second the card is mostly idle.

    Partials are display-only. The text that actually gets typed is always the
    single full-quality pass taken after you release the key.
    """

    enabled: bool = True
    interval_ms: int = 420
    # Whisper pads every input to 30 s, so very short clips are where it
    # hallucinates most. Waiting for a second of speech avoids opening the
    # pill with a confident wrong guess.
    min_audio_s: float = 1.0
    # Skip a pass if barely any new audio arrived since the last one, which
    # happens when the model is running slower than the interval.
    min_growth_s: float = 0.25
    # Partials favour latency over accuracy; the final pass does the opposite.
    beam_size: int = 1
    vad_filter: bool = False


@dataclass
class InjectConfig:
    # Above this length, paste instead of synthesising keystrokes: SendInput
    # is ~1 ms/char, so a long paragraph would visibly type itself out.
    clipboard_threshold: int = 120
    restore_clipboard: bool = True
    # How long to leave our text on the clipboard before putting the old
    # contents back. Ctrl+V is asynchronous: the target app reads the
    # clipboard on its own schedule, and restoring too early means it
    # pastes whatever was there before.
    restore_delay_ms: int = 450
    keystroke_delay_ms: int = 0


@dataclass
class IpcConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    # Cap overlay level frames at ~30 fps regardless of audio block rate.
    level_fps: int = 30


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    ipc: IpcConfig = field(default_factory=IpcConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    cleanup: str = "passthrough"  # passthrough | local-llm | claude

    #: True when this load created the config file: the first launch on this
    #: machine. Not saved; it only steers first-run setup.
    first_run: ClassVar[bool] = False

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        if path is None:
            path = config_dir() / "config.json"
            _migrate_legacy_config(path)
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            cfg.first_run = True
            return cfg
        raw = json.loads(path.read_text(encoding="utf-8"))
        cfg = cls(
            audio=AudioConfig(**raw.get("audio", {})),
            hotkey=HotkeyConfig(**raw.get("hotkey", {})),
            asr=AsrConfig(**raw.get("asr", {})),
            inject=InjectConfig(**raw.get("inject", {})),
            ipc=IpcConfig(**raw.get("ipc", {})),
            streaming=StreamingConfig(**raw.get("streaming", {})),
            cleanup=raw.get("cleanup", "passthrough"),
        )
        cfg.first_run = False
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or (config_dir() / "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
