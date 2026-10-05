"""Synthetic images only: no customer PDFs or external model calls."""
import base64
import io
import unittest
from unittest.mock import patch

import fitz
from PIL import Image

from src import config, nvidia_client


class VisionCardTests(unittest.TestCase):
    def test_higher_zoom_delivers_more_pixels_and_is_bounded(self):
        with fitz.open() as doc:
            doc.new_page()
            widths = []
            for zoom in (3, 4, 5, 6, 100):
                card = nvidia_client.crop_jobsheet_serial(doc, 0, zoom=zoom)
                with Image.open(io.BytesIO(base64.b64decode(card))) as image:
                    widths.append(image.width)
                    self.assertGreater(image.height, 0)
            self.assertEqual([1200, 1600, 2000, 2400, 2400], widths)

    def test_invalid_zoom_fails_before_render(self):
        for zoom in (0, -1, float("nan"), float("inf")):
            with patch.object(nvidia_client, "_render_field_crop") as render:
                with self.assertRaises(ValueError):
                    nvidia_client.crop_jobsheet_serial(None, 0, zoom=zoom)
                render.assert_not_called()

    def test_fixed_product_and_serial_crops_do_not_cross_into_neighbour(self):
        with fitz.open() as doc:
            page = doc.new_page(width=1000, height=1000)
            for field, color in (("product_raw", (1, 0, 0)),
                                 ("serial_candidates", (0, 0, 1))):
                left, top, right, bottom = config.OCR_FIELD_BOXES[field]
                page.draw_rect(fitz.Rect(left*1000, top*1000, right*1000, bottom*1000),
                               color=None, fill=color)
            with patch.object(nvidia_client, "_preprocess_field_image", side_effect=lambda img, **kw: img):
                for field, expected, forbidden in (("product_raw", (255, 0, 0), (0, 0, 255)),
                                                   ("serial_candidates", (0, 0, 255), (255, 0, 0))):
                    crop = nvidia_client._render_field_crop(doc, 0, field, 3)
                    self.assertEqual(expected, crop.getpixel((crop.width//2, crop.height//2)))
                    self.assertNotIn(forbidden, set(crop.getdata()))

    def test_transcription_includes_printed_values_but_not_field_labels(self):
        rules = nvidia_client._TRANSCRIPTION_RULES
        self.assertIn("printed, typed and handwritten VALUES", rules)
        self.assertIn("ignore printed field labels", rules)

    def test_focused_date_crop_keeps_ink_above_the_value_cell(self):
        # A handwritten month can extend above the value-cell top line. The
        # former 0.323 focused bound removed this stroke before model reading.
        with fitz.open() as doc:
            page = doc.new_page(width=1000, height=1000)
            page.draw_rect(fitz.Rect(590, 315, 600, 319),
                           color=None, fill=(1, 0, 0))
            with patch.object(nvidia_client, "_preprocess_field_image", side_effect=lambda img, **kw: img):
                crop = nvidia_client._render_field_crop(
                    doc, 0, "service_date_raw", 5, focused=True
                )
            self.assertIn((255, 0, 0), set(crop.getdata()))

    def test_signature_date_crops_do_not_overlap(self):
        with fitz.open() as doc:
            page = doc.new_page(width=1000, height=1000)
            for field, color in (("engineer_signed_date", (1, 0, 0)),
                                 ("customer_signed_date", (0, 0, 1))):
                left, top, right, bottom = config.OCR_FIELD_BOXES[field]
                page.draw_rect(fitz.Rect(left*1000, top*1000, right*1000, bottom*1000),
                               color=None, fill=color)
            with patch.object(nvidia_client, "_preprocess_field_image",
                              side_effect=lambda img, **kw: img):
                engineer = nvidia_client._render_field_crop(
                    doc, 0, "engineer_signed_date", 5
                )
                customer = nvidia_client._render_field_crop(
                    doc, 0, "customer_signed_date", 5
                )
            self.assertIn((255, 0, 0), set(engineer.getdata()))
            self.assertNotIn((0, 0, 255), set(engineer.getdata()))
            self.assertIn((0, 0, 255), set(customer.getdata()))
            self.assertNotIn((255, 0, 0), set(customer.getdata()))

    def test_circle_accepts_short_affirmative_reply_but_not_a_guess(self):
        for reply in ("not PM", "UNKNOWN PM", "probably CM", "PM or CM",
                      "The choices are CM, PM, FCO and INS", "PM is not circled", ""):
            with patch.object(nvidia_client, "_call_vision", return_value=reply):
                self.assertEqual("UNKNOWN", nvidia_client._read_job_nature("synthetic"))
        for reply, expected in (("PM", "PM"), (" cm ", "CM"),
                                ("FCO", "FCO"), ("INS", "INS"),
                                ("PM.", "PM"), ("The circled option is PM.", "PM"),
                                ('The word "CM" is circled.', "CM"),
                                ("PM is the circled word.", "PM")):
            with patch.object(nvidia_client, "_call_vision", return_value=reply):
                self.assertEqual(expected, nvidia_client._read_job_nature("synthetic"))

    def test_circle_request_has_room_for_short_fallback_sentence(self):
        with patch.object(nvidia_client, "_call_vision", return_value="PM") as call:
            self.assertEqual("PM", nvidia_client._read_job_nature("synthetic"))
        self.assertEqual(64, call.call_args.kwargs["max_tokens"])

    def test_unknown_circle_gets_only_one_wider_retry(self):
        with patch.object(nvidia_client, "crop_job_nature", return_value="synthetic") as crop, \
             patch.object(nvidia_client, "_read_job_nature", return_value="UNKNOWN") as read:
            self.assertEqual("UNKNOWN", nvidia_client.detect_cm_pm(None, 0))
        self.assertEqual(2, crop.call_count)
        self.assertEqual(2, read.call_count)
        self.assertEqual(config.CMPM_FALLBACK_TOP, crop.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
