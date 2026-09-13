"""回填（TXT / EPUB）、报告、一致性 的测试（离线）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from tests.fixtures.books import (
    write_sample_epub,
    write_sample_txt,
)
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.assemble import assemble
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.models import Chapter
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application
from trans_novel.pipeline.contracts import NodeRequest
from trans_novel.pipeline.nodes.finish import AssembleNode, TitlesNode

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


class TestTitleTranslation(unittest.TestCase):
    def test_manifest_keeps_book_title_and_translates_chapter_titles(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            # 书名不翻译；章节标题译出并写回 manifest（fake：标题0/1）
            m = store.load_manifest()
            self.assertNotIn("title_translated", m)
            self.assertTrue(all(c.get("title_translated") for c in m["chapters"]))
            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                opf = z.read("OEBPS/content.opf").decode("utf-8")
            self.assertIn("サンプル小説", opf)  # OPF 书名保持原文
            self.assertIn("<dc:language>zh-Hans</dc:language>", opf)
            self.assertEqual(os.path.basename(out), "novel.zh.epub")

    def test_model_title_controls_are_removed_before_persistence(self):
        class FakeStore:
            def __init__(self):
                self.manifest = {
                    "title": "Book",
                    "chapters": [{"index": 0, "title": "Chapter"}],
                    "meta": {},
                }

            @staticmethod
            def pending_chapters():
                return []

            def load_manifest(self):
                return self.manifest

            @staticmethod
            def load_chapter(_index):
                return Chapter(index=0, title="Chapter")

            def save_manifest(self, manifest):
                self.manifest = manifest

            @staticmethod
            def log_event(*_args, **_kwargs):
                pass

        config = _config("state")
        client = FakeClient(
            handler=lambda *_args: json.dumps({"titles": ["前\x02后"]}, ensure_ascii=False)
        )
        node = TitlesNode(
            client=client,
            config=config,
            src="en",
            tgt="zh",
            glossary=SimpleNamespace(all_terms=list),
        )
        store = FakeStore()
        node.execute(
            NodeRequest(
                store=store,
                node_id="titles",
                key="titles",
                ci=None,
                scope="book",
                input_path="input.epub",
            )
        )

        self.assertEqual(store.manifest["chapters"][0]["title_translated"], "前后")

    def test_assemble_cleans_legacy_manifest_titles_once(self):
        class FakeStore:
            def __init__(self):
                self.manifest = {
                    "chapters": [{"title_translated": "前\x02后"}],
                    "meta": {"toc_entries": [{"title_translated": "目\x02录"}]},
                }
                self.saved = []

            def load_manifest(self):
                return self.manifest

            def save_manifest(self, manifest):
                self.saved.append(manifest)

            def load_state(self):
                return SimpleNamespace(chapters=[])

            def log_event(self, *_args, **_kwargs):
                pass

        store = FakeStore()
        config = Config.from_dict({"llm": fake_llm_dict()})
        node = AssembleNode(output=config.output, out_format="epub")
        request = NodeRequest(
            store=store,
            node_id="assemble",
            key="assemble",
            ci=None,
            scope="book",
            input_path="input.epub",
        )

        def fake_assemble(received_store, _input_path, _outputs, _out_format, **_kwargs):
            self.assertIs(received_store, store)
            self.assertEqual(received_store.manifest["chapters"][0]["title_translated"], "前后")
            self.assertEqual(
                received_store.manifest["meta"]["toc_entries"][0]["title_translated"], "目录"
            )
            return ["output.epub"]

        with patch("trans_novel.pipeline.nodes.finish.assemble_outputs", side_effect=fake_assemble):
            node.execute(request)

        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.saved[0]["chapters"][0]["title_translated"], "前后")
        self.assertEqual(store.saved[0]["meta"]["toc_entries"][0]["title_translated"], "目录")

    def test_rewrite_targets_propagates_to_titles(self):
        from trans_novel.pipeline.quality import rewrite_targets

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _cfg = _run(txt, os.path.join(d, "state"))
            # 手动写入含变体的标题译名
            m = store.load_manifest()
            m["title_translated"] = "佳穂传"
            m["chapters"][0]["title_translated"] = "佳穂登场"
            store.save_manifest(m)
            g = GlossaryStore(store.glossary_path)
            rewrite_targets(store, g, {"佳穂": "佳穗"})
            g.close()
            m2 = store.load_manifest()
            self.assertNotIn("title_translated", m2)  # 书名译名字段被清理
            self.assertEqual(m2["chapters"][0]["title_translated"], "佳穗登场")  # 章名已规范


if __name__ == "__main__":
    unittest.main()
