"""PyInstaller entry point for the isolated desktop backend."""

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # The onedir resource must take precedence over any host cache setting.
    os.environ["TIKTOKEN_CACHE_DIR"] = str(Path(sys._MEIPASS) / "tiktoken_cache")

from wenyi_desktop.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
