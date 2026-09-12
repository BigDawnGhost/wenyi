"""回填（TXT / EPUB）、报告、一致性 的测试（离线）。"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from tests.fixtures.books import (
    write_sample_txt,
)
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.assemble.report import build_report
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application

_FB2_WITH_IMAGES = """\
<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"
             xmlns:xlink="http://www.w3.org/1999/xlink">
<description><title-info>
  <book-title>Illustrated Book</book-title>
  <coverpage><image xlink:href="#cover.jpg"/></coverpage>
</title-info></description>
<body><section><title><p>Chapter</p></title>
  <image xlink:href="#inside.png"/><p>Illustrated text.</p>
</section></body>
<binary id="cover.jpg" content-type="image/jpeg">Y292ZXItYnl0ZXM=</binary>
<binary id="inside.png" content-type="image/png">aW5zaWRlLWJ5dGVz</binary>
</FictionBook>
"""


def _write_vertical_epub(path: str) -> None:
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>縦書き小説</dc:title>
    <dc:language>ja</dc:language>
  </metadata>
  <manifest>
    <item id="style" href="style.css" media-type="text/css"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine page-progression-direction="rtl">
    <itemref idref="ch1"/>
  </spine>
</package>
"""
    ch1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" class="vrtl"><head>
<title>第一章</title><link rel="stylesheet" href="style.css"/>
</head><body>
<h1>第一章　出会い</h1>
<p>綾小路は教室の窓際に座っていた。</p>
</body></html>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/style.css", "html { writing-mode: vertical-rl; }")
        zf.writestr("OEBPS/ch1.xhtml", ch1)


def _config(state_dir: str):
    config = Config.from_dict({"llm": fake_llm_dict(), "quality": "quality"})
    config.source_lang = "ja"
    config.state_dir = state_dir
    return config


def _run(input_path, state_dir):
    cfg = _config(state_dir)
    orch = Application(cfg, client=FakeClient(handler=routing_handler))
    store = orch.run(input_path)
    _stamp_formal_prereqs(store)
    return store, cfg


def _stamp_formal_prereqs(store):
    """Direct writer tests stamp title, QA, Repair, and report prerequisites."""
    from trans_novel.pipeline.state import NODE_DETERMINISTIC_QA, NODE_REPAIR, NodeState

    state = store.load_state()
    for node_id in ("titles", NODE_DETERMINISTIC_QA, NODE_REPAIR, "report"):
        state.nodes.setdefault(node_id, NodeState(node_id=node_id, status="succeeded"))
    store.save_state(state)
    return store


class TestReport(unittest.TestCase):
    def test_report_summary(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            g = GlossaryStore(store.glossary_path)
            report = build_report(store, g)
            g.close()
            s = report["summary"]
            self.assertEqual(s["chapters_done"], s["chapters_total"])
            self.assertEqual(s["empty_targets"], 0)  # 全部段都有译文
            self.assertGreaterEqual(s["terms"], 1)

    def test_classified_report_uses_chapter_processing_schema(self):
        from trans_novel.ingest.models import ChapterProcessing

        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            write_sample_txt(source)
            store, _ = _run(source, os.path.join(directory, "state"))
            state = store.load_state()
            state.chapters[0].processing = ChapterProcessing(
                action="preserve",
                review_required=False,
                reason="reference list",
                source_sha256="source",
                strategy_version="chapter_semantics_v1",
            )
            state.chapters[1].processing = ChapterProcessing(
                action="translate",
                review_required=True,
                reason="mixed content",
                source_sha256="source",
                strategy_version="chapter_semantics_v1",
            )
            store.save_state(state)
            glossary = GlossaryStore(store.glossary_path)
            report = build_report(store, glossary)
            glossary.close()

            self.assertEqual(
                report["chapter_processing"],
                {
                    "preserved": [
                        {
                            "chapter": state.chapters[0].index,
                            "title": state.chapters[0].title,
                            "reason": "reference list",
                        }
                    ],
                    "review_required": [
                        {
                            "chapter": state.chapters[1].index,
                            "title": state.chapters[1].title,
                            "reason": "mixed content",
                        }
                    ],
                },
            )
            self.assertNotIn("back_matter_chapters", report)
            self.assertNotIn("back_matter_chapters", report["summary"])


if __name__ == "__main__":
    unittest.main()
