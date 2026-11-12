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
            page[NameObject("/Contents")] = content
            if cropbox is not None:
                page.cropbox = RectangleObject(cropbox)
            if rotation:
                page.rotate(rotation)
            page[NameObject("/UserUnit")] = FloatObject(user_unit)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
