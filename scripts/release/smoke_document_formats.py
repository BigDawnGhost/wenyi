"""Exercise document format adapters from a clean, installed Core package."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.segmenter import load_document
from wenyi_core.markup import _epub_native
from wenyi_core.storage.file import FileStorage


def _translate_and_export(source: Path, output: Path, original: str, target: str) -> None:
    document = load_document(str(source), source_lang="en", target_lang="zh")
    matches = [
        segment
        for chapter in document.chapters
        for segment in chapter.segments
        if segment.source == original
    ]
    if len(matches) != 1:
        raise SystemExit(f"{source.suffix}: expected exactly one matching input paragraph")
    matches[0].target = target
    with closing(FileStorage(str(output.with_suffix(".state")))) as store:
        store.save_manifest(store.stage_document(document))
        assemble(store, str(source), str(output), out_format=output.suffix[1:], about_page=False)


def _check_pdf_spread(root: Path) -> None:
    source, target = root / "left.pdf", root / "right.pdf"
    for path, width in ((source, 100), (target, 120)):
        with PdfWriter() as writer:
            page = writer.add_blank_page(width, 150)
            font = DictionaryObject(
                {
                    NameObject(k): NameObject(v)
                    for k, v in (
                        ("/Type", "/Font"),
                        ("/Subtype", "/Type1"),
                        ("/BaseFont", "/Helvetica"),
                    )
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            content = DecodedStreamObject()
            content.set_data(f"BT /F1 12 Tf 10 20 Td ({path.stem}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(content)
            writer.write(path)
    prepared = _epub_native.pdf_spread_source(source.read_bytes())
    if isinstance(prepared, str):
        raise SystemExit(f"Native PDF smoke source rejected: {prepared}")
    fallback = _epub_native.pdf_write_spreads(prepared, target, [0], True)
    if fallback is not None:
        raise SystemExit(f"Native PDF smoke fixture fell back: {fallback}")
    with PdfReader(target) as reader:
        if len(reader.pages) != 1 or reader.pages[0].mediabox.width != 220:
            raise SystemExit("Native PDF spread did not contain both pages")
        if reader.pages[0].extract_text(extraction_mode="layout").split() != ["left", "right"]:
            raise SystemExit("Native PDF spread lost searchable text or changed its reading order")


def main() -> None:
    with TemporaryDirectory(prefix="wenyi-formats-") as directory:
        root = Path(directory)
        markdown = root / "input.md"
        markdown.write_text("# Sample\n\nNative smoke paragraph.\n", encoding="utf-8")
        docx = root / "word.docx"
        html = root / "page.html"
        epub = root / "book.epub"
        text = root / "plain.txt"
        _translate_and_export(markdown, docx, "Native smoke paragraph.", "一段正文。")
        _translate_and_export(docx, html, "一段正文。", "网页正文。")
        _translate_and_export(html, epub, "网页正文。", "电子书正文。")
        _translate_and_export(epub, text, "电子书正文。", "最终正文。")
        if "最终正文。" not in text.read_text(encoding="utf-8").splitlines():
            raise SystemExit("The text export did not contain the translated paragraph")
        _check_pdf_spread(root)
    print("Installed Core document formats: Markdown → DOCX → HTML → EPUB → TXT passed")
    print("Installed Core bilingual PDF spread composition passed")


if __name__ == "__main__":
    main()
