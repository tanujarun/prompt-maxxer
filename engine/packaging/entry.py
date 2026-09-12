"""Entry point for the standalone engine built by PyInstaller.

PyInstaller needs a plain script to start from; the package's own __main__
uses relative imports, which only work when run as `python -m prompt_maxxer`.
"""

import sys

from prompt_maxxer.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
