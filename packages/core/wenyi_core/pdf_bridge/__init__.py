"""HTTP client for the external Wenyi BabelDOC bridge (AGPL service).

This package must never import babeldoc / pdf2zh. It only talks HTTP.
"""

from .client import BabeldocBridgeClient, BabeldocBridgeError

__all__ = ["BabeldocBridgeClient", "BabeldocBridgeError"]
