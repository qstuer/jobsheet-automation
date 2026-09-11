#!/usr/bin/env python3
"""只測 NVIDIA 圖片入口，不讀雲端工作單，也不連接 Asana/OneDrive。"""
import base64
import io
import sys

from PIL import Image, ImageDraw, ImageFont

from . import config, nvidia_client


EXPECTED_CODE = "739184"


def _make_test_image() -> str:
    """建立不含真實客戶資料的簡單測試圖片。"""
    image = Image.new("RGB", (720, 240), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=42)
    draw.text((40, 55), "JOBSHEET MODEL CHECK", fill="black", font=font)
    draw.text((40, 130), f"CODE: {EXPECTED_CODE}", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return base64.b64encode(buffer.getvalue()).decode()


def main() -> int:
    prompt = (
        "Read the six digits visibly printed after CODE in this synthetic test image. "
        "Return JSON only with exactly this shape: {\"code\":\"six visible digits\"}. "
        "Do not explain and do not use markdown."
    )
    raw = nvidia_client._call_vision(
        prompt,
        _make_test_image(),
        max_tokens=128,
        expects_json=True,
    )
    result = nvidia_client._parse_json_object(raw, required_keys={"code"})
    if result.get("code") != EXPECTED_CODE:
        raise RuntimeError("NVIDIA 圖片測試讀到錯誤代碼")
    print(f"NVIDIA 圖片測試成功：{config.NVIDIA_MODEL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
