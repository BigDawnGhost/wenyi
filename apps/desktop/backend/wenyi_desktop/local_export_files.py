"""Read Desktop exports without following links, including concurrent path swaps."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from io import BufferedReader
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Callable, Iterator


class ExportLease(BufferedReader):
    """Release an export lease exactly once, after its file handle is closed."""

    def __init__(self, stream: BinaryIO, release: Callable[[], None]):
        self._close_lock = Lock()
        self._release: Callable[[], None] | None = release
        super().__init__(stream)

    def close(self) -> None:
        with self._close_lock:
            try:
                super().close()
            finally:
                if self.closed and self._release is not None:
                    release, self._release = self._release, None
                    release()


def _component(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or any(char in name for char in "/\\:")
        or any(ord(char) < 32 for char in name)
    ):
        raise ValueError("Unsafe export path component")


def _windows_handle(path: Path, *, directory: bool) -> int:
    """Pin one component without delete sharing; never follow reparse points."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(
        str(path),
        0x80 if directory else 0x80000000,  # FILE_READ_ATTRIBUTES / GENERIC_READ
        0x3,  # Share reads/writes, but not deletion or renaming.
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x02000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    info = kernel.GetFileInformationByHandleEx
    info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    info.restype = wintypes.BOOL
    attributes = AttributeTag()
    try:
        if not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise ValueError("Export contains a reparse point")
        if bool(attributes.attributes & 0x10) != directory:
            raise ValueError("Export contains an unexpected file type")
        return handle
    except BaseException:
        _close_windows_handle(handle)
        raise


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    close(handle)


class _Directory:
    """An anchored directory: an openat descriptor or a Windows rename-blocking handle."""

    def __init__(self, path: Path, parent: _Directory | None = None):
        self.path = path
        # Trusted absolute ancestors need not obey portable asset-name rules.
        if parent is not None and path.name in {"", ".", ".."}:
            raise ValueError("Invalid directory component")
        if os.name == "nt":
            self.handle = _windows_handle(path, directory=True)
        else:
            self.handle = os.open(
                path.name if parent else path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent.handle if parent else None,
            )

    def close(self) -> None:
        if os.name == "nt":
            _close_windows_handle(self.handle)
        else:
            os.close(self.handle)

    def names(self) -> list[str]:
        return sorted(os.listdir(self.path if os.name == "nt" else self.handle))

    def metadata(self, name: str) -> os.stat_result:
        _component(name)
        if os.name == "nt":
            return (self.path / name).lstat()
        return os.stat(name, dir_fd=self.handle, follow_symlinks=False)

    def open_file(self, name: str) -> BinaryIO:
        _component(name)
        if os.name == "nt":
            import msvcrt

            handle = _windows_handle(self.path / name, directory=False)
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                _close_windows_handle(handle)
                raise
        else:
            # Nonblocking avoids hanging if a regular file was replaced by a FIFO.
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.handle)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Export contains a non-regular or multiply linked file")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise


@contextmanager
def _root(path: Path) -> Iterator[_Directory]:
    """Pin each ancestor before looking up the next component."""
    path = path.absolute()
    with ExitStack() as stack:
        directory = _Directory(Path(path.anchor))
        stack.callback(directory.close)
        for part in path.parts[1:]:
            directory = _Directory(directory.path / part, directory)
            stack.callback(directory.close)
        yield directory


def open_regular(file: Path) -> BinaryIO:
    with _root(file.parent) as directory:
        return directory.open_file(file.name)


def _read_file(directory: _Directory, name: str) -> bytes:
    with directory.open_file(name) as source:
        before = os.fstat(source.fileno())
        payload = source.read()
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Export changed while reading")
        return payload


def _asset_resources(
    directory: _Directory, prefix: str, depth: int = 0
) -> Iterator[tuple[str, bytes]]:
    if depth > 64:
        raise ValueError("Export assets are nested too deeply")
    for name in directory.names():
        metadata = directory.metadata(name)
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("Export assets contain a link")
        if stat.S_ISDIR(metadata.st_mode):
            child = _Directory(directory.path / name, directory)
            try:
                yield from _asset_resources(child, f"{prefix}/{name}", depth + 1)
            finally:
                child.close()
        elif stat.S_ISREG(metadata.st_mode):
            yield f"{prefix}/{name}", _read_file(directory, name)
        else:
            raise ValueError("Export assets contain a non-regular file")


def html_document(file: Path) -> BinaryIO:
    """Convert legacy HTML-plus-assets history to one temporary, self-contained HTML.

    Reuse the pinned-directory inventory so conversion cannot read unrelated files,
    symlinks or resources swapped outside the published export.
    """
    from wenyi_core.assemble.html_resources import _embed_html_resources

    output = tempfile.TemporaryFile()
    try:
        with _root(file.parent) as directory:
            html = _read_file(directory, file.name).decode("utf-8")
            name = f"{file.stem}.assets"
            try:
                child = _Directory(directory.path / name, directory)
            except FileNotFoundError:
                resources = {}
            else:
                try:
                    resources = dict(_asset_resources(child, name))
                finally:
                    child.close()
            rendered = _embed_html_resources(html, source_dir="", zip_resources=resources)
        output.write(rendered.encode("utf-8"))
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
