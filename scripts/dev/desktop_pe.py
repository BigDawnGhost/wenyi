"""Require the built native Windows application to use the GUI PE subsystem."""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import BinaryIO


def _read_at(stream: BinaryIO, offset: int, size: int, label: str) -> bytes:
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size:
        raise ValueError(f"Truncated {label}: expected {size} bytes at offset {offset}")
    return data


def verify_gui_subsystem(binary: Path) -> None:
    """Read DOS, COFF and PE32/PE32+ headers, not an installer filename or stub."""
    with binary.open("rb") as stream:
        dos = _read_at(stream, 0, 64, "DOS header")
        if dos[:2] != b"MZ":
            raise ValueError("Invalid DOS signature: expected MZ")
        pe_offset = struct.unpack_from("<I", dos, 60)[0]
        if pe_offset < 64:
            raise ValueError("Invalid PE offset: overlaps DOS header")
        coff = _read_at(stream, pe_offset, 24, "PE signature and COFF header")
        if coff[:4] != b"PE\0\0":
            raise ValueError("Invalid PE signature: expected PE\\0\\0")
        sections = struct.unpack_from("<H", coff, 6)[0]
        optional_size, characteristics = struct.unpack_from("<HH", coff, 20)
        if not characteristics & 0x0002:
            raise ValueError("PE is not an executable image")
        if sections == 0:
            raise ValueError("PE executable has no sections")
        if optional_size < 2:
            raise ValueError("Invalid optional header size")
        optional = _read_at(stream, pe_offset + 24, optional_size, "PE optional header")
        magic = struct.unpack_from("<H", optional)[0]
        minimum_size = {0x10B: 96, 0x20B: 112}.get(magic)
        if minimum_size is None:
            raise ValueError(f"Unsupported PE optional header magic: 0x{magic:04x}")
        if optional_size < minimum_size:
            raise ValueError(f"Invalid optional header size: expected at least {minimum_size}")
        directory_offset = minimum_size - 4
        directories = struct.unpack_from("<I", optional, directory_offset)[0]
        if minimum_size + directories * 8 > optional_size:
            raise ValueError("Invalid optional header size: data directories exceed header")
        _read_at(stream, pe_offset + 24 + optional_size, sections * 40, "PE section table")
        subsystem = struct.unpack_from("<H", optional, 68)[0]
        if subsystem != 2:
            actual = "Windows CUI=3" if subsystem == 3 else f"subsystem {subsystem}"
            raise ValueError(f"Expected Windows GUI=2, found {actual}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, help="Built native wenyi-desktop.exe (not NSIS)")
    args = parser.parse_args()
    try:
        verify_gui_subsystem(args.binary)
    except (OSError, ValueError) as error:
        print(f"{args.binary}: {error}", file=sys.stderr)
        return 1
    print(f"{args.binary}: PE subsystem Windows GUI=2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
