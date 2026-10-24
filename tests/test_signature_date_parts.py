"""Synthetic transcription/crop tests, with no customer answers or API calls."""
import base64
import io
import os
import unittest
from unittest.mock import patch

import fitz
from PIL import Image

from src import nvidia_client as vision, signature_date_cards as cards


class DatePartsTests(unittest.TestCase):
    def test_literal_groups_and_fenced_serialization_are_one_date(self):
        reply = '{"day":"09","month":"10","year":"2027"}'
        for text in (reply, '```json\n' + reply + '\n```'):
            self.assertEqual('2027-10-09', vision._parse_signature_date_parts_reply(text))

    def test_incomplete_group_is_not_invented_from_other_groups(self):
        self.assertIsNone(vision._parse_signature_date_parts_reply(
            '{"day":"09","month":null,"year":"2027"}'))

    def test_year_is_not_repaired_to_current_or_candidate_year(self):
        self.assertEqual('2007-10-09', vision._parse_signature_date_parts_reply(
            '{"day":"09","month":"10","year":"2007"}'))

    def test_invalid_ambiguous_nonliteral_or_extra_answers_are_rejected(self):
        for raw in (
                '{"day":"31","month":"02","year":"2027"}',
                '{"day":"09","month":"10","year":"207"}',
                '{"day":"09","month":"10","year":2027}',
                '{"day":"09","month":"1?","year":"2027"}',
                '{"day":"09","month":"10","year":"２０２７"}',
                '{"day":"09","month":"10","year":"2027","date":"2027-10-09"}',
                '{"day":"09","month":"10","year":"2027","year":"2007"}',
                '{"day":"09","month":"10","month":"11"}',
                '{"day":"09","month":"10","year":"2027"} {}',
                'Maybe {"day":"09","month":"10","year":"2027"}',
                '[["day","09"],["month","10"],["year","2027"]]',
                '{"day":{"day":"09"},"month":"10","year":"2027"}',
                None):
            with self.subTest(raw=raw), self.assertRaises(vision.NvidiaResponseError):
                vision._parse_signature_date_parts_reply(raw)

    def test_opt_in_reader_keeps_format_retry_a_single_panel_value(self):
        with patch.dict(os.environ, {cards.ENABLED_ENV: '1', 'JOBSHEET_DRY_RUN': '1'}), \
             patch.object(cards, 'render', return_value='synthetic-panel') as crop, \
             patch.object(vision, '_call_vision', side_effect=['bad',
                 '{"day":"09","month":"10","year":"2027"}']) as call:
            self.assertEqual('2027-10-09', vision.ocr_jobsheet_signature_date(
                None, 0, 'engineer_signed_date', zoom=5))
        crop.assert_called_once()
        self.assertEqual(2, call.call_count)
        for request in call.call_args_list:
            self.assertEqual('synthetic-panel', request.args[1])
            self.assertNotIn('2027', request.args[0])
            self.assertNotIn('candidate', request.args[0].lower())

    def test_opt_in_requires_dry_run_before_render_or_network(self):
        with patch.dict(os.environ, {cards.ENABLED_ENV: '1', 'JOBSHEET_DRY_RUN': '0'}), \
             patch.object(cards, 'render') as render, patch.object(vision, '_call_vision') as call:
            with self.assertRaises(ValueError):
                vision.ocr_jobsheet_signature_date(None, 0, 'engineer_signed_date')
        render.assert_not_called()
        call.assert_not_called()

    def test_flag_absent_preserves_existing_reader(self):
        with patch.dict(os.environ, {cards.ENABLED_ENV: '0'}), \
             patch.object(cards, 'render') as crop, \
             patch.object(vision, 'crop_jobsheet_field_card', return_value='legacy-panel'), \
             patch.object(vision, '_call_vision', return_value='{"signed_date":"09/10/2027"}'):
            self.assertEqual('2027-10-09', vision.ocr_jobsheet_signature_date(None, 0, 'engineer_signed_date'))
        crop.assert_not_called()


class DateCardTests(unittest.TestCase):
    def setUp(self):
        flags = patch.dict(os.environ, {cards.ENABLED_ENV: '1', 'JOBSHEET_DRY_RUN': '1'})
        flags.start()
        self.addCleanup(flags.stop)

    def test_checklist_crop_excludes_signature_above_and_keeps_upper_date_strokes(self):
        with fitz.open() as doc:
            for _ in range(4):
                doc.new_page(width=600, height=800)
            for field in ('checklist_engineer_signed_date', 'checklist_customer_signed_date'):
                l, t, r, b = cards.BOXES[field]
                # A synthetic signature in the row above; date strokes may
                # start slightly above the date rule, inside the retained box.
                doc[3].draw_rect(fitz.Rect(l*600, .714*800, r*600, .720*800),
                                 fill=(1, 0, 0), color=(1, 0, 0))
                doc[3].draw_rect(fitz.Rect((l+.02)*600, .726*800, (l+.04)*600, .760*800),
                                 fill=(0, 0, 1), color=(0, 0, 1))
            with patch.object(vision, '_preprocess_field_image', side_effect=lambda image, **_: image.convert('RGB')):
                for field in ('checklist_engineer_signed_date', 'checklist_customer_signed_date'):
                    image = Image.open(io.BytesIO(base64.b64decode(cards.render(doc, 3, field, 5))))
                    red = sum(r > 180 and g < 60 and b < 60 for r, g, b in image.getdata())
                    blue = sum(b > 180 and r < 60 and g < 60 for r, g, b in image.getdata())
                    self.assertEqual(0, red)
                    self.assertGreater(blue, 30)
                    self.assertLessEqual(image.width, 1132)

    def test_invalid_panel_page_zoom_or_incomplete_packet_stops_before_render(self):
        with fitz.open() as doc:
            for _ in range(3):
                doc.new_page()
            for page, field, zoom in ((2, 'checklist_engineer_signed_date', 5),
                                      (1, 'engineer_signed_date', 5),
                                      (0, 'unknown', 5), (0, 'engineer_signed_date', float('nan')),
                                      (0, 'engineer_signed_date', 0)):
                with self.subTest(page=page, field=field, zoom=zoom), self.assertRaises(ValueError):
                    cards.render(doc, page, field, zoom)


if __name__ == '__main__':
    unittest.main()
