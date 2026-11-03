"""Print the shared HTTP schema; see `wenyi_backend.schema_export` for the logic."""

from __future__ import annotations

import json
import sys

from wenyi_backend.schema_export import build_schema

if __name__ == "__main__":
    # The schema embeds Chinese literals (default_template) that the TypeScript
    # generator has to receive byte for byte. Windows picks a legacy console
    # codepage for stdout, so pin UTF-8 here rather than relying on the caller's
    # environment: a mojibake contract still compares equal to itself and only
    # fails on a host whose stdout already is UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(build_schema(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
