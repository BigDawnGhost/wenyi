"""Export guard, resource-resolver and OPF-fallback tests (worklist-r2 X-5).

These pin the failure paths that implement AGENTS.md's invariant: source/state
disagreement must fail loudly instead of exporting a silent mismatch.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import pytest
from bs4 import BeautifulSoup, Tag
from wenyi_core.assemble.epub_navigation import _rewrite_opf_metadata
from wenyi_core.assemble.html_resources import _load_html_resource
from wenyi_core.assemble.writer import assemble
from wenyi_core.config import Config
from wenyi_core.ingest.models import KIND_TEXT  # noqa: F401 - documents segment kinds used below
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator

from tests.fake_llm import routing_handler
from tests.pipeline_fixtures import require_file_storage
from tests.sample_data import write_inline_sample_epub


def _run_full_pipeline(input_path: str, state_dir: str):
    """Local duplicate of test_assemble._run: collected test modules must not be imported."""
    cfg = Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "preset": "fake",
                "models": {
                    "default_strong": {"provider": "default", "model": "p"},
                    "default_cheap": {"provider": "default", "model": "f"},
                },
            },
            "pipeline": {"review": True, "review_autofix": False, "polish": True},
            "paths": {"state_dir": state_dir},
        }
    )
    orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
    return require_file_storage(orch.run(input_path)), cfg


def _rewrite_zip_excluding(src_path: str, dest_path: str, drop: str) -> None:
    with zipfile.ZipFile(src_path) as src, zipfile.ZipFile(dest_path, "w") as dest:
        for info in src.infolist():
            if info.filename == drop:
                continue
            dest.writestr(info, src.read(info.filename))


def _write_resource_epub(path: Path, *, include_image: bool) -> None:
    """Build a root-level chapter with a missing resource and an optional ZIP image."""
    container = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    image_item = (
        '<item id="image" href="cover.png" media-type="image/png"/>' if include_image else ""
    )
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Resource test</dc:title><dc:language>ja</dc:language></metadata>"
        '<manifest><item id="chapter" href="chapter.xhtml" '
        f'media-type="application/xhtml+xml"/>{image_item}</manifest>'
        '<spine><itemref idref="chapter"/></spine></package>'
    )
    image = '<img id="cover" src="cover.png"/>' if include_image else ""
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>静かな朝だった。</p>'
        f'<img id="missing" src="local-only.txt"/>{image}</body></html>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("content.opf", opf)
        archive.writestr("chapter.xhtml", chapter)
        if include_image:
            archive.writestr("cover.png", b"epub-image")


def _image_source(soup: BeautifulSoup, image_id: str) -> str:
    image = soup.find("img", id=image_id)
    assert isinstance(image, Tag)
    source = image.get("src")
    assert isinstance(source, str)
    return source


@pytest.mark.parametrize("zip_resources", [{}, {"other.png": b"other-image"}])
def test_zip_resource_lookup_never_reads_missing_local_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, zip_resources: dict[str, bytes]
) -> None:
    local = tmp_path / "local-only.txt"
    local.write_bytes(b"private-local-data")
    monkeypatch.chdir(tmp_path)

    for reference in (
        "local-only.txt",
        "local%2Donly.txt",
        f"../{tmp_path.name}/local-only.txt",
        str(local),
        local.as_uri(),
    ):
        assert _load_html_resource(reference, source_dir="", zip_resources=zip_resources) is None, (
            reference
        )


def test_zip_resource_lookup_cannot_be_shadowed_by_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cover.png").write_bytes(b"local-image")
    monkeypatch.chdir(tmp_path)

    assert _load_html_resource(
        "cover.png", source_dir="", zip_resources={"cover.png": b"epub-image"}
    ) == ("image/png", b"epub-image")


@pytest.mark.parametrize(
    "reference",
    ["../Images/cover%20image.png?version=1#sample", "OEBPS/Images/cover%20image.png"],
)
def test_zip_resource_lookup_preserves_container_paths(reference: str) -> None:
    assert _load_html_resource(
        reference,
        source_dir="OEBPS/Text",
        zip_resources={"OEBPS/Images/cover image.png": b"epub-image"},
    ) == ("image/png", b"epub-image")


@pytest.mark.parametrize("zip_resources", [None, {}], ids=["local", "zip"])
def test_html_resource_modes_preserve_data_and_external_uris(
    zip_resources: dict[str, bytes] | None,
) -> None:
    assert _load_html_resource(
        "data:image/png;base64,aW5saW5l", source_dir="", zip_resources=zip_resources
    ) == ("image/png", b"inline")
    assert _load_html_resource(
        "data:image/svg+xml,%3Csvg%2F%3E", source_dir="", zip_resources=zip_resources
    ) == ("image/svg+xml", b"<svg/>")
    for reference in ("https://example.invalid/cover.png", "//example.invalid/cover.png", "#cover"):
        assert _load_html_resource(reference, source_dir="", zip_resources=zip_resources) is None, (
            reference
        )


@pytest.mark.parametrize("local_directory", ["cwd", "source"])
@pytest.mark.parametrize("include_image", [False, True], ids=["empty-media", "zip-image"])
@pytest.mark.parametrize("bilingual", [False, True], ids=["monolingual", "bilingual"])
def test_epub_html_export_never_packages_files_outside_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_directory: str,
    include_image: bool,
    bilingual: bool,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    local = cwd if local_directory == "cwd" else source_dir
    (local / "local-only.txt").write_bytes(b"private-local-data")
    (local / "cover.png").write_bytes(b"local-image")
    source = source_dir / "book.epub"
    _write_resource_epub(source, include_image=include_image)
    monkeypatch.chdir(cwd)
    store, _ = _run_full_pipeline(str(source), str(tmp_path / "state"))
    assert any(segment.target for segment in store.load_chapter(0).segments)

    output = Path(
        assemble(
            store,
            str(source),
            out_path=str(tmp_path / "export" / "book.html"),
            out_format="html",
            bilingual=bilingual,
            about_page=False,
        )
    )

    soup = BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
    assert _image_source(soup, "missing") == "local-only.txt"
    assets = output.with_suffix(".assets")
    if include_image:
        assert [path.read_bytes() for path in assets.iterdir()] == [b"epub-image"]
        image_href = _image_source(soup, "cover")
        assert image_href.startswith("book.assets/")
        assert (output.parent / image_href).read_bytes() == b"epub-image"
    else:
        assert not assets.exists()


def test_html_input_still_packages_local_and_embedded_images(tmp_path: Path) -> None:
    source = tmp_path / "source.html"
    source.write_text(
        "<html><body><p>静かな朝だった。</p>"
        '<img id="local" src="cover.png"/>'
        '<img id="inline" src="data:image/png;base64,aW5saW5l"/>'
        '<img id="external" src="https://example.invalid/cover.png"/>'
        "</body></html>",
        encoding="utf-8",
    )
    (tmp_path / "cover.png").write_bytes(b"local-image")
    store, _ = _run_full_pipeline(str(source), str(tmp_path / "state"))

    output = Path(assemble(store, str(source), out_format="html", about_page=False))

    soup = BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
    for image_id, payload in (("local", b"local-image"), ("inline", b"inline")):
        href = _image_source(soup, image_id)
        assert href.startswith(f"{output.stem}.assets/")
        assert (output.parent / href).read_bytes() == payload
    assert _image_source(soup, "external") == "https://example.invalid/cover.png"


class TestEpubResourceGuards(unittest.TestCase):
    def _prepared(self, directory: str):
        epub = os.path.join(directory, "inline.epub")
        write_inline_sample_epub(epub)
        store, _ = _run_full_pipeline(epub, os.path.join(directory, "state"))
        return epub, store

    def test_undeclared_body_resource_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            epub, store = self._prepared(directory)
            chapter = store.load_chapter(0)
            chapter.segments[0].resource_href = "OEBPS/ghost.xhtml"
            store.save_chapter(chapter)

            with self.assertRaisesRegex(ValueError, "undeclared body resources"):
                assemble(store, epub, out_format="epub", about_page=False)

    def test_missing_body_resource_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            epub, store = self._prepared(directory)
            trimmed = os.path.join(directory, "trimmed.epub")
            _rewrite_zip_excluding(epub, trimmed, "OEBPS/ch1.xhtml")

            with self.assertRaisesRegex(ValueError, "body resource not found"):
                assemble(store, trimmed, out_format="epub", about_page=False)

    def test_stripped_backfill_anchors_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            epub, store = self._prepared(directory)
            with zipfile.ZipFile(epub) as src:
                ch1 = src.read("OEBPS/ch1.xhtml").decode("utf-8")
                entries = {
                    info.filename: (info, src.read(info.filename)) for info in src.infolist()
                }
            # Anchors are re-derived from the source at export time; removing the
            # anchored element entirely makes the fresh annotation miss the anchors the
            # state requires (robust under both id-based and ordinal anchor schemes).
            anchored_span = '<span id="kobo.1.1">Je suis là, sous le pommier.</span>'
            self.assertIn(anchored_span, ch1)
            rewritten = ch1.replace(anchored_span, "")
            trimmed = os.path.join(directory, "stripped.epub")
            with zipfile.ZipFile(trimmed, "w") as dest:
                for info, data in entries.values():
                    dest.writestr(
                        info,
                        rewritten.encode("utf-8") if info.filename == "OEBPS/ch1.xhtml" else data,
                    )

            with self.assertRaisesRegex(ValueError, "lacks backfill anchors"):
                assemble(store, trimmed, out_format="epub", about_page=False)


class TestHtmlResourceResolver(unittest.TestCase):
    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            inside = Path(directory) / "keep.png"
            inside.write_bytes(b"inside")
            outside_dir = Path(directory).parent
            outside = outside_dir / "escape.png"
            outside.write_bytes(b"outside")
            try:
                loaded = _load_html_resource("keep.png", source_dir=directory)
                self.assertIsNotNone(loaded)
                escaped = _load_html_resource(
                    "../escape.png", source_dir=str(Path(directory) / "sub")
                )
                self.assertIsNone(escaped)
            finally:
                outside.unlink(missing_ok=True)

    def test_invalid_base64_data_uri_is_rejected(self):
        self.assertIsNone(
            _load_html_resource("data:image/png;base64,!!!not-base64!!!", source_dir=".")
        )


class TestOpfMetadataFallbacks(unittest.TestCase):
    def test_opf_without_title_and_language_gets_language_created(self):
        opf = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="bookid"><metadata/></package>'
        )
        encoded = _rewrite_opf_metadata(
            opf.encode("utf-8"), book_title="T", lang="zh-Hans", force_horizontal=False
        )
        text = encoded.decode("utf-8")
        self.assertIn("<dc:language>zh-Hans</dc:language>", text)
        # No dc:title/title element exists, so the title branch leaves the OPF otherwise
        # untouched (the fallback exists for damaged metadata, not synthesis).
        self.assertNotIn("<dc:title>", text)


if __name__ == "__main__":
    unittest.main()
