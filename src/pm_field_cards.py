"""Read-only PM header layout experiment; no reference answers or network calls."""
import base64
import io
import math
import os

import fitz
from PIL import Image, ImageDraw

from . import nvidia_client as vision

ENABLED_ENV = "JOBSHEET_PM_FIELD_CARDS"
# Philips checklist page 1 only. Keep handwriting above each printed underline;
# never include the inspection table or last-page calibration-tool serials.
FIELDS = {
    "hospital": ("CHECKLIST CUSTOMER / HOSPITAL", (0.165, 0.137, 0.600, 0.174)),
    "date": ("CHECKLIST DATE", (0.665, 0.137, 0.925, 0.174)),
    "product": ("CHECKLIST SYSTEM / PRODUCT", (0.155, 0.166, 0.355, 0.205)),
    "serial": ("CHECKLIST sn / MACHINE SERIAL", (0.395, 0.166, 0.602, 0.208)),
    # A correction may be written immediately below the asset underline.
    "asset": ("CHECKLIST ASSET NO.", (0.700, 0.173, 0.925, 0.235)),
}


def require_read_only():
    if os.environ.get(ENABLED_ENV) != "1" or os.environ.get("JOBSHEET_DRY_RUN") != "1":
        raise ValueError("PM field cards require explicit read-only mode")


def render(doc, zoom):
    require_read_only()
    if len(doc) != 4 or not math.isfinite(zoom) or zoom <= 0:
        raise ValueError("PM field cards require a complete four-page document")
    zoom = min(zoom, 6.0)
    # Near-square cards preserve character pixels when a service constrains
    # the image's longest side; the old five-field strip was over 9:1 wide.
    width, panel_w, panel_h, gap = 1600, 772, 230, 16
    card = Image.new("RGB", (width, 3 * panel_h + 4 * gap), "white")
    draw = ImageDraw.Draw(card)
    page, rect = doc[1], doc[1].rect
    for number, (label, box) in enumerate(FIELDS.values()):
        row, column = divmod(number, 2)
        x, y = gap + column * (panel_w + gap), gap + row * (panel_h + gap)
        draw.rectangle((x, y, x + panel_w, y + panel_h), outline="black", width=3)
        draw.text((x + 12, y + 10), label, fill="black")
        l, t, r, b = box
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(
            rect.x0 + l * rect.width, rect.y0 + t * rect.height,
            rect.x0 + r * rect.width, rect.y0 + b * rect.height))
        crop = vision._preprocess_field_image(
            Image.open(io.BytesIO(pix.tobytes("png"))), strong=zoom >= 6)
        scale = min((panel_w - 24) / crop.width, (panel_h - 48) / crop.height)
        crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
                           Image.Resampling.LANCZOS)
        card.paste(crop, (x + (panel_w - crop.width) // 2, y + 34 + (panel_h - 40 - crop.height) // 2))
    output = io.BytesIO()
    card.save(output, "JPEG", quality=92)
    return base64.b64encode(output.getvalue()).decode("ascii")
