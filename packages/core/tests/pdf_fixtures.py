"""Small, searchable PDFs for offline export tests."""

from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    RectangleObject,
)

from tests.document_assertions import require


def pdf_dictionary(root: object, *keys: str) -> DictionaryObject:
    """Resolve a chain of PDF dictionaries, checking each intermediate type."""
    node = require(root, DictionaryObject)
    for key in keys:
        node = require(node[key], DictionaryObject)
    return node


def pdf_bytes(
    labels: list[str],
    *,
    width: float = 200,
    height: float = 300,
    rotation: int = 0,
    cropbox: tuple[float, float, float, float] | None = None,
    user_unit: float = 1,
) -> bytes:
    """Draw vector frames and ASCII labels without a renderer or external fonts."""
    with PdfWriter() as writer:
        for label in labels:
            page = writer.add_blank_page(width=width, height=height)
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {
                            NameObject("/F1"): DictionaryObject(
                                {
                                    NameObject("/Type"): NameObject("/Font"),
                                    NameObject("/Subtype"): NameObject("/Type1"),
                                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                                }
                            )
                        }
                    )
                }
            )
            content = DecodedStreamObject()
            content.set_data(
                (
                    f"0.2 0.4 0.6 RG 2 w 40 50 {width - 80} {height - 100} re S\n"
                    f"BT /F1 12 Tf 60 80 Td ({label}) Tj ET\n"
                ).encode("ascii")
            )
            # A PDF stream must be an indirect object.
            page[NameObject("/Contents")] = writer._add_object(content)
            if cropbox is not None:
                page.cropbox = RectangleObject(cropbox)
            if rotation:
                page.rotate(rotation)
            page[NameObject("/UserUnit")] = FloatObject(user_unit)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()


def text_positions(page):
    positions = {}

    def locate(operator, operands, cm, tm):
        if operator == b"Tj":
            text = operands[0]
            label = text.decode("ascii") if isinstance(text, bytes) else str(text)
            positions[label] = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]

    page.extract_text(visitor_operand_before=locate)
    return positions
