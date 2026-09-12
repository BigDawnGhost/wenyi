from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from trans_novel.ingest.epub.reader import read_epub


class TestEpubSemanticEvidence(unittest.TestCase):
    def test_preserves_xhtml_guide_and_landmark_evidence_on_logical_chapter(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="refs" href="refs.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine><itemref idref="refs"/></spine>
<guide><reference type="bibliography" title="Misleading" href="refs.xhtml#start"/></guide>
</package>"""
        nav = """<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol><li><a href="refs.xhtml#start">Anything</a></li></ol></nav>
<nav epub:type="landmarks"><ol><li><a epub:type="bibliography" role="doc-bibliography"
 href="refs.xhtml#start">Anything</a></li></ol></nav>
</body></html>"""
        refs = """<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:epub="http://www.idpf.org/2007/ops"><body>
<section epub:type="bibliography" role="doc-bibliography">
<h1 id="start">Ordinary heading</h1><p>Author. Work.</p></section>
</body></html>"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "semantic.epub")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
                archive.writestr(
                    "META-INF/container.xml",
                    """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>""",
                )
                archive.writestr("content.opf", opf)
                archive.writestr("nav.xhtml", nav)
                archive.writestr("refs.xhtml", refs)

            document = read_epub(path, "en", "zh")

        hints = document.chapters[0].meta["semantic_hints"]
        self.assertIn("opf:guide:type=bibliography", hints)
        self.assertIn("nav:landmark:type=bibliography", hints)
        self.assertIn("nav:landmark:role=doc-bibliography", hints)
        self.assertIn("xhtml:section:type=bibliography", hints)
        self.assertIn("xhtml:section:role=doc-bibliography", hints)
        self.assertNotIn("Misleading", "\n".join(hints))
        self.assertNotIn("Anything", "\n".join(hints))


if __name__ == "__main__":
    unittest.main()
