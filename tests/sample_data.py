"""生成测试用样本：日文 TXT 与最小 EPUB。"""

from __future__ import annotations

import zipfile

SAMPLE_TXT = """\
# 第一章　出会い

綾小路は教室の窓際に座っていた。空はどこまでも青く、遠くで鳥が鳴いていた。

「おはよう、綾小路くん」と堀北が声をかけた。彼女はいつも通り無表情だった。

綾小路は小さく頷いた。何も言わなかった。

# 第二章　放課後

放課後、二人は屋上で待ち合わせた。風が強かった。

「先輩、これからどうするつもりですか」と堀北が尋ねた。
"""


def write_sample_txt(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(SAMPLE_TXT)


_CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>サンプル小説</dc:title>
    <dc:language>ja</dc:language>
  </metadata>
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

_CH1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title></head>
<body>
<h1>第一章　出会い</h1>
<p>綾小路は教室の窓際に座っていた。</p>
<p>「おはよう、綾小路くん」と堀北が声をかけた。</p>
</body></html>
"""

_CH2 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第二章</title></head>
<body>
<h1>第二章　放課後</h1>
<p>放課後、二人は屋上で待ち合わせた。風が強かった。</p>
</body></html>
"""


def write_sample_epub(path: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype 必须最先写且不压缩
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", _OPF)
        zf.writestr("OEBPS/ch1.xhtml", _CH1)
        zf.writestr("OEBPS/ch2.xhtml", _CH2)
