"""Wenyi command-line package for local workflows.

``wenyi_cli.cli`` assembles commands that use the shared core and file storage.
The package exposes the ``wenyi`` console script and a Python module entry point.
"""

from .main import app, main

__all__ = ["app", "main"]
