"""Prompt Maxxer - push-to-talk dictation engine."""

import os as _os

# Hugging Face warns on every start when Windows cannot create symlinks
# (Developer Mode off). The cache works fine without them, and nobody using the
# app can act on the warning.
_os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from . import cuda_paths as _cuda_paths  # noqa: E402

# Must happen before faster_whisper/ctranslate2 is imported anywhere.
_cuda_paths.register()

APP_NAME = "Prompt Maxxer"
__version__ = "0.1.0"
