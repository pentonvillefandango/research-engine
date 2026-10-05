"""Write tests/fixtures/pages/sample.pdf: a tiny 2-page PDF with Info metadata. Run once."""

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "pages" / "sample.pdf"
PAGES = [
    "Widget Pro pricing sheet. Plan Free costs 0. Plan Pro costs 10 per month.",
    "Support hours are 9 to 5. Contact sales for enterprise volume discounts.",
]


def build() -> bytes:
    objs: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(PAGES)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(PAGES)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(PAGES):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
            ).encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<< /Title (Sample Vendor Sheet) /Author (Test Author) >>")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {len(objs)} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


if __name__ == "__main__":
    OUT.write_bytes(build())
    print("wrote", OUT)
