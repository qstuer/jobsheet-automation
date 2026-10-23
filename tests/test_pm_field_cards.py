"""Synthetic fixed-layout PM cards; never contact a model or use real answers."""
import base64
from contextlib import ExitStack
import io
import json
import unittest
from unittest.mock import patch

import fitz
from PIL import Image

from src import matching_rules as rules, nvidia_client as vision, pm_field_cards as cards
from tests.test_matching_rules import reading, task


class PMFieldCardTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict('os.environ', {cards.ENABLED_ENV: '1', 'JOBSHEET_DRY_RUN': '1'}))
        self.doc = fitz.open()
        self.addCleanup(self.doc.close)
        for _ in range(4):
            self.doc.new_page(width=600, height=850)

    def test_real_renderer_keeps_each_header_value_and_excludes_neighbours(self):
        # Physical colour bands verify actual source clipping, not a mocked
        # field image. No colour/character is generated into the model input.
        for number, (label, box) in enumerate(cards.FIELDS.values(), 1):
            l, t, r, b = box
            rect = fitz.Rect(l * 600 + 3, t * 850 + 2, r * 600 - 3, b * 850 - 2)
            self.doc[1].draw_rect(rect, color=None, fill=(number / 10,)*3)
        self.doc[1].draw_rect(fitz.Rect(0, 250, 600, 850), color=None, fill=(0, 0, 0))
        self.doc[3].draw_rect(self.doc[3].rect, color=None, fill=(0, 0, 0))
        for zoom in (5., 6.):
            seen = []
            def capture(image, strong=False):
                seen.append(image.convert('L'))
                return image.convert('RGB')
            with patch.object(vision, '_preprocess_field_image', side_effect=capture):
                image = Image.open(io.BytesIO(base64.b64decode(cards.render(self.doc, zoom))))
            self.assertEqual((1600, 754), image.size)
            self.assertEqual(5, len(seen))
            for number, crop in enumerate(seen, 1):
                self.assertAlmostEqual(number * 25.5, crop.getpixel((crop.width // 2, crop.height // 2)), delta=2)
            # Spare sixth panel is blank, not another invented field.
            self.assertEqual((255, 255, 255), image.getpixel((1200, 650)))

    def test_rejects_incomplete_document_and_invalid_zoom(self):
        for zoom in (0, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                cards.render(self.doc, zoom)
        with fitz.open() as incomplete:
            incomplete.new_page()
            with self.assertRaises(ValueError):
                cards.render(incomplete, 5)

    def test_tall_serial_strokes_and_below_line_asset_correction_retained(self):
        serial = fitz.Rect(.43 * 600, .167 * 850, .45 * 600, .171 * 850)
        corrected_asset = fitz.Rect(.73 * 600, .221 * 850, .77 * 600, .230 * 850)
        for rect in (serial, corrected_asset):
            self.doc[1].draw_rect(rect, color=None, fill=(0, 0, 0))
        seen = []
        def capture(image, strong=False):
            seen.append(image.convert('L'))
            return image.convert('RGB')
        with patch.object(vision, '_preprocess_field_image', side_effect=capture):
            cards.render(self.doc, 6)
        self.assertEqual(0, seen[3].getextrema()[0])
        self.assertEqual(0, seen[4].getextrema()[0])
        self.assertGreater(cards.FIELDS['asset'][1][3], .230)
        self.assertLess(cards.FIELDS['asset'][1][3], .260)

    def test_read_only_gate_precedes_renderer_and_ocr(self):
        with patch.dict('os.environ', {'JOBSHEET_DRY_RUN': '0'}), patch.object(vision, '_call_vision') as call:
            with self.assertRaises(ValueError):
                rules._checklist_read(self.doc, 5)
            with self.assertRaises(ValueError):
                rules.read_and_match(self.doc, 'PM')
        call.assert_not_called()

    def test_two_existing_readers_keep_page_provenance_and_no_answer_hints(self):
        raw = json.dumps({'hospital': 'Test Hospital', 'product': 'Affiniti 70',
                          'serial': 'ABC12X3456', 'date': '28/9/2026', 'asset': None})
        with patch.object(vision, '_call_vision', return_value=raw) as call:
            values = [rules._checklist_read(self.doc, z) for z in (5, 6)]
        self.assertEqual(2, call.call_count)
        for value in values:
            self.assertEqual((1, 'pm_checklist_header'), (value['_page'], value['_source']))
            self.assertEqual(['ABC12X3456'], value['serial_candidates'])
        for request in call.call_args_list:
            self.assertNotIn('ABC12X3456', request.kwargs['prompt'])
            self.assertNotIn('Test Hospital', request.kwargs['prompt'])
        ev = rules.evidence(values)
        self.assertEqual(2, ev['serial_candidates']['ABC12X3456']['votes'])
        self.assertEqual({1: 2}, ev['serial_candidates']['ABC12X3456']['pages'])

    def test_uncertain_serial_is_not_completed_or_promoted_to_identity(self):
        raw = json.dumps({'hospital': None, 'product': None, 'serial': 'ABC?2X3456', 'date': None, 'asset': None})
        with patch.object(vision, '_call_vision', return_value=raw):
            result = rules._checklist_read(self.doc, 5)
        self.assertEqual([], result['serial_candidates'])
        self.assertIsNone(rules.assess_task(task(), rules.evidence([result, result]), 'PM'))

    def test_extra_fields_or_wrong_types_rejected_without_votes(self):
        for extra in ({'serial': ['ABC12X3456']}, {'invented': 'value'}):
            raw = {'hospital': None, 'product': None, 'serial': None, 'date': None, 'asset': None, **extra}
            with patch.object(vision, '_call_vision', return_value=json.dumps(raw)):
                with self.assertRaises(vision.NvidiaResponseError):
                    rules._checklist_read(self.doc, 5)

    def test_disabled_flag_preserves_original_strip(self):
        raw = json.dumps(dict.fromkeys(cards.FIELDS))
        with patch.dict('os.environ', {cards.ENABLED_ENV: '0'}), patch.object(
                cards, 'render') as render, patch.object(vision, '_call_vision', return_value=raw) as call:
            rules._checklist_read(self.doc, 5)
        render.assert_not_called()
        self.assertEqual(rules.CHECKLIST_PROMPT, call.call_args.kwargs['prompt'])

    def test_unchanged_matching_rules_still_reject_wrong_hospital_type_and_visit(self):
        for wrong in (task(hospital='Different Hospital'), task(kind='CM'), task(due='2026-07-01')):
            chosen, _ = rules.select_task([wrong], rules.evidence([reading(), reading()]), 'PM', None)
            self.assertIsNone(chosen)
        # A mismatch phone does not become a match just because a PM card exists.
        ev = rules.evidence([reading(serial='ABC12X3400', phone='29876543')]*2)
        self.assertIsNone(rules.assess_task(task(), ev, 'PM'))


if __name__ == '__main__':
    unittest.main()
