"""本地内容寻址产物存储的合同、完整性与并发测试。"""

from __future__ import annotations

import hashlib
import io
import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from trans_novel.storage import (
    ArtifactCorruption,
    ArtifactNotFound,
    ArtifactStoreError,
    ContentAddressedArtifactStore,
    InvalidArtifactReference,
)


def _object_path(root: Path, digest: str) -> Path:
    """按公共布局规则定位测试对象，不依赖实现的私有帮助函数。"""
    return root / "sha256" / digest[:2] / digest


def _missing_ref(digest: str = "a" * 64) -> dict[str, Any]:
    """构造形状合法、但尚未发布的引用。"""
    return {
        "uri": f"artifact://sha256/{digest}",
        "sha256": digest,
        "media_type": "application/octet-stream",
        "size_bytes": 0,
    }


def _process_put(
    root: str,
    payload: bytes,
    start_event: Any,
    result_queue: Any,
) -> None:
    """在 spawn 子进程中同时发布同一 payload，验证真正的文件锁边界。"""
    try:
        if not start_event.wait(10):
            result_queue.put(("error", "start timeout"))
            return
        ref = ContentAddressedArtifactStore(root).put_bytes(
            payload,
            media_type="application/octet-stream",
        )
        result_queue.put(("ok", ref))
    except Exception as error:  # noqa: BLE001 - 子进程必须把失败传回父进程断言
        result_queue.put(("error", f"{type(error).__name__}: {error}"))


def _process_put_with_timeout(
    root: str,
    payload: bytes,
    lock_timeout_seconds: float,
    result_queue: Any,
) -> None:
    """让独立进程以短截止时间竞争父进程已持有的文件锁。"""
    try:
        ref = ContentAddressedArtifactStore(
            root,
            lock_timeout_seconds=lock_timeout_seconds,
            lock_poll_interval_seconds=0.01,
        ).put_bytes(payload, media_type="application/octet-stream")
        result_queue.put(("ok", ref))
    except Exception as error:  # noqa: BLE001 - 子进程错误必须回传给父进程断言
        result_queue.put(("error", f"{type(error).__name__}: {error}"))


def _fork_put_inherited_store(
    store: ContentAddressedArtifactStore,
    payload: bytes,
    result_queue: Any,
) -> None:
    """在 fork 子进程复用父进程 store，覆盖继承锁状态的恢复路径。"""
    try:
        ref = store.put_bytes(payload, media_type="application/octet-stream")
        result_queue.put(("ok", ref))
    except Exception as error:  # noqa: BLE001 - 子进程错误必须回传给父进程断言
        result_queue.put(("error", f"{type(error).__name__}: {error}"))


def _reap_started_processes(processes: list[Any]) -> None:
    """完整回收已启动进程，避免失败测试把子进程遗留给后续门禁。"""
    # 先给所有进程温和终止机会，再统一等待，避免逐个等待拖长清理时间。
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join(timeout=5)

    # terminate 若未生效则使用不可忽略的 kill，并做最终 join 回收系统资源。
    for process in processes:
        if process.is_alive():
            process.kill()
    for process in processes:
        process.join()


class _ChunkGuardStream(io.BytesIO):
    """拒绝一次性读取，证明 put_stream 使用有界块并保留调用方句柄。"""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        if size <= 0:
            raise AssertionError("put_stream 不得执行无界读取")
        self.read_sizes.append(size)
        return super().read(size)


def test_put_bytes_uses_canonical_uri_layout_and_is_idempotent(tmp_path: Path) -> None:
    """相同内容重复发布只复用已经完整校验的正式对象。"""
    store = ContentAddressedArtifactStore(tmp_path)
    payload = "不可变正文".encode()
    digest = hashlib.sha256(payload).hexdigest()

    first = store.put_bytes(payload, media_type="text/plain; charset=utf-8")
    second = store.put_bytes(payload, media_type="text/plain; charset=utf-8")
    alternate_media_type = store.put_bytes(payload, media_type="application/octet-stream")

    assert first == second
    assert first == {
        "uri": f"artifact://sha256/{digest}",
        "sha256": digest,
        "media_type": "text/plain; charset=utf-8",
        "size_bytes": len(payload),
    }
    assert _object_path(tmp_path, digest).read_bytes() == payload
    assert alternate_media_type["sha256"] == first["sha256"]
    assert alternate_media_type["media_type"] == "application/octet-stream"
    assert not list(_object_path(tmp_path, digest).parent.glob(".tmp-*.part"))
    assert store.contains(first)
    assert store.verify(first) == first


def test_existing_corrupt_target_is_never_overwritten(tmp_path: Path) -> None:
    """digest 路径被不同字节占用时，发布必须保留现场并报告损坏。"""
    store = ContentAddressedArtifactStore(tmp_path)
    payload = b"expected immutable bytes"
    ref = store.put_bytes(payload, media_type="application/octet-stream")
    target = _object_path(tmp_path, ref["sha256"])
    corrupt = b"x" * len(payload)
    target.write_bytes(corrupt)

    with pytest.raises(ArtifactCorruption):
        store.put_bytes(payload, media_type="application/octet-stream")

    assert target.read_bytes() == corrupt
    assert store.contains(ref) is True
    with pytest.raises(ArtifactCorruption):
        store.verify(ref)


def test_put_stream_hashes_large_input_in_bounded_chunks_without_closing_it(
    tmp_path: Path,
) -> None:
    """超过内存 spool 阈值的大流仍以常量块读取，并能完整回读。"""
    payload = (b"0123456789abcdef" * (700 * 1024)) + b"tail"
    source = _ChunkGuardStream(payload)
    store = ContentAddressedArtifactStore(tmp_path)

    ref = store.put_stream(source, media_type="application/octet-stream")

    assert ref["sha256"] == hashlib.sha256(payload).hexdigest()
    assert ref["size_bytes"] == len(payload)
    assert len(source.read_sizes) > 2
    assert source.closed is False
    with store.open_binary(ref) as reader:
        assert reader.read() == payload
    assert source.closed is False


def test_put_stream_starts_at_current_position(tmp_path: Path) -> None:
    """调用方可以先消费流头；store 只发布当前位置到 EOF。"""
    source = io.BytesIO(b"header-body")
    source.seek(len(b"header-"))
    store = ContentAddressedArtifactStore(tmp_path)

    ref = store.put_stream(source, media_type="application/octet-stream")

    with store.open_binary(ref) as reader:
        assert reader.read() == b"body"


def test_put_json_is_canonical_utf8_and_rejects_lossy_values(tmp_path: Path) -> None:
    """键顺序不影响身份，Unicode 不转义，非稳定 JSON 在写盘前被拒绝。"""
    store = ContentAddressedArtifactStore(tmp_path)
    left = {"雪": "山", "b": 2, "a": [True, None, 1.5]}
    right = {"a": [True, None, 1.5], "b": 2, "雪": "山"}
    expected = '{"a":[true,null,1.5],"b":2,"雪":"山"}'.encode()

    first = store.put_json(left)
    second = store.put_json(right)

    assert first == second
    assert first["media_type"] == "application/json"
    with store.open_binary(first) as reader:
        assert reader.read() == expected

    # Python JSON 编码器会宽松转换这些值；合同要求在发生形状变化前明确拒绝。
    with pytest.raises(ValueError):
        store.put_json({1: "integer key"})
    with pytest.raises(ValueError):
        store.put_json({"not_finite": float("nan")})
    with pytest.raises(ValueError):
        store.put_json({"tuple": (1, 2)})


@pytest.mark.parametrize(
    "ref",
    [
        {
            **_missing_ref(),
            "uri": f"artifact://sha256/{'A' * 64}",
        },
        {
            **_missing_ref(),
            "uri": "artifact://sha256/../../outside",
        },
        {
            **_missing_ref(),
            "uri": f"artifact://sha256/{'a' * 64}?download=1",
        },
        {
            **_missing_ref(),
            "uri": f"artifact://sha512/{'a' * 64}",
        },
        {
            **_missing_ref(),
            "uri": f"artifact://sha256/{'b' * 64}",
        },
        {
            "uri": f"artifact://sha256/{'a' * 64}",
            "sha256": "a" * 64,
            "media_type": "application/octet-stream",
        },
    ],
)
def test_invalid_or_traversing_references_are_rejected(
    tmp_path: Path,
    ref: dict[str, Any],
) -> None:
    """非规范 URI、路径穿越、身份分裂和缺字段都不能触碰文件系统对象。"""
    store = ContentAddressedArtifactStore(tmp_path)

    with pytest.raises(InvalidArtifactReference):
        store.contains(ref)
    with pytest.raises(InvalidArtifactReference):
        store.verify(ref)
    with pytest.raises(InvalidArtifactReference):
        with store.open_binary(ref):
            pass


def test_missing_artifact_has_distinct_contains_verify_and_open_semantics(tmp_path: Path) -> None:
    """合法缺失不是非法引用：contains 为假，读取和完整校验抛 NotFound。"""
    store = ContentAddressedArtifactStore(tmp_path)
    ref = _missing_ref()

    assert store.contains(ref) is False
    with pytest.raises(ArtifactNotFound):
        store.verify(ref)
    with pytest.raises(ArtifactNotFound):
        with store.open_binary(ref):
            pass


def test_open_binary_is_an_existence_read_not_an_implicit_digest_check(tmp_path: Path) -> None:
    """损坏对象仍可供取证读取；只有 verify 承担完整 hash 校验。"""
    store = ContentAddressedArtifactStore(tmp_path)
    ref = store.put_bytes(b"formal bytes", media_type="application/octet-stream")
    target = _object_path(tmp_path, ref["sha256"])
    target.write_bytes(b"tampered bytes")

    with store.open_binary(ref) as reader:
        assert reader.read() == b"tampered bytes"
    with pytest.raises(ArtifactCorruption):
        store.verify(ref)


def test_verify_rejects_reference_size_drift_and_returns_a_detached_copy(tmp_path: Path) -> None:
    """引用元数据也属于合同；返回副本不能与调用方共享映射身份。"""
    store = ContentAddressedArtifactStore(tmp_path)
    ref = store.put_bytes(b"abc", media_type="text/plain")
    verified = store.verify(ref)

    assert verified == ref
    assert verified is not ref

    wrong_size = {**ref, "size_bytes": 4}
    with pytest.raises(ArtifactCorruption, match="大小"):
        store.verify(wrong_size)


def test_threaded_publishers_share_one_valid_object(tmp_path: Path) -> None:
    """同进程不同 store 实例并发时，共享根锁并留下一个正式对象。"""
    payload = b"thread-safe" * (128 * 1024)
    stores = [ContentAddressedArtifactStore(tmp_path) for _ in range(2)]

    def publish(index: int) -> dict[str, Any]:
        return stores[index % len(stores)].put_bytes(
            payload,
            media_type="application/octet-stream",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = list(executor.map(publish, range(24)))

    assert all(ref == refs[0] for ref in refs)
    assert stores[0].verify(refs[0]) == refs[0]
    target_dir = _object_path(tmp_path, refs[0]["sha256"]).parent
    assert sorted(path.name for path in target_dir.iterdir()) == [refs[0]["sha256"]]


def test_spawned_processes_publish_the_same_object_safely(tmp_path: Path) -> None:
    """独立进程不能绕过根目录文件锁或相互覆盖正式对象。"""
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    payload = b"process-safe" * (64 * 1024)
    processes = [
        context.Process(
            target=_process_put,
            args=(str(tmp_path), payload, start_event, result_queue),
        )
        for _ in range(3)
    ]
    started_processes: list[Any] = []

    try:
        for process in processes:
            # start 可能在部分创建子进程后抛错；只要 pid 已出现就纳入 finally 回收。
            try:
                process.start()
            finally:
                if process.pid is not None:
                    started_processes.append(process)
        start_event.set()
        for process in started_processes:
            process.join(timeout=20)
        if any(process.is_alive() for process in started_processes):
            pytest.fail("子进程并发发布超时")
        results = [result_queue.get(timeout=3) for _ in processes]
    except Empty as error:
        pytest.fail(f"子进程没有返回并发发布结果：{error}")
    finally:
        _reap_started_processes(started_processes)
        result_queue.close()
        result_queue.join_thread()

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    assert all(status == "ok" for status, _ in results), results
    refs = [payload_ref for _, payload_ref in results]
    assert all(ref == refs[0] for ref in refs)
    assert ContentAddressedArtifactStore(tmp_path).verify(refs[0]) == refs[0]


def test_process_file_lock_respects_one_configured_deadline(tmp_path: Path) -> None:
    """跨进程竞争由显式短截止时间结束，而不是依赖平台隐式重试次数。"""
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    holder = ContentAddressedArtifactStore(tmp_path)
    process = context.Process(
        target=_process_put_with_timeout,
        args=(str(tmp_path), b"contended", 0.2, result_queue),
    )
    started_processes: list[Any] = []

    try:
        # 父进程持有真正的 OS 文件锁时启动 contender，避免只测到线程 RLock。
        with holder._exclusive_store_lock():  # noqa: SLF001
            try:
                process.start()
            finally:
                if process.pid is not None:
                    started_processes.append(process)
            status, detail = result_queue.get(timeout=10)
        process.join(timeout=5)
    except Empty as error:
        pytest.fail(f"锁竞争子进程没有在截止时间内返回：{error}")
    finally:
        _reap_started_processes(started_processes)
        result_queue.close()
        result_queue.join_thread()

    assert process.exitcode == 0
    assert status == "error"
    assert "ArtifactStoreError" in detail
    assert "超时" in detail


@pytest.mark.skipif(not hasattr(os, "fork"), reason="仅 POSIX 提供 fork")
def test_fork_child_rebuilds_inherited_thread_and_file_lock_state(
    tmp_path: Path,
) -> None:
    """另一线程持锁时 fork，子进程仍能在父进程释放后继续使用旧 store。"""
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    store = ContentAddressedArtifactStore(tmp_path, lock_timeout_seconds=5)
    lock_entered = threading.Event()
    release_lock = threading.Event()
    holder_errors: list[BaseException] = []

    def hold_store_lock() -> None:
        """模拟 fork 时已经消失于子进程的锁持有线程。"""
        try:
            with store._exclusive_store_lock():  # noqa: SLF001
                lock_entered.set()
                if not release_lock.wait(10):
                    raise TimeoutError("父进程测试线程等待释放信号超时")
        except BaseException as error:  # noqa: BLE001 - 测试线程错误由主线程统一断言
            holder_errors.append(error)

    holder_thread = threading.Thread(target=hold_store_lock, daemon=True)
    holder_thread.start()
    assert lock_entered.wait(5), "父进程测试线程未取得 store 锁"

    process = context.Process(
        target=_fork_put_inherited_store,
        args=(store, b"fork-safe", result_queue),
    )
    started_processes: list[Any] = []
    try:
        try:
            process.start()
        finally:
            if process.pid is not None:
                started_processes.append(process)

        # 父进程释放后，子进程应等待 OS 锁并完成；继承的 RLock 不能永久阻塞它。
        release_lock.set()
        process.join(timeout=10)
        if process.is_alive():
            pytest.fail("fork 子进程疑似卡在继承的线程锁")
        status, detail = result_queue.get(timeout=3)
    except Empty as error:
        pytest.fail(f"fork 子进程没有返回结果：{error}")
    finally:
        release_lock.set()
        holder_thread.join(timeout=10)
        _reap_started_processes(started_processes)
        result_queue.close()
        result_queue.join_thread()

    assert holder_thread.is_alive() is False
    assert holder_errors == []
    assert process.exitcode == 0
    assert status == "ok", detail
    assert store.verify(detail) == detail


def test_first_digest_prefix_syncs_both_directory_levels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次发布先同步对象目录，再同步承载新前缀项的 sha256 根目录。"""
    store = ContentAddressedArtifactStore(tmp_path)
    synced_directories: list[Path] = []
    monkeypatch.setattr(store, "_sync_directory", synced_directories.append)
    payload = b"new-prefix-durability"
    digest = hashlib.sha256(payload).hexdigest()

    store.put_bytes(payload, media_type="application/octet-stream")

    expected_prefix = _object_path(tmp_path, digest).parent
    assert synced_directories == [expected_prefix, tmp_path / "sha256"]


def test_preexisting_prefix_is_resynced_without_trusting_its_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """看见前缀目录不代表创建者已 fsync；成功发布仍要补齐两级同步。"""
    store = ContentAddressedArtifactStore(tmp_path)
    payload = b"preexisting-prefix"
    digest = hashlib.sha256(payload).hexdigest()
    expected_prefix = _object_path(tmp_path, digest).parent
    expected_prefix.mkdir()
    synced_directories: list[Path] = []
    monkeypatch.setattr(store, "_sync_directory", synced_directories.append)

    store.put_bytes(payload, media_type="application/octet-stream")

    assert synced_directories == [expected_prefix, tmp_path / "sha256"]


def test_idempotent_existing_target_reestablishes_directory_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """幂等早返不能信任上一发布者；验证内容后重新同步两级目录。"""
    store = ContentAddressedArtifactStore(tmp_path)
    payload = b"existing-target"
    first = store.put_bytes(payload, media_type="application/octet-stream")
    expected_prefix = _object_path(tmp_path, first["sha256"]).parent
    synced_directories: list[Path] = []
    monkeypatch.setattr(store, "_sync_directory", synced_directories.append)

    second = store.put_bytes(payload, media_type="application/octet-stream")

    assert second == first
    assert synced_directories == [expected_prefix, tmp_path / "sha256"]


@pytest.mark.parametrize(
    "options",
    [
        {"lock_timeout_seconds": 0},
        {"lock_timeout_seconds": float("inf")},
        {"lock_timeout_seconds": True},
        {"lock_poll_interval_seconds": -0.1},
        {"lock_poll_interval_seconds": float("nan")},
    ],
)
def test_invalid_lock_timing_is_rejected_before_filesystem_initialization(
    tmp_path: Path,
    options: dict[str, object],
) -> None:
    """锁配置错误不能创建 sha256 目录或留下锁文件。"""
    with pytest.raises(ValueError):
        ContentAddressedArtifactStore(tmp_path, **options)  # type: ignore[arg-type]

    assert (tmp_path / "sha256").exists() is False
    assert (tmp_path / ".artifact-store.lock").exists() is False


def test_unicode_root_is_supported(tmp_path: Path) -> None:
    """Windows 和 POSIX 都必须能在含非 ASCII 字符的根目录发布和读取。"""
    root = tmp_path / "文译・产物仓库"
    store = ContentAddressedArtifactStore(root)

    ref = store.put_bytes("雪国".encode(), media_type="text/plain; charset=utf-8")

    assert _object_path(root, ref["sha256"]).is_file()
    with store.open_binary(ref) as reader:
        assert reader.read().decode() == "雪国"


def test_cleanup_removes_only_strictly_stale_owned_temp_files(tmp_path: Path) -> None:
    """清理器只处理合法 digest 目录中的 `.tmp-*.part`，并严格比较时间阈值。"""
    store = ContentAddressedArtifactStore(tmp_path)
    prefix_dir = tmp_path / "sha256" / "ab"
    prefix_dir.mkdir(parents=True)
    old_temp = prefix_dir / ".tmp-old.part"
    fresh_temp = prefix_dir / ".tmp-fresh.part"
    similar_name = prefix_dir / ".tmp-old.partial"
    outside = tmp_path / ".tmp-outside.part"
    for path in (old_temp, fresh_temp, similar_name, outside):
        path.write_bytes(b"temporary")

    old_time = time.time() - 7200
    os.utime(old_temp, (old_time, old_time))

    removed = store.cleanup_stale_temps(older_than_seconds=3600)

    assert removed == 1
    assert old_temp.exists() is False
    assert fresh_temp.exists()
    assert similar_name.exists()
    assert outside.exists()

    with pytest.raises(ValueError):
        store.cleanup_stale_temps(older_than_seconds=-1)


def test_invalid_media_type_is_rejected_before_stream_consumption(tmp_path: Path) -> None:
    """调用参数错误不能消耗不可重放的输入流。"""
    source = io.BytesIO(b"payload")
    store = ContentAddressedArtifactStore(tmp_path)

    with pytest.raises(InvalidArtifactReference):
        store.put_stream(source, media_type="  ")

    assert source.tell() == 0


class _ShortWriteTarget:
    """故障注入目标：只接收源块前半部分，但保留可用于 fsync 的文件句柄。"""

    def __init__(self, path: Path) -> None:
        self._file = path.open("w+b")

    def write(self, data: bytes) -> int:
        accepted = max(1, len(data) // 2)
        self._file.write(data[:accepted])
        return accepted

    def flush(self) -> None:
        self._file.flush()

    def fileno(self) -> int:
        return self._file.fileno()

    def close(self) -> None:
        self._file.close()


def test_copy_stage_rejects_short_writes_before_publication(tmp_path: Path) -> None:
    """底层短写必须在正式命名空间之前被检出，不能按源块长度计账。"""
    store = ContentAddressedArtifactStore(tmp_path)
    target = _ShortWriteTarget(tmp_path / "short-write.bin")
    try:
        with pytest.raises(ArtifactStoreError, match="短写"):
            store._copy_and_sync(io.BytesIO(b"abcdef"), target)  # noqa: SLF001
    finally:
        target.close()
