"""基于本地文件系统的不可变内容寻址产物存储。

写入流程先流式计算 SHA-256，再把数据写入最终目录中的唯一临时文件。临时文件
完成 ``flush``/``fsync`` 并关闭后，才会在跨线程、跨进程锁内以“不覆盖”方式
发布。正式对象一经出现便不会被改写；已有对象必须重新校验后才能幂等复用。
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Iterator

from ..domain.workflow import ArtifactRef, copy_json_value, validate_artifact_ref
from ..workflow.repository import (
    ArtifactCorruption,
    ArtifactNotFound,
    ArtifactStoreError,
    InvalidArtifactReference,
)

_ARTIFACT_URI_PATTERN = re.compile(r"\Aartifact://sha256/([0-9a-f]{64})\Z")
_DIGEST_PREFIX_PATTERN = re.compile(r"\A[0-9a-f]{2}\Z")
_TEMP_FILE_PATTERN = re.compile(r"\A\.tmp-[^./\\]+\.part\Z")

# 固定块大小使大文件的内存成本保持常量；spool 超过阈值后由标准库自动落盘。
_STREAM_CHUNK_SIZE = 1024 * 1024
_SPOOL_MEMORY_LIMIT = 8 * 1024 * 1024

# 不同 store 实例只要指向同一规范根目录，就必须共享同一进程内锁。
_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.RLock] = {}

# 活跃锁文件也要登记：POSIX fork 会复制文件描述符，子进程若不主动关闭副本，
# 可能把父进程的 flock 生命周期意外延长。登记表只保存未关闭的无缓冲句柄。
_ACTIVE_LOCK_FILES_GUARD = threading.Lock()
_ACTIVE_LOCK_FILES: set[BinaryIO] = set()


def _thread_lock_for(root: Path) -> threading.RLock:
    """返回根目录级可重入锁，补足文件锁在同进程线程间的差异。"""
    key = _path_comparison_key(root)
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())


def _before_fork() -> None:
    """冻结两个锁注册表，使子进程拿到一份内部一致的快照。"""
    # 固定获取顺序，且普通运行路径从不反向同时获取这两个 guard。
    _ROOT_LOCKS_GUARD.acquire()
    _ACTIVE_LOCK_FILES_GUARD.acquire()


def _after_fork_in_parent() -> None:
    """父进程 fork 完成后按反序释放注册表 guard。"""
    _ACTIVE_LOCK_FILES_GUARD.release()
    _ROOT_LOCKS_GUARD.release()


def _after_fork_in_child() -> None:
    """丢弃继承的线程锁状态，并关闭继承的文件锁句柄。"""
    global _ACTIVE_LOCK_FILES_GUARD, _ROOT_LOCKS_GUARD  # noqa: PLW0603
    global _ACTIVE_LOCK_FILES, _ROOT_LOCKS  # noqa: PLW0603

    # fork 后只有调用 fork 的线程存在。其他线程持有的 RLock 无法再释放，
    # 所以不能复用缓存；活动 flock 句柄则必须在子进程关闭其副本。
    for lock_file in tuple(_ACTIVE_LOCK_FILES):
        try:
            lock_file.close()
        except (OSError, ValueError):
            pass

    _ROOT_LOCKS = {}
    _ACTIVE_LOCK_FILES = set()
    _ROOT_LOCKS_GUARD = threading.Lock()
    _ACTIVE_LOCK_FILES_GUARD = threading.Lock()


# Python 3.10 的官方 fork 回调只在控制流会返回解释器时触发；这种可见 fork
# 是本存储支持的跨 fork 契约，绕过 PyOS_* hooks 的第三方 C fork 不在范围内。
if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )


def _path_comparison_key(path: Path) -> str:
    """返回可稳定比较的真实绝对路径，并统一 Windows 扩展路径前缀。"""
    resolved = os.path.realpath(os.path.abspath(os.fspath(path)))
    if os.name == "nt":
        # pathlib/os 在目录刚被并发创建时可能在 ``D:\...`` 和
        # ``\\?\D:\...`` 之间切换；两者是同一路径，必须先归一化。
        lowered = resolved.casefold()
        if lowered.startswith("\\\\?\\unc\\"):
            resolved = "\\\\" + resolved[8:]
        elif lowered.startswith("\\\\?\\"):
            resolved = resolved[4:]
    return os.path.normcase(os.path.normpath(resolved))


class ContentAddressedArtifactStore:
    """把不可变字节保存为 ``artifact://sha256/<digest>`` 引用。

    初版针对本地、支持硬链接和字节范围锁的文件系统（Windows 下为
    NTFS）。网络共享或不支持无覆盖硬链接的文件系统会显式报错，不会
    退化为可能覆盖已有对象的写入方式。
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        lock_timeout_seconds: float = 300.0,
        lock_poll_interval_seconds: float = 0.05,
    ) -> None:
        """绑定本地根目录，并配置取得根级锁的最长等待时间。"""
        # 先验证纯参数，避免无效配置在文件系统留下半初始化目录。
        self._lock_timeout_seconds = self._validate_lock_duration(
            lock_timeout_seconds,
            field="lock_timeout_seconds",
        )
        self._lock_poll_interval_seconds = self._validate_lock_duration(
            lock_poll_interval_seconds,
            field="lock_poll_interval_seconds",
        )

        try:
            self.root = Path(root).resolve(strict=False)
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.root.is_dir():
                raise NotADirectoryError(str(self.root))
            self._objects_root = self.root / "sha256"
            self._objects_root.mkdir(exist_ok=True)
            self._assert_contained(self._objects_root)

            # 无论本进程是否创建了 sha256，都同步根目录。这样并发初始化者中
            # 只要有一个成功返回，就不会依赖另一个进程尚未落盘的目录项。
            self._sync_directory(self.root)
        except (OSError, TypeError, ValueError) as error:
            raise ArtifactStoreError(f"无法初始化产物存储根目录：{root!s}") from error

        # 锁文件位于根目录而不是某个 digest 目录，从而能保护首次建目录、发布和清理。
        self._lock_path = self.root / ".artifact-store.lock"

    def put_bytes(self, data: bytes, *, media_type: str) -> ArtifactRef:
        """发布一段不可变字节；调用方数据不会被存储实现持有。"""
        if type(data) is not bytes:
            raise TypeError("data 必须是 bytes")

        # 统一经过流式入口，保证 bytes、文件流和非 seekable 流拥有完全相同的发布语义。
        return self.put_stream(BytesIO(data), media_type=media_type)

    def put_stream(self, stream: BinaryIO, *, media_type: str) -> ArtifactRef:
        """从流的当前位置读取到 EOF；不会 seek 或关闭调用方流。"""
        normalized_media_type = self._validate_media_type(media_type)
        hasher = hashlib.sha256()
        size_bytes = 0

        # 首遍只消费输入流并计算身份。SpooledTemporaryFile 允许不可寻址流和大文件
        # 使用相同控制流，同时避免把整个 payload 保存在 Python 堆中。
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_LIMIT, mode="w+b") as spool:
            while True:
                try:
                    chunk = stream.read(_STREAM_CHUNK_SIZE)
                except (AttributeError, OSError, ValueError) as error:
                    raise ArtifactStoreError("读取产物流失败") from error
                if chunk == b"":
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("二进制流的 read() 必须返回 bytes-like 对象")

                stable_chunk = bytes(chunk)
                hasher.update(stable_chunk)
                try:
                    written = spool.write(stable_chunk)
                except (OSError, ValueError) as error:
                    raise ArtifactStoreError("暂存产物流失败") from error
                if written != len(stable_chunk):  # pragma: no cover - 标准 spool 正常情况下全写
                    raise ArtifactStoreError("产物 spool 发生短写")
                size_bytes += len(stable_chunk)

            digest = hasher.hexdigest()
            try:
                spool.seek(0)
            except (OSError, ValueError) as error:  # pragma: no cover - 依赖临时存储故障
                raise ArtifactStoreError("无法重绕产物 spool") from error
            return self._publish_spool(
                spool,
                digest=digest,
                size_bytes=size_bytes,
                media_type=normalized_media_type,
            )

    def put_json(
        self,
        value: object,
        *,
        media_type: str = "application/json",
    ) -> ArtifactRef:
        """以稳定、紧凑的 UTF-8 JSON 编码发布一个无损 JSON 值。"""
        # 领域复制器会先拒绝非字符串键、非有限浮点数、tuple 和孤立 surrogate，
        # 避免 json.dumps 的宽松转换悄悄改变调用方数据形状。
        stable_value = copy_json_value(value, field="artifact JSON")
        encoded = json.dumps(
            stable_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return self.put_bytes(encoded, media_type=media_type)

    def verify(self, ref: ArtifactRef) -> ArtifactRef:
        """完整重算对象大小与 SHA-256，并返回隔离的规范引用副本。"""
        normalized = self._normalize_ref(ref)
        path = self._object_path(normalized["sha256"])
        self._verify_file(
            path,
            expected_digest=normalized["sha256"],
            expected_size=normalized["size_bytes"],
        )
        return self._make_ref(
            normalized["sha256"],
            size_bytes=normalized["size_bytes"],
            media_type=normalized["media_type"],
        )

    @contextmanager
    def open_binary(self, ref: ArtifactRef) -> Iterator[BinaryIO]:
        """校验地址后打开对象并负责关闭句柄，但不隐式执行全量 digest 校验。"""
        normalized = self._normalize_ref(ref)
        path = self._object_path(normalized["sha256"])

        # lstat 明确拒绝目录和符号链接；正式对象只能是本 store 发布的普通文件。
        try:
            entry_stat = path.lstat()
        except FileNotFoundError as error:
            raise ArtifactNotFound(f"产物不存在：{normalized['uri']}") from error
        except OSError as error:
            raise ArtifactStoreError(f"无法检查产物：{normalized['uri']}") from error
        if not stat.S_ISREG(entry_stat.st_mode):
            raise ArtifactCorruption(f"产物路径不是普通文件：{normalized['uri']}")

        try:
            reader = path.open("rb")
        except FileNotFoundError as error:
            raise ArtifactNotFound(f"产物不存在：{normalized['uri']}") from error
        except OSError as error:
            raise ArtifactStoreError(f"无法打开产物：{normalized['uri']}") from error
        try:
            yield reader
        finally:
            reader.close()

    def contains(self, ref: ArtifactRef) -> bool:
        """只检查规范地址是否存在普通文件，不读取或信任其中内容。"""
        normalized = self._normalize_ref(ref)
        path = self._object_path(normalized["sha256"])
        try:
            return stat.S_ISREG(path.lstat().st_mode)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ArtifactStoreError(f"无法检查产物是否存在：{normalized['uri']}") from error

    def cleanup_stale_temps(self, *, older_than_seconds: float) -> int:
        """删除严格早于阈值的本实现临时文件，并返回删除数量。"""
        if (
            isinstance(older_than_seconds, bool)
            or not isinstance(older_than_seconds, (int, float))
            or not math.isfinite(float(older_than_seconds))
            or older_than_seconds < 0
        ):
            raise ValueError("older_than_seconds 必须是非负有限数")
        cutoff = time.time() - float(older_than_seconds)
        removed = 0

        # 发布和清理使用同一根级锁，清理器不会删除仍在 flush/fsync 的活跃临时文件。
        with self._exclusive_store_lock():
            try:
                prefix_entries = list(os.scandir(self._objects_root))
            except FileNotFoundError:
                return 0
            except OSError as error:
                raise ArtifactStoreError("无法扫描产物临时文件") from error

            for prefix_entry in prefix_entries:
                if _DIGEST_PREFIX_PATTERN.fullmatch(
                    prefix_entry.name
                ) is None or not prefix_entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    candidates = list(os.scandir(prefix_entry.path))
                except OSError as error:
                    raise ArtifactStoreError("无法扫描产物 digest 目录") from error

                # 只删除本实现严格命名的普通临时文件；正式对象和相似文件一律保留。
                for candidate in candidates:
                    if _TEMP_FILE_PATTERN.fullmatch(
                        candidate.name
                    ) is None or not candidate.is_file(follow_symlinks=False):
                        continue
                    try:
                        if candidate.stat(follow_symlinks=False).st_mtime < cutoff:
                            os.unlink(candidate.path)
                            removed += 1
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        raise ArtifactStoreError("无法删除过期产物临时文件") from error
        return removed

    def _publish_spool(
        self,
        spool: BinaryIO,
        *,
        digest: str,
        size_bytes: int,
        media_type: str,
    ) -> ArtifactRef:
        """把已计算身份的 spool 写入同目录临时文件并无覆盖发布。"""
        ref = self._make_ref(digest, size_bytes=size_bytes, media_type=media_type)
        target = self._object_path(digest)

        # 锁内先重校验已有对象；相同内容幂等返回，任何不一致都保留现场并报损坏。
        with self._exclusive_store_lock():
            if os.path.lexists(target):
                self._verify_file(
                    target,
                    expected_digest=digest,
                    expected_size=size_bytes,
                )

                # “已经存在”只能证明当前可见，不能证明创建者曾完成目录 fsync。
                # 幂等复用也要补齐两级同步，才能为本次成功返回建立持久性保证。
                self._sync_artifact_directories(target.parent)
                return ref

            try:
                # sha256 根目录已在初始化时建立；单层 mkdir 能区分“同名文件”
                # 与可复用目录。目录即使已存在，后续仍会重新同步其父目录。
                target.parent.mkdir()
            except FileExistsError:
                if not target.parent.is_dir():
                    raise ArtifactStoreError("产物 digest 路径不是目录")
            except OSError as error:
                raise ArtifactStoreError("无法创建产物 digest 目录") from error
            self._assert_contained(target)

            temp_path: Path | None = None
            try:
                # 临时文件与正式对象处于同一目录，后续硬链接发布不会跨文件系统。
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w+b",
                        prefix=".tmp-",
                        suffix=".part",
                        dir=target.parent,
                        delete=False,
                    ) as temp_file:
                        temp_path = Path(temp_file.name)
                        copied_digest, copied_size = self._copy_and_sync(spool, temp_file)
                except ArtifactStoreError:
                    raise
                except OSError as error:
                    raise ArtifactStoreError("写入或同步产物临时文件失败") from error

                # 关闭句柄后再次核对复制结果，内存/磁盘短写不会进入正式命名空间。
                if copied_digest != digest or copied_size != size_bytes:
                    raise ArtifactCorruption("产物临时文件与流式内容身份不一致")

                try:
                    # hard-link 的目标创建具有 O_EXCL 语义；即使有外部写入者，也绝不覆盖。
                    os.link(temp_path, target)
                except FileExistsError:
                    self._verify_file(
                        target,
                        expected_digest=digest,
                        expected_size=size_bytes,
                    )
                except OSError as error:
                    raise ArtifactStoreError("文件系统不支持安全的无覆盖产物发布") from error
                finally:
                    self._unlink_temp_best_effort(temp_path)

                # 文件 fsync 不持久化目录项；两级目录都必须同步，且不能把
                # “本进程看见它已存在”误当成“先前进程已经把它持久化”。
                self._sync_artifact_directories(target.parent)
                return ref
            finally:
                if temp_path is not None:
                    self._unlink_temp_best_effort(temp_path)

    def _copy_and_sync(self, source: BinaryIO, target: BinaryIO) -> tuple[str, int]:
        """把 spool 复制到临时文件，同时复算身份并完成文件级持久化。"""
        hasher = hashlib.sha256()
        size_bytes = 0
        while True:
            chunk = source.read(_STREAM_CHUNK_SIZE)
            if chunk == b"":
                break
            if not isinstance(chunk, bytes):  # pragma: no cover - source 是内部二进制 spool
                raise ArtifactStoreError("内部 spool 返回了非 bytes 数据")
            written = target.write(chunk)
            if written != len(chunk):  # pragma: no cover - 本地缓冲文件正常情况下全写
                raise ArtifactStoreError("产物临时文件发生短写")
            hasher.update(chunk)
            size_bytes += len(chunk)
        target.flush()
        os.fsync(target.fileno())
        return hasher.hexdigest(), size_bytes

    def _verify_file(
        self,
        path: Path,
        *,
        expected_digest: str,
        expected_size: int,
    ) -> None:
        """流式重算一个正式对象；不一致时绝不修复或覆盖现场。"""
        uri = self._uri_for_digest(expected_digest)
        try:
            entry_stat = path.lstat()
        except FileNotFoundError as error:
            raise ArtifactNotFound(f"产物不存在：{uri}") from error
        except OSError as error:
            raise ArtifactStoreError(f"无法检查产物：{uri}") from error
        if not stat.S_ISREG(entry_stat.st_mode):
            raise ArtifactCorruption(f"产物路径不是普通文件：{uri}")

        hasher = hashlib.sha256()
        size_bytes = 0
        try:
            with path.open("rb") as stored:
                while True:
                    chunk = stored.read(_STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    size_bytes += len(chunk)
        except FileNotFoundError as error:
            raise ArtifactNotFound(f"产物不存在：{uri}") from error
        except OSError as error:
            raise ArtifactStoreError(f"无法读取产物：{uri}") from error

        # size 与 digest 分别报告，便于定位截断和静默内容替换两类损坏。
        if size_bytes != expected_size:
            raise ArtifactCorruption(f"产物大小不匹配：期望 {expected_size}，实际 {size_bytes}")
        actual_digest = hasher.hexdigest()
        if actual_digest != expected_digest:
            raise ArtifactCorruption(
                f"产物 SHA-256 不匹配：期望 {expected_digest}，实际 {actual_digest}"
            )

    def _normalize_ref(self, ref: ArtifactRef) -> ArtifactRef:
        """验证 ArtifactRef 形状、规范 URI 以及 URI/digest 一致性。"""
        try:
            normalized = validate_artifact_ref(ref)
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidArtifactReference("ArtifactRef 字段无效") from error

        match = _ARTIFACT_URI_PATTERN.fullmatch(normalized["uri"])
        if match is None:
            raise InvalidArtifactReference("本地存储只接受 artifact://sha256/<64 位小写十六进制>")
        if match.group(1) != normalized["sha256"]:
            raise InvalidArtifactReference("ArtifactRef URI 与 sha256 字段不一致")
        return normalized

    def _make_ref(self, digest: str, *, size_bytes: int, media_type: str) -> ArtifactRef:
        """构造只含稳定标量的规范引用。"""
        return {
            "uri": self._uri_for_digest(digest),
            "sha256": digest,
            "media_type": media_type,
            "size_bytes": size_bytes,
        }

    def _object_path(self, digest: str) -> Path:
        """把安全 digest 映射到 ``root/sha256/前两位/完整值``。"""
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise InvalidArtifactReference("非法 SHA-256 不能映射为存储路径")
        candidate = self._objects_root / digest[:2] / digest
        self._assert_contained(candidate)
        return candidate

    def _assert_contained(self, path: Path) -> None:
        """拒绝解析后逃出根目录的路径，包括恶意中间符号链接。"""
        root_key = _path_comparison_key(self.root)
        path_key = _path_comparison_key(path)
        try:
            common = os.path.commonpath((root_key, path_key))
        except ValueError as error:
            raise InvalidArtifactReference("产物路径越过了存储根目录") from error
        if common != root_key:
            raise InvalidArtifactReference("产物路径越过了存储根目录")

    @staticmethod
    def _uri_for_digest(digest: str) -> str:
        """返回唯一的规范 artifact URI。"""
        return f"artifact://sha256/{digest}"

    @staticmethod
    def _validate_media_type(media_type: object) -> str:
        """在消费输入流前拒绝无效或不可 UTF-8 编码的媒体类型。"""
        if type(media_type) is not str or not media_type.strip():
            raise InvalidArtifactReference("media_type 必须是非空原生字符串")
        try:
            media_type.encode("utf-8")
        except UnicodeEncodeError as error:
            raise InvalidArtifactReference("media_type 必须可写入 UTF-8") from error
        return media_type

    @staticmethod
    def _validate_lock_duration(value: object, *, field: str) -> float:
        """把锁时间配置收窄为 threading 能安全接收的正有限秒数。"""
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            or float(value) > threading.TIMEOUT_MAX
        ):
            raise ValueError(f"{field} 必须是正有限秒数且不超过 threading.TIMEOUT_MAX")
        return float(value)

    @contextmanager
    def _exclusive_store_lock(self) -> Iterator[None]:
        """在同一截止时间内取得线程锁和根目录文件锁。"""
        deadline = time.monotonic() + self._lock_timeout_seconds

        # 每次动态查表至关重要：fork 子进程会清空继承的锁缓存，因此旧 store
        # 实例也必须取得子进程中新建的 RLock，不能缓存父进程的锁对象。
        thread_lock = _thread_lock_for(self.root)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not thread_lock.acquire(timeout=remaining):
            raise ArtifactStoreError("等待产物存储线程锁超时")

        try:
            try:
                with self._tracked_lock_file() as lock_file:
                    self._prepare_lock_byte(lock_file)
                    self._lock_file(lock_file, deadline=deadline)
                    try:
                        yield
                    finally:
                        self._unlock_file(lock_file)
            except ArtifactStoreError:
                raise
            except OSError as error:
                raise ArtifactStoreError("无法获取产物存储锁") from error
        finally:
            thread_lock.release()

    @contextmanager
    def _tracked_lock_file(self) -> Iterator[BinaryIO]:
        """打开无缓冲锁文件，并登记供 POSIX fork 子进程关闭其副本。"""
        lock_file: BinaryIO | None = None

        # 打开与登记位于同一 guard 内，fork 前回调不会观察到“已打开但未登记”
        # 的描述符。关闭也在 guard 内完成，避免相反的短暂遗漏窗口。
        with _ACTIVE_LOCK_FILES_GUARD:
            lock_file = self._lock_path.open("a+b", buffering=0)
            _ACTIVE_LOCK_FILES.add(lock_file)
        try:
            yield lock_file
        finally:
            with _ACTIVE_LOCK_FILES_GUARD:
                try:
                    lock_file.close()
                finally:
                    _ACTIVE_LOCK_FILES.discard(lock_file)

    @staticmethod
    def _prepare_lock_byte(lock_file: BinaryIO) -> None:
        """保证 Windows 字节范围锁始终有一个可锁定字节。"""
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            written = lock_file.write(b"\0")
            if written != 1:  # pragma: no cover - 本地无缓冲文件正常情况下全写
                raise ArtifactStoreError("产物锁文件发生短写")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        lock_file.seek(0)

    def _lock_file(self, lock_file: BinaryIO, *, deadline: float) -> None:
        """以非阻塞系统调用轮询，直到获得文件锁或统一截止时间耗尽。"""
        last_contention: OSError | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArtifactStoreError("等待产物存储文件锁超时") from last_contention

            try:
                lock_file.seek(0)
                if os.name == "nt":  # pragma: no cover - 仅 Windows 执行
                    import msvcrt

                    # LK_LOCK 只重试十次，并不是真正无限阻塞；显式使用非阻塞
                    # 模式，才能与 POSIX 共用准确且可配置的等待上限。
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl  # pragma: no cover - 仅 POSIX 执行

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as error:
                if not self._is_lock_contention(error):
                    raise ArtifactStoreError("无法获取产物存储文件锁") from error
                last_contention = error

            # 睡眠不越过 deadline；EINTR 也沿用同一上限，避免信号风暴无限等待。
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArtifactStoreError("等待产物存储文件锁超时") from last_contention
            time.sleep(min(self._lock_poll_interval_seconds, remaining))

    @staticmethod
    def _is_lock_contention(error: OSError) -> bool:
        """区分可重试的锁竞争与权限、文件系统等永久错误。"""
        retryable_errno = {errno.EACCES, errno.EAGAIN, errno.EINTR}
        if hasattr(errno, "EDEADLK"):
            retryable_errno.add(errno.EDEADLK)

        # Windows CRT 通常给出 errno=EACCES/EDEADLK；winerror 集合作为
        # 不同 Python/CRT 组合的防御性兼容，不会吞掉其他 I/O 错误。
        return error.errno in retryable_errno or getattr(error, "winerror", None) in {
            32,
            33,
            36,
        }

    def _sync_artifact_directories(self, prefix_directory: Path) -> None:
        """按子到父顺序持久化对象目录项和 digest 前缀目录项。"""
        self._sync_directory(prefix_directory)
        self._sync_directory(self._objects_root)

    @staticmethod
    def _unlock_file(lock_file: BinaryIO) -> None:
        """释放平台文件锁；调用方仍负责关闭锁文件。"""
        lock_file.seek(0)
        if os.name == "nt":  # pragma: no cover - 仅 Windows 执行
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl  # pragma: no cover - 仅 POSIX 执行

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        """在支持目录 fsync 的平台持久化链接创建和临时文件删除。"""
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(directory, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:  # pragma: no cover - 依赖具体文件系统能力
            raise ArtifactStoreError("无法同步产物目录") from error

    @staticmethod
    def _unlink_temp_best_effort(path: Path) -> None:
        """清理由当前调用创建的临时链接；失败时交给显式过期清理。"""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # 正式对象已经安全发布时，临时文件残留不应把一次成功发布改写成失败。
            pass


__all__ = ["ContentAddressedArtifactStore"]
