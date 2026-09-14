"""以實際頁面影像辨認同一份 PDF，不受重新編碼或 metadata 影響。"""
from pathlib import Path
from typing import List

import fitz
from PIL import Image, ImageOps

from . import config


def page_dhash(page: fitz.Page, size: int = None) -> int:
    """產生忽略細微掃描／壓縮差異的 difference hash。"""
    size = size or config.PDF_VISUAL_HASH_SIZE
    pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), colorspace=fitz.csGRAY)
    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    image = ImageOps.autocontrast(image).resize((size + 1, size), Image.Resampling.LANCZOS)
    value = 0
    pixels = list(image.get_flattened_data())
    stride = size + 1
    for row in range(size):
        offset = row * stride
        for col in range(size):
            value = (value << 1) | int(pixels[offset + col] > pixels[offset + col + 1])
    return value


def pdf_visual_signature(path: Path) -> List[int]:
    doc = fitz.open(path)
    try:
        return [page_dhash(page) for page in doc]
    finally:
        doc.close()


def hash_distance_ratio(left: int, right: int, size: int = None) -> float:
    size = size or config.PDF_VISUAL_HASH_SIZE
    return (left ^ right).bit_count() / float(size * size)


def visually_same_pdf(left: Path, right: Path) -> bool:
    """頁數及每頁視覺 hash 都相近才視為同一份掃描內容。"""
    left_sig = pdf_visual_signature(left)
    right_sig = pdf_visual_signature(right)
    if len(left_sig) != len(right_sig):
        return False
    return all(
        hash_distance_ratio(a, b) <= config.PDF_VISUAL_MAX_HASH_RATIO
        for a, b in zip(left_sig, right_sig)
    )
