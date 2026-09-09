"""
本機 NVIDIA 視覺辨認準確度對照測試

用法：
  1. 把 5-10 張歷史 jobsheet PDF 放到 tests/sample_pdfs/
  2. 設環境變數 NVIDIA_API_KEY
  3. 跑 python -m tests.test_ocr_local
  4. 人工檢查輸出 JSON 的準確度

常見誤讀（對照 CLAUDE.md Section 5）：
  9→G, O→0, 0→D, l→1, S→5, C450→CX50
"""
import json
import os
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).parent.parent))
from src import nvidia_client


def main():
    sample_dir = Path(__file__).parent / "sample_pdfs"
    if not sample_dir.exists():
        print(f"❌ {sample_dir} 不存在，請放 PDF 進去")
        return 1

    pdfs = sorted(sample_dir.glob("*.pdf"))
    print(f"找到 {len(pdfs)} 張 PDF\n")

    for pdf_path in pdfs:
        print(f"=== {pdf_path.name} ===")
        doc = fitz.open(pdf_path)
        try:
            cm_pm = nvidia_client.detect_cm_pm(doc, 0)
            ocr_1x = nvidia_client.ocr_jobsheet_fields(doc, 0, zoom=1.0)
            print(f"  類型: {cm_pm}")
            print(f"  OCR 1x: {json.dumps(ocr_1x, ensure_ascii=False, indent=4)}")

            # 如果 order_no 為 None，也試 1.5x 看看差異
            if not ocr_1x.get("order_no"):
                ocr_15x = nvidia_client.ocr_jobsheet_fields(doc, 0, zoom=1.5)
                print(f"  OCR 1.5x: {json.dumps(ocr_15x, ensure_ascii=False, indent=4)}")
        except Exception as e:
            print(f"  ❌ 錯誤: {e}")
        finally:
            doc.close()
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
