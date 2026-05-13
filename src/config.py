"""集中管理所有設定常數"""
import os

# === API Keys (從環境變數讀，GitHub Actions 從 Secrets 注入) ===
NVIDIA_API_KEY      = os.environ.get("NVIDIA_API_KEY", "")
ASANA_TOKEN         = os.environ.get("ASANA_TOKEN", "")
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "462226775624951")

# === rclone 路徑 ===
GDRIVE_INPUT   = "googledrive:From_BrotherDevice"
GDRIVE_PENDING = "googledrive:From_BrotherDevice/_PENDING"
ONEDRIVE_OUTPUT = "onedrive:Hong Kong Sen's Healthcare/JOBSHEETS"

# === Google Drive Folder IDs (給 Apps Script 用) ===
GDRIVE_FROM_BROTHER_FOLDER_ID = "1ls3TQXyr0GTxDOO3MVQgR3QFDPXFM6gn"
GDRIVE_PENDING_FOLDER_ID      = "1Yby1chpl40PYv3Ph9xhnJd971Qwo32en"

# === K2.6 (NVIDIA NIM) ===
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
K26_MODEL       = "moonshotai/kimi-k2.6"

# === Asana ===
ASANA_BASE_URL = "https://app.asana.com/api/1.0"

# === OCR 設定 ===
OCR_ZOOM_DEFAULT  = 1.0   # 預設 1x zoom（約 425 tokens/張）
OCR_ZOOM_FALLBACK = 1.5   # 4 層全失敗後升級（約 1785 tokens/張）
OCR_CROP_TOP      = 0.10  # 從圖片高 10% 開始裁
OCR_CROP_BOTTOM   = 0.30  # 裁到 30%
OCR_CONTRAST      = 2.0   # 對比加強倍數

# === 業務邏輯 ===
ORDER_NO_REGEX    = r"^[56]\d{7}$"   # 8 位、5 或 6 開頭
CM_PAGES_PER_JOB  = 2                 # CM Job 共 2 頁，保留第 1 頁
PM_PAGES_PER_JOB  = 6                 # PM Job 共 6 頁，保留第 1,3,4,5 頁
PM_KEEP_OFFSETS   = [0, 2, 3, 4]     # 相對於 Job 起始頁的偏移
