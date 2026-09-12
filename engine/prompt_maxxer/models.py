"""The speech models the app can run, how well each suits this machine, and
getting them onto disk.

Every model listed is a faster-whisper (CTranslate2) conversion, so any of them
drops into the existing engine unchanged. VRAM and RAM figures are estimates for
int8_float16 on a GPU and int8 on a CPU. Download sizes are estimates until a
model is on disk, then they are its real size.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .system import SystemInfo

log = logging.getLogger(__name__)

# The files faster-whisper itself fetches for a model (utils.download_model).
ALLOW_PATTERNS = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]

# Left free on the GPU for the desktop, browsers and anything else using it.
VRAM_HEADROOM_GB = 1.0

GOOD = "good"
SLOW = "slow"
TOO_BIG = "too_big"
ENGLISH_ONLY = "english_only"

ALIASES = {"turbo": "large-v3-turbo", "large": "large-v3"}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    params_m: int
    size_gb: float
    english_only: bool
    accuracy: int  # 1-5, relative within this list
    speed: int  # 1-5 on a CUDA GPU, relative within this list
    blurb: str

    @property
    def vram_gb(self) -> float:
        """Weights at roughly half their fp16 size, plus working memory.

        Checked on an RTX 4080 SUPER while transcribing, CUDA context included:
        large-v3-turbo 1.30 GB (estimate 1.3), medium.en 1.24 GB (1.2),
        small.en 0.57 GB (0.7). Close for large models, slightly high for small.
        """
        return round(self.size_gb * 0.55 + 0.4, 1)

    @property
    def ram_gb(self) -> float:
        """Approximate resident memory running int8 on the CPU."""
        return round(self.size_gb * 0.6 + 0.5, 1)


CATALOG: list[ModelSpec] = [
    ModelSpec("large-v3-turbo", 809, 1.62, False, 5, 4,
              "Large-v3 accuracy with a 4-layer decoder. The dictation sweet spot on a GPU."),
    ModelSpec("large-v3", 1550, 3.09, False, 5, 2,
              "The most accurate Whisper. Noticeably slower to return text."),
    ModelSpec("distil-large-v3.5", 756, 1.51, True, 4, 4,
              "Distilled from large-v3 for English. Fast, English only."),
    ModelSpec("distil-large-v3", 756, 1.51, True, 4, 4,
              "The earlier distilled large-v3 for English."),
    ModelSpec("medium.en", 769, 1.53, True, 4, 3,
              "Solid English accuracy, with a full-size decoder that slows it down."),
    ModelSpec("medium", 769, 1.53, False, 4, 3, "Multilingual medium."),
    ModelSpec("distil-medium.en", 394, 0.79, True, 3, 4, "Fast English model for mid-range GPUs."),
    ModelSpec("small.en", 244, 0.48, True, 3, 5, "Light and quick. A good CPU choice."),
    ModelSpec("small", 244, 0.48, False, 3, 5, "Multilingual small."),
    ModelSpec("distil-small.en", 166, 0.34, True, 2, 5, "Very fast English model for modest hardware."),
    ModelSpec("base.en", 74, 0.15, True, 2, 5, "Runs on almost anything. Misses more words."),
    ModelSpec("base", 74, 0.15, False, 2, 5, "Multilingual base."),
    ModelSpec("tiny.en", 39, 0.08, True, 1, 5, "Smallest English model, for very weak machines."),
    ModelSpec("tiny", 39, 0.08, False, 1, 5, "Smallest multilingual model."),
]

_BY_ID = {spec.id: spec for spec in CATALOG}

#: The exact Hugging Face revision of every model, so a model can never change
#: under the app. Without these, each download takes whatever is newest on the
#: repository's main branch, and an upstream change - a re-conversion, a new
#: tokenizer - would reach every machine unannounced. Update deliberately, and
#: test, by bumping a hash.
REVISIONS = {
    "large-v3-turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    "large-v3": "edaa852ec7e145841d8ffdb056a99866b5f0a478",
    "distil-large-v3.5": "9793ccc07920e0f830e1dba0343efcdf0ef8c903",
    "distil-large-v3": "c3058b475261292e64a0412df1d2681c06260fab",
    "medium.en": "a29b04bd15381511a9af671baec01072039215e3",
    "medium": "08e178d48790749d25932bbc082711ddcfdfbc4f",
    "distil-medium.en": "80ddfce281f77766d8943d63109199fc8145dfa5",
    "small.en": "d1d751a5f8271d482d14ca55d9e2deeebbae577f",
    "small": "536b0662742c02347bc0e980a01041f333bce120",
    "distil-small.en": "ef77d90526ccd62cde3808ee70626a01e5cf83e4",
    "base.en": "3d3d5dee26484f91867d81cb899cfcf72b96be6c",
    "base": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
    "tiny.en": "0d3d19a32d3338f10357c0889762bd8d64bbdeba",
    "tiny": "d90ca5fe260221311c53c58e660288d3deb8d356",
}


def revision_for(model_id: str) -> str | None:
    """The pinned revision for a catalog model, or None for anything else."""
    return REVISIONS.get(canonical(model_id))


def canonical(model_id: str) -> str:
    return ALIASES.get(model_id, model_id)


def spec_for(model_id: str) -> ModelSpec | None:
    return _BY_ID.get(canonical(model_id))


# -- disk ------------------------------------------------------------------


def repo_for(model_id: str) -> str:
    from faster_whisper.utils import _MODELS

    return _MODELS[canonical(model_id)]


def _repo_dir(repo: str) -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))


def _repo_bytes(repo_dir: Path) -> int:
    """Bytes of model data on disk for a cached repository.

    Counts regular files anywhere under the repository folder. With symlinks,
    Hugging Face keeps the data in blobs/ and snapshots/ holds links to it; on
    Windows without symlink support (Developer Mode off) there is no blobs/ at
    all and the files sit in snapshots/ directly. Skipping links and counting
    regular files is right for both layouts, and includes the .incomplete file
    of a download in progress.
    """
    total = 0
    for root, _dirs, files in os.walk(repo_dir):
        for name in files:
            path = Path(root) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                pass
    return total


def is_downloaded(model_id: str) -> bool:
    from huggingface_hub import try_to_load_from_cache

    try:
        cached = try_to_load_from_cache(repo_for(model_id), "model.bin", revision=revision_for(model_id))
    except KeyError:
        return False
    return isinstance(cached, str)


def disk_size_gb(model_id: str) -> float | None:
    size = _repo_bytes(_repo_dir(repo_for(model_id)))
    return round(size / 1e9, 2) if size else None


# -- fit and recommendation -----------------------------------------------------


def _gb(value: float) -> str:
    return f"{value:g}"


def assess(spec: ModelSpec, info: SystemInfo, language: str | None) -> tuple[str, str]:
    """How a model suits this machine: a fit class and a short note."""
    if spec.english_only and language not in (None, "en"):
        return ENGLISH_ONLY, "English only"

    if info.cuda:
        if info.vram_gb is not None and spec.vram_gb > info.vram_gb - VRAM_HEADROOM_GB:
            return TOO_BIG, f"Needs ≈{_gb(spec.vram_gb)} GB VRAM"
        # On a GPU with room for it, a slower model is still a good fit: it is
        # the model that is slower, not this machine struggling with it.
        slower = " · slower" if spec.speed <= 2 else ""
        return GOOD, f"≈{_gb(spec.vram_gb)} GB VRAM{slower}"

    if spec.ram_gb > info.ram_gb * 0.5:
        return TOO_BIG, f"Needs ≈{_gb(spec.ram_gb)} GB RAM"
    if spec.params_m >= 700 or (spec.params_m >= 240 and info.threads < 8):
        return SLOW, "Slow on this CPU"
    return GOOD, "Runs on the CPU"


def recommend(info: SystemInfo, language: str | None) -> tuple[str, str]:
    """The best model for this machine, and why, in a sentence."""
    english = language == "en"
    vram = info.vram_gb

    if info.cuda and (vram is None or vram >= 3):
        gpu = info.gpu or "CUDA GPU"
        memory = f" has {_gb(vram)} GB of VRAM" if vram is not None else " is CUDA-capable"
        need = spec_for("large-v3-turbo").vram_gb
        return (
            "large-v3-turbo",
            f"Your {gpu}{memory}. Turbo gives large-v3 accuracy at dictation speed "
            f"and needs about {_gb(need)} GB of it.",
        )
    if info.cuda and vram >= 2:
        pick = "distil-large-v3.5" if english else "small"
        return pick, f"With {_gb(vram)} GB of VRAM this is the most accurate model that still leaves room to spare."
    if info.cuda:
        pick = "distil-small.en" if english else "base"
        return pick, f"Your GPU has {_gb(vram)} GB of VRAM, so a compact model keeps dictation responsive."

    if info.threads >= 12 and info.ram_gb >= 8:
        pick = "small.en" if english else "small"
        why = f"No CUDA GPU, but {info.threads} CPU threads handle a small model at usable speed."
    elif info.threads >= 4:
        pick = "base.en" if english else "base"
        why = "No CUDA GPU, so a light model keeps each take from lagging on the CPU."
    else:
        pick = "tiny.en" if english else "tiny"
        why = "This machine is modest, so the smallest model is the one that will feel responsive."
    return pick, why


def catalog(
    info: SystemInfo, language: str | None, active: str, downloading: str | None = None
) -> dict:
    recommended, reason = recommend(info, language)
    rows = []
    for spec in CATALOG:
        downloaded = is_downloaded(spec.id)
        size = disk_size_gb(spec.id) if downloaded else None
        fit, note = assess(spec, info, language)
        rows.append(
            {
                "id": spec.id,
                "params_m": spec.params_m,
                "size_gb": size if size is not None else spec.size_gb,
                "size_exact": size is not None,
                "english_only": spec.english_only,
                "accuracy": spec.accuracy,
                "speed": spec.speed,
                "vram_gb": spec.vram_gb,
                "downloaded": downloaded,
                "fit": fit,
                "note": note,
                "blurb": spec.blurb,
            }
        )
    return {
        "system": info.to_dict(),
        "active": canonical(active),
        "recommended": recommended,
        "reason": reason,
        "downloading": downloading,
        "models": rows,
    }


# -- downloading -----------------------------------------------------------------


class Downloader:
    """One model download at a time.

    Progress is read off the cache directory rather than from Hugging Face's
    progress bars, which report per file and differ between transfer backends.
    Bytes on disk against the repository's published file sizes is the number
    a person actually wants.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: str | None = None

    def start(
        self,
        model_id: str,
        on_progress: Callable[[int, int | None], None],
        on_done: Callable[[Exception | None], None],
    ) -> bool:
        with self._lock:
            if self.current is not None:
                return False
            self.current = canonical(model_id)
        threading.Thread(
            target=self._run,
            args=(canonical(model_id), on_progress, on_done),
            name="prompt-maxxer-download",
            daemon=True,
        ).start()
        return True

    def _run(self, model_id, on_progress, on_done) -> None:
        from huggingface_hub import HfApi, constants, snapshot_download

        # Plain HTTP rather than the Xet transfer backend. Xet assembles each
        # file from a chunk cache and writes it out only at the end, so there
        # is nothing on disk to measure and progress would sit at zero until
        # the download was already over. HTTP writes the file as it arrives.
        constants.HF_HUB_DISABLE_XET = True

        error: Exception | None = None
        repo = repo_for(model_id)
        revision = revision_for(model_id)
        repo_dir = _repo_dir(repo)
        stop = threading.Event()

        total: int | None = None
        try:
            info = HfApi().model_info(repo, revision=revision, files_metadata=True)
            total = sum(
                sibling.size or 0
                for sibling in info.siblings or []
                if any(fnmatch.fnmatch(sibling.rfilename, p) for p in ALLOW_PATTERNS)
            ) or None
        except Exception:
            log.debug("could not read file sizes for %s", repo, exc_info=True)

        def poll() -> None:
            while not stop.wait(0.25):
                on_progress(_repo_bytes(repo_dir), total)

        poller = threading.Thread(target=poll, name="prompt-maxxer-download-poll", daemon=True)
        poller.start()
        log.info("downloading %s from %s (%s bytes)", model_id, repo, total)
        try:
            snapshot_download(repo, revision=revision, allow_patterns=ALLOW_PATTERNS)
        except Exception as exc:
            log.exception("download of %s failed", model_id)
            error = exc
        finally:
            stop.set()
            poller.join(timeout=1)
            with self._lock:
                self.current = None

        if error is None:
            on_progress(_repo_bytes(repo_dir), total)
        on_done(error)
