"""PyInstaller entry point for the isolated desktop backend."""

import multiprocessing
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    # Divert frozen worker/resource-tracker invocations before importing the application.
    multiprocessing.freeze_support()
    if getattr(sys, "frozen", False):
        # The onedir resource must take precedence over any host cache setting.
        os.environ["TIKTOKEN_CACHE_DIR"] = str(Path(sys._MEIPASS) / "tiktoken_cache")
    from wenyi_desktop.__main__ import main

    main()
