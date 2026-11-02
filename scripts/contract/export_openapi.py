"""Print the shared HTTP schema; see `wenyi_backend.schema_export` for the logic."""

from __future__ import annotations

import json
import sys

from wenyi_backend.schema_export import build_schema

if __name__ == "__main__":
    json.dump(build_schema(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
