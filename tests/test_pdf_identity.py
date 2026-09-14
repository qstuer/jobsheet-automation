"""PDF 視覺防重複測試。"""
import tempfile
import unittest
from pathlib import Path

import fitz

from src import pdf_identity


def _make_pdf(path: Path, text: str, metadata_title: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(50, 50, 545, 790), color=(0, 0, 0), width=2)
    page.insert_text((80, 120), text, fontsize=28)
    if text.startswith("COMPLETELY"):
        page.draw_rect(fitz.Rect(80, 200, 500, 500), color=(0, 0, 0), fill=(0, 0, 0))
    doc.set_metadata({"title": metadata_title})
    doc.save(str(path))
    doc.close()


@unittest.skipUnless(isinstance(getattr(fitz, "VersionBind", None), str),
                     "PyMuPDF is not installed in the lightweight test interpreter")
class PdfIdentityTests(unittest.TestCase):
    def test_reencoded_same_page_is_visually_identical(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            left = Path(tmpdir) / "left.pdf"
            right = Path(tmpdir) / "right.pdf"
            _make_pdf(left, "SR 61932685", "first encoding")
            _make_pdf(right, "SR 61932685", "different metadata")
            self.assertNotEqual(left.read_bytes(), right.read_bytes())
            self.assertTrue(pdf_identity.visually_same_pdf(left, right))

    def test_materially_changed_page_is_not_identical(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            left = Path(tmpdir) / "left.pdf"
            right = Path(tmpdir) / "right.pdf"
            _make_pdf(left, "SR 61932685", "same")
            _make_pdf(right, "COMPLETELY DIFFERENT DOCUMENT", "same")
            self.assertFalse(pdf_identity.visually_same_pdf(left, right))


if __name__ == "__main__":
    unittest.main()
