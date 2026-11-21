"""Exercise the installed or frozen CLI's offline MinerU converter without credentials."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def main(executable: Path) -> None:
    with TemporaryDirectory(prefix="wenyi-mineru-cli-") as directory:
        work = Path(directory)
        source, output = work / "result.zip", work / "result.epub"
        equation = {
            "type": "equation_interline",
            "content": {"math_content": r"\sqrt{2}", "math_type": "latex"},
        }
        with ZipFile(source, "w") as archive:
            archive.writestr("example_content_list_v2.json", json.dumps([[equation]]))
        subprocess.run(
            [str(executable), "mineru-epub", str(source), str(output)], cwd=work, check=True
        )
        with ZipFile(output) as archive:
            chapter = ET.fromstring(archive.read("EPUB/source.xhtml"))
            # A visible LaTeX fallback does not count as a working math installation.
            if chapter.find(".//{http://www.w3.org/1998/Math/MathML}msqrt") is None:
                raise RuntimeError("The CLI did not render the square root as MathML.")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(strict=True))
