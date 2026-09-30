"""Workers preserve startup failures, cooperate with cancellation and export partial books."""

import asyncio
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag
from ebooklib import epub
from fastapi import HTTPException
from wenyi_api.job_service import start_job
from wenyi_api.workers import ExportWorkerSettings, WorkerSettings, tasks
from wenyi_core.config import Config
from wenyi_core.ingest.segmenter import load_document
from wenyi_core.pipeline.runstore import ExportSnapshotStore, source_sha256


@pytest.mark.parametrize("order", ["target_first", "source_first"])
@pytest.mark.parametrize("preserve_source_style", [False, True])
def test_bilingual_epub_export_keeps_untranslated_ruby_once(
    monkeypatch, tmp_path, order, preserve_source_style
):
    source = tmp_path / "source.epub"
    book = epub.EpubBook()
    book.set_identifier("partial-translation")
    book.set_title("Sample")
    book.set_language("ja")
    pages = []
    for name, body in [
        (
            "first.xhtml",
            '<p id="translated"><ruby>翻訳<rt>ほんやく</rt></ruby>済み</p>'
            '<p id="pending" class="body-text"><ruby>漢字<rt>かんじ</rt></ruby>です</p>'
            '<p><a href="last.xhtml#last">次へ</a></p>',
        ),
        ("last.xhtml", '<p id="last"><ruby>未訳<rt>みやく</rt></ruby>の章</p>'),
    ]:
        page = epub.EpubHtml(title=name, file_name=name, lang="ja")
        page.content = f"<html><body>{body}</body></html>"
        book.add_item(page)
        pages.append(page)
    book.toc = pages
    book.spine = pages
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(source), book)

    document = load_document(str(source), "ja", "en")
    assert len(document.chapters) == 2
    for chapter in document.chapters:
        for segment in chapter.segments:
            if segment.source.startswith("翻訳"):
                segment.target = "Already translated."
    manifest = {
        "title": document.title,
        "fmt": "epub",
        "source_lang": "ja",
        "target_lang": "en",
        "meta": document.meta,
        "chapters": [{"index": chapter.index} for chapter in document.chapters],
    }
    snapshot = ExportSnapshotStore(
        str(tmp_path / "run"), manifest, {chapter.index: chapter for chapter in document.chapters}
    )
    before = [chapter.model_dump() for chapter in document.chapters]

    def create_snapshot(*, actual_sha256):
        assert actual_sha256 == source_sha256(str(source))
        return snapshot

    storage = SimpleNamespace(create_export_snapshot=create_snapshot, assemble_lock=nullcontext)
    monkeypatch.setattr(tasks, "init_pool", lambda dsn: object())
    monkeypatch.setattr(tasks, "_pipeline_storage", lambda pid, pool: storage)
    monkeypatch.setattr(tasks, "_resolve_source", lambda pid: str(source))
    monkeypatch.setattr(
        tasks,
        "_build_config_for",
        lambda pid, run_id: Config.from_dict({"language": {"target": "en"}}),
    )
    monkeypatch.setattr(
        tasks.dal, "get_project", lambda pid: {"source_meta": {"original_filename": "source.epub"}}
    )
    monkeypatch.setattr(tasks.paths, "exports_dir", lambda pid: str(tmp_path / "exports"))
    published = []
    monkeypatch.setattr(
        tasks, "publish_export", lambda pool, pid, eid, path, **kw: published.append(path)
    )

    assert (
        tasks._render_export_sync(
            "p",
            export_id=1,
            fmt="epub",
            bilingual=True,
            order=order,
            preserve_source_style=preserve_source_style,
            about_page=False,
        )
        == 1
    )
    assert len(published) == 1
    assert Path(published[0]).name == "source.en-bi.epub"
    with ZipFile(published[0]) as archive:
        first = BeautifulSoup(archive.read("EPUB/first.xhtml"), "html.parser")
        last = BeautifulSoup(archive.read("EPUB/last.xhtml"), "html.parser")
    translated = first.find(id="translated")
    assert isinstance(translated, Tag)
    assert translated.get_text() == "Already translated."
    assert len(first.find_all(class_="tn-source")) == 1
    pending = first.find(id="pending")
    assert isinstance(pending, Tag)
    assert pending.get("class") == ["body-text"]
    assert len(first.find_all("ruby")) == 2
    assert first.find("a", href="last.xhtml#last") is not None
    assert len(last.find_all("p")) == 1
    assert last.find(class_="tn-source") is None
    last_paragraph = last.find(id="last")
    assert isinstance(last_paragraph, Tag)
    reading = last_paragraph.find("rt")
    assert isinstance(reading, Tag)
    assert reading.get_text() == "みやく"
    assert [chapter.model_dump() for chapter in document.chapters] == before
    assert [
        snapshot.load_chapter(chapter.index).model_dump() for chapter in document.chapters
    ] == before


@pytest.mark.parametrize(
    "kind",
    ["parse", "prepare", "translation", "chapter_translation", "review", "srt"],
)
def test_startup_failure_is_persisted(monkeypatch, kind):
    statuses = []
    monkeypatch.setattr(
        tasks.dal, "set_project_status", lambda pid, state, **kw: statuses.append((state, kw))
    )

    def fail(*args):
        raise RuntimeError("config missing")

    monkeypatch.setattr(tasks, "_execute", fail)
    with pytest.raises(RuntimeError, match="config missing"):
        asyncio.run(tasks._run(kind, "p", None, {}))
    assert statuses == [("error", {"error": "config missing"})]


def test_arq_cancellation_waits_for_state_to_be_flushed(monkeypatch):
    started, flushed = threading.Event(), threading.Event()

    def execute(kind, pid, run_id, params, stop):
        started.set()
        assert stop.wait(3)
        flushed.set()

    monkeypatch.setattr(tasks, "_execute", execute)

    async def run():
        job = asyncio.create_task(tasks._run("translation", "p", None, {}))
        await asyncio.to_thread(started.wait, 3)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert flushed.is_set()

    asyncio.run(run())


def test_exports_have_independent_queue():
    assert WorkerSettings.queue_name != ExportWorkerSettings.queue_name
    assert tasks.run_export not in WorkerSettings.functions
    assert tasks.run_export in ExportWorkerSettings.functions
    assert all("qa" not in fn.__name__ for fn in WorkerSettings.functions)


def test_model_comparison_cannot_be_enqueued():
    with pytest.raises(HTTPException) as raised:
        asyncio.run(start_job("test", "model_compare"))
    assert raised.value.status_code == 422
    assert all(fn.__name__ != "run_model_compare" for fn in WorkerSettings.functions)


def test_resolve_source_never_guesses_using_title(monkeypatch, tmp_path):
    source = tmp_path / "source.html"
    source.write_text("<p>Text</p>", encoding="utf-8")
    monkeypatch.setattr(
        tasks.dal, "get_project", lambda pid: {"title": None, "source_path": "source.html"}
    )
    monkeypatch.setattr(tasks, "settings", SimpleNamespace(data_dir=str(tmp_path)))
    assert tasks._resolve_source("p") == str(source)


def test_export_cancellation_waits_for_render_to_finish(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def render(*args, **kwargs):
        started.set()
        assert release.wait(3)
        finished.set()
        return 1

    monkeypatch.setattr(tasks, "_export_sync", render)

    async def run():
        task = asyncio.create_task(tasks.run_export({}, project_id="p", export_id=1, fmt="txt"))
        await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(run())
