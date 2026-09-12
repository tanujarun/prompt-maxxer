"""First launch on a machine unlike this one.

Pretends to be a laptop with no CUDA GPU, starts the engine against a brand-new
config, and checks that it picks a CPU-appropriate model instead of the stock
GPU default, downloads it with progress before loading, saves the choice, and
becomes ready with it.

Uses a throwaway config and its own port, and removes the model it downloads
afterwards if it was not already on disk. Stop the real app first: two keyboard
hooks would both react.
"""

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

from websockets.sync.client import connect

import prompt_maxxer  # noqa: F401
from prompt_maxxer import models, system
from prompt_maxxer.config import Config
from prompt_maxxer.pipeline import Pipeline

PORT = 8798
LAPTOP = system.SystemInfo(
    cpu="Test Laptop CPU", threads=8, ram_gb=16.0, gpu=None, vram_gb=None, cuda=False,
)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    expected, reason = models.recommend(LAPTOP, "en")
    already_on_disk = models.is_downloaded(expected)
    print(f"a {LAPTOP.threads}-thread laptop without CUDA should get {expected}: {reason}")
    print(f"{expected} already on disk: {already_on_disk}")

    cfg_path = Path(tempfile.mkdtemp(prefix="prompt-maxxer-first-run-")) / "config.json"
    cfg = Config.load(cfg_path)
    check("a new config is flagged as a first run", cfg.first_run)
    check("the stock default is the GPU model", cfg.asr.model == "large-v3-turbo" and cfg.asr.device == "cuda")
    cfg.ipc.port = PORT

    system.detect = lambda: LAPTOP  # the pipeline reads hardware through this
    pipeline = Pipeline(cfg, config_path=cfg_path, first_run=True)

    events: list[dict] = []
    stop = threading.Event()

    def listen() -> None:
        for _ in range(100):  # the event bus starts a moment after start()
            try:
                ws = connect(f"ws://127.0.0.1:{PORT}")
                break
            except OSError:
                time.sleep(0.05)
        else:
            return
        with ws:
            while not stop.is_set():
                try:
                    events.append(json.loads(ws.recv(timeout=0.5)))
                except TimeoutError:
                    continue
                except Exception:
                    return

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()

    started = time.monotonic()
    pipeline.start()
    elapsed = time.monotonic() - started
    time.sleep(1.0)
    stop.set()
    listener.join(timeout=2)

    try:
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))["asr"]
        check("picks the recommended model and saves it", saved["model"] == expected, saved["model"])
        check("places it on the CPU", saved["device"] == "cpu" and saved["compute_type"] == "int8",
              f"{saved['device']} / {saved['compute_type']}")

        downloads = [e for e in events if e.get("event") == "model_download" and e.get("model") == expected]
        if already_on_disk:
            print("   (model was already on disk, so no download was expected)")
        else:
            sizes = sorted({e.get("done_bytes") or 0 for e in downloads if e["status"] == "downloading"})
            check("downloads it before loading, with progress", len(sizes) >= 2, f"{len(sizes)} distinct sizes")
            check("reports the download finished", any(e["status"] == "done" for e in downloads))

        ready = next((e for e in events if e.get("event") == "ready"), None)
        check("becomes ready with that model on the CPU",
              ready and ready["model"] == expected and ready["device"] == "cpu", str(ready))
        print(f"   start took {elapsed:.1f}s")
    finally:
        pipeline.stop()
        if not already_on_disk and models.is_downloaded(expected):
            repo_dir = models._repo_dir(models.repo_for(expected))
            shutil.rmtree(repo_dir, ignore_errors=True)
            print(f"   removed the test download of {expected}")

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
