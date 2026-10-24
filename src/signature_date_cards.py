"""Opt-in read-only date-value crops; no answers, task dates or network calls."""
import base64
import io
import math
import os

import fitz
from PIL import Image, ImageDraw

ENABLED_ENV = 'JOBSHEET_SIGNATURE_DATE_PARTS'
# Focus on printed date rows, with bleed above the rule for tall handwriting.
# Some signature/label ink can remain in that bleed. Never erase stamps or
# reconstruct obscured digits just to produce a visually cleaner panel.
BOXES = {
    'engineer_signed_date': (0.245, 0.938, 0.475, 0.982),
    'customer_signed_date': (0.780, 0.938, 0.975, 0.984),
    'checklist_engineer_signed_date': (0.130, 0.724, 0.430, 0.795),
    'checklist_customer_signed_date': (0.620, 0.724, 0.940, 0.795),
}


def require_read_only():
    if os.environ.get(ENABLED_ENV) != '1' or os.environ.get('JOBSHEET_DRY_RUN') != '1':
        raise ValueError('Signature date parts require explicit read-only mode')


def render(doc, page_idx, field, zoom):
    require_read_only()
    if field not in BOXES or not math.isfinite(zoom) or zoom <= 0:
        raise ValueError('Invalid signature date panel')
    if (field.startswith('checklist_') and (len(doc) != 4 or page_idx != 3)) or (
            not field.startswith('checklist_') and page_idx != 0):
        raise ValueError('Signature date panel provenance mismatch')
    from . import nvidia_client as vision
    page, zoom = doc[page_idx], min(zoom, 6.0)
    l, t, r, b = BOXES[field]
    rect = page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(
        rect.x0 + l*rect.width, rect.y0 + t*rect.height,
        rect.x0 + r*rect.width, rect.y0 + b*rect.height))
    crop = vision._preprocess_field_image(Image.open(io.BytesIO(pix.tobytes('png'))), strong=zoom >= 6)
    # Avoid a 2000px-wide strip being reduced to tiny strokes by the backend.
    # Use the full unaltered date row; no character-level segmentation/guessing.
    crop.thumbnail((1100, 300), Image.Resampling.LANCZOS)
    card = Image.new('RGB', (max(480, crop.width + 32), crop.height + 76), 'white')
    draw = ImageDraw.Draw(card)
    draw.text((16, 12), field.replace('_', ' ').upper() + ' - DATE VALUE ONLY', fill='black')
    card.paste(crop, ((card.width - crop.width)//2, 52))
    output = io.BytesIO()
    card.save(output, 'JPEG', quality=92)
    return base64.b64encode(output.getvalue()).decode('ascii')
