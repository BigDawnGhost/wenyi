from __future__ import annotations

import threading

from trans_novel.application.understanding import UnderstandingChapter, build_understanding


def test_parallel_digest_commits_in_completion_order_and_summarizes_manifest_order() -> None:
    calling_thread = threading.get_ident()
    started = {index: threading.Event() for index in range(3)}
    release = {index: threading.Event() for index in range(3)}
    saved = {index: threading.Event() for index in range(3)}
    worker_threads: set[int] = set()
    save_calls: list[tuple[int, str, int]] = []
    events: list[tuple[str, dict[str, object], int]] = []
    progress_updates: list[tuple[int, int, str, int]] = []
    synopsis_inputs: list[tuple[list[str], str, int]] = []
    saved_analysis: list[dict[str, object]] = []

    def digest(source: str) -> str:
        index = int(source)
        worker_threads.add(threading.get_ident())
        started[index].set()
        assert release[index].wait(timeout=5)
        return f"digest-{index}"

    def save_digest(index: int, digest_value: str) -> None:
        save_calls.append((index, digest_value, threading.get_ident()))
        saved[index].set()

    def emit(name: str, attributes) -> None:
        events.append((name, dict(attributes), threading.get_ident()))

    def publish_progress(done: int, total: int, message: str) -> None:
        progress_updates.append((done, total, message, threading.get_ident()))

    def summarize(digests: list[str], brief: str) -> str:
        synopsis_inputs.append((digests, brief, threading.get_ident()))
        return "book synopsis"

    def release_in_completion_order() -> None:
        for signal in started.values():
            signal.wait(timeout=5)
        for index in (2, 0, 1):
            release[index].set()
            saved[index].wait(timeout=5)

    controller = threading.Thread(target=release_in_completion_order)
    controller.start()
    result = build_understanding(
        [
            UnderstandingChapter(index=0, source_text="0"),
            UnderstandingChapter(index=1, source_text="1"),
            UnderstandingChapter(index=2, source_text="2"),
        ],
        synopsis_order=[0, 1, 2],
        enabled=True,
        concurrency=3,
        digest_chapter=digest,
        summarize_book=summarize,
        style_brief=lambda analysis: str(analysis["style"]),
        load_analysis=lambda: {"style": "restrained"},
        save_analysis=saved_analysis.append,
        save_digest=save_digest,
        emit_event=emit,
        progress=publish_progress,
    )
    controller.join(timeout=5)

    assert not controller.is_alive()
    assert result == "book synopsis"
    assert [index for index, _, _ in save_calls] == [2, 0, 1]
    assert all(thread_id == calling_thread for _, _, thread_id in save_calls)
    assert worker_threads and calling_thread not in worker_threads
    assert synopsis_inputs == [(["digest-0", "digest-1", "digest-2"], "restrained", calling_thread)]
    assert saved_analysis == [{"style": "restrained", "book_synopsis": "book synopsis"}]
    assert [name for name, _, _ in events] == [
        "book_understanding_chapter_digest_started",
        "book_understanding_chapter_digest_saved",
        "book_understanding_chapter_digest_saved",
        "book_understanding_chapter_digest_saved",
        "book_synopsis_saved",
    ]
    assert events[0][1] == {"chapters": [0, 1, 2], "workers": 3}
    assert [event[1]["chapter"] for event in events[1:4]] == [2, 0, 1]
    assert all(thread_id == calling_thread for _, _, thread_id in events)
    assert [(done, total, message) for done, total, message, _ in progress_updates] == [
        (0, 3, "预扫章节梗概"),
        (1, 3, "预扫章节梗概"),
        (2, 3, "预扫章节梗概"),
        (3, 3, "预扫章节梗概"),
        (0, 0, "生成全书概览…"),
    ]
    assert all(thread_id == calling_thread for *_, thread_id in progress_updates)


def test_cached_digest_and_synopsis_skip_llm_and_writes() -> None:
    digest_calls: list[str] = []
    synopsis_calls: list[list[str]] = []
    digest_saves: list[tuple[int, str]] = []
    analysis_saves: list[dict[str, object]] = []
    events: list[str] = []

    result = build_understanding(
        [UnderstandingChapter(index=7, source_text="source", source_digest="cached digest")],
        synopsis_order=[7],
        enabled=True,
        concurrency=0,
        digest_chapter=lambda source: digest_calls.append(source) or "new digest",
        summarize_book=lambda digests, _brief: synopsis_calls.append(digests) or "new synopsis",
        style_brief=lambda _analysis: "style",
        load_analysis=lambda: {"book_synopsis": "cached synopsis"},
        save_analysis=analysis_saves.append,
        save_digest=lambda index, digest: digest_saves.append((index, digest)),
        emit_event=lambda name, _attributes: events.append(name),
    )

    assert result == "cached synopsis"
    assert digest_calls == []
    assert synopsis_calls == []
    assert digest_saves == []
    assert analysis_saves == []
    assert events == []


def test_disabled_understanding_is_a_true_short_circuit() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def fail(*_args, **_kwargs):
        raise AssertionError("disabled understanding must not invoke this dependency")

    result = build_understanding(
        (),
        synopsis_order=(),
        enabled=False,
        concurrency=4,
        digest_chapter=fail,
        summarize_book=fail,
        style_brief=fail,
        load_analysis=fail,
        save_analysis=fail,
        save_digest=fail,
        emit_event=lambda name, attributes: events.append((name, dict(attributes))),
        progress=fail,
    )

    assert result == ""
    assert events == [("book_understanding_skipped", {"reason": "disabled"})]
