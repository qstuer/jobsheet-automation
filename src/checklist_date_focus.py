"""Bounded first-PM-checklist date evidence, separate from cover ACTION DATE."""
import base64
from datetime import date
import io
import math
import os

import fitz
from PIL import Image, ImageDraw

from . import pm_field_cards

ENABLED_ENV = 'JOBSHEET_CHECKLIST_DATE_FOCUS'
ZOOMS = (5.0, 6.0)
SOURCE = 'pm_checklist_date_focus'
FIELD = 'checklist_header_date'
PAGE = 1


def require_read_only():
    if os.environ.get(ENABLED_ENV) != '1' or os.environ.get('JOBSHEET_DRY_RUN') != '1':
        raise ValueError('Checklist date focus requires explicit read-only mode')


def render(doc, page_idx, zoom):
    require_read_only()
    if (type(page_idx) is not int or page_idx != PAGE or len(doc) != 4 or
            type(zoom) not in (int, float) or not math.isfinite(zoom) or zoom <= 0):
        raise ValueError('Checklist date focus requires page 2 of a complete PM packet')
    from . import nvidia_client as vision
    page, zoom = doc[PAGE], min(zoom, 6.0)
    # The same calibrated Date box as the five-field PM header card. No
    # customer-specific offset, character segmentation or inpainting is used.
    l, t, r, b = pm_field_cards.FIELDS['date'][1]
    rect = page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(
        rect.x0 + l*rect.width, rect.y0 + t*rect.height,
        rect.x0 + r*rect.width, rect.y0 + b*rect.height))
    crop = vision._preprocess_field_image(Image.open(io.BytesIO(pix.tobytes('png'))), strong=zoom >= 6)
    crop.thumbnail((1100, 300), Image.Resampling.LANCZOS)
    card = Image.new('RGB', (max(480, crop.width+32), crop.height+76), 'white')
    ImageDraw.Draw(card).text((16, 12), 'FIRST PM CHECKLIST - DATE VALUE ONLY', fill='black')
    card.paste(crop, ((card.width-crop.width)//2, 52))
    output = io.BytesIO()
    card.save(output, 'JPEG', quality=92)
    return base64.b64encode(output.getvalue()).decode('ascii')


def summarize(records):
    """Two agreeing reads of the existing header field, not two paper sources.

    A serialization retry is not another record. This preserves the existing
    two-read PM header standard; signature-date proof remains separate.
    """
    result = {'status': 'not_attempted', 'date': None, 'read_count': 0, 'page_count': 0}
    if not records:
        return result
    if not isinstance(records, (list, tuple)) or len(records) > 2:
        return {**result, 'status': 'invalid'}
    values = {}
    for record in records:
        if (not isinstance(record, dict) or type(record.get('page')) is not int or
                record['page'] != PAGE or record.get('field') != FIELD or
                record.get('source') != SOURCE or type(record.get('zoom')) not in (float, int) or
                record['zoom'] not in ZOOMS or record['zoom'] in values):
            return {**result, 'status': 'invalid'}
        value = record.get('date')
        parsed = None
        if value is not None:
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError('Non-canonical date')
            except (TypeError, ValueError):
                return {**result, 'status': 'invalid'}
        values[record['zoom']] = parsed
    result.update(read_count=len(records), page_count=1)
    days = set(values.values()) - {None}
    if len(days) > 1:
        return {**result, 'status': 'conflict'}
    if set(values) == set(ZOOMS) and None not in values.values() and len(days) == 1:
        return {**result, 'status': 'confirmed', 'date': next(iter(days))}
    return {**result, 'status': 'insufficient'}
