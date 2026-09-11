"""集中管理所有設定常數"""
import os

# === API Keys (從環境變數讀，GitHub Actions 從 Secrets 注入) ===
NVIDIA_API_KEY      = os.environ.get("NVIDIA_API_KEY", "")
ASANA_TOKEN         = os.environ.get("ASANA_TOKEN", "")
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "462226775624951")

# === rclone 路徑 ===
GDRIVE_INPUT   = "googledrive:From_BrotherDevice"
# 階段A（splitter）切好的單一 job PDF 落地處；階段B（processor）從這裡讀
GDRIVE_SPLIT   = "googledrive:From_BrotherDevice/_SPLIT"
# 切割失敗（頁數驗算不過）整份原檔搬來這，等人工審查
GDRIVE_SPLIT_FAILED = "googledrive:From_BrotherDevice/_SPLIT_FAILED"
GDRIVE_PENDING = "googledrive:From_BrotherDevice/_PENDING"
ONEDRIVE_OUTPUT = "onedrive:Hong Kong Sen's Healthcare/JOBSHEETS"

# === Google Drive Folder IDs (給 Apps Script 用) ===
GDRIVE_FROM_BROTHER_FOLDER_ID = "1ls3TQXyr0GTxDOO3MVQgR3QFDPXFM6gn"
GDRIVE_PENDING_FOLDER_ID      = "1Yby1chpl40PYv3Ph9xhnJd971Qwo32en"

# === 視覺 OCR 模型 (NVIDIA NIM) ===
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Kimi K3 在 2026-09 的 NVIDIA 免費入口支援圖片及結構化輸出。
# 模型名稱不是密鑰；GitHub 可用 Actions variable NVIDIA_MODEL 暫時覆蓋。
NVIDIA_MODEL = (
    os.environ.get("NVIDIA_MODEL")
    or "moonshotai/kimi-k3"
)
NVIDIA_FALLBACK_MODEL = "meta/llama-3.2-11b-vision-instruct"
# 免費入口不可無限等待。OpenAI SDK 的內建重試關掉，由本程式明確控制，
# 讓一個失聯請求不會再拖住整批工作單數分鐘。
NVIDIA_REQUEST_TIMEOUT_SECONDS = 45.0

# Kimi K3 會先推理再給最後答案；過小的 max_tokens 可能只留下推理、沒有
# JSON/CM/PM 結果。簡單圈選和詳細欄位各保留足夠但有限的輸出空間。
KIMI_TEXT_MAX_TOKENS = 1024
KIMI_JSON_MAX_TOKENS = 4096
KIMI_STREAM_MAX_SECONDS = 90.0

# === Asana ===
ASANA_BASE_URL = "https://app.asana.com/api/1.0"

# === OCR 設定（訂單、設備、醫院、電話、資產編號、日期）===
OCR_ZOOM_DEFAULT  = 2.0   # 手寫細字至少 2x，避免低解像度下用猜的
OCR_ZOOM_FALLBACK = 2.5   # 舊版單次升級用（保留相容）
OCR_CROP_TOP      = 0.10  # 從圖片高 10% 開始裁
OCR_CROP_BOTTOM   = 0.56  # 裁到 56%，把聯絡電話、日期及 asset 一併納入
OCR_CONTRAST      = 2.0   # 對比加強倍數

# === OCR 多輪交叉核對 ===
# 同一個 Asana 工作至少要在兩個不同解像度都命中才接受；只命中一次便標成待核對。
OCR_RETRY_ZOOMS = [2.0, 2.5, 3.0]
OCR_MATCH_CONFIRMATIONS = 2

# === CM/PM 偵測專用裁切（JOB NATURE 欄）===
# 用戶實測座標：JOB NATURE 那一格在 縱向 10%-17%、橫向 70%-95%
CMPM_CROP_TOP    = 0.10
CMPM_CROP_BOTTOM = 0.17
CMPM_CROP_LEFT   = 0.70
CMPM_CROP_RIGHT  = 0.95
CMPM_ZOOM        = 3.0
CMPM_FALLBACK_TOP    = 0.08
CMPM_FALLBACK_BOTTOM = 0.20

# === 配對失敗的最終處置 ===
# 不可靠的名稱不得進 OneDrive；原檔留在 Google Drive _PENDING 等人工核對。
CHECK_PREFIX = "[待核對]"  # 只保留給舊檔名相容，不再用於新上傳

# === 業務邏輯 ===
ORDER_NO_REGEX    = r"^[56]\d{7}$"   # 8 位、5 或 6 開頭
CM_PAGES_PER_JOB  = 2                 # CM Job 共 2 頁，保留第 1 頁
PM_PAGES_PER_JOB  = 6                 # PM Job 共 6 頁，保留第 1,3,4,5 頁
PM_KEEP_OFFSETS   = [0, 2, 3, 4]     # 相對於 Job 起始頁的偏移

# 掃描器通常把正面、背面成對掃入。PM 標準為 6 張掃描頁，但現場可能只附
# 一部分 checklist；切頁時最多向前找 6 頁內的下一張工作單作邊界。
CONTENT_DARK_PIXEL_THRESHOLD = 200
CONTENT_MIN_DARK_RATIO = 0.03

# 工作單首頁的固定印刷版面會彼此相似，checklist 則明顯不同。先做本機版面
# 比對，通過後才讓視覺模型讀 CM/PM，可避免模型在 checklist 上猜到 PM。
JOBSHEET_LAYOUT_WIDTH = 96
JOBSHEET_LAYOUT_HEIGHT = 128
JOBSHEET_LAYOUT_DARK_THRESHOLD = 210
JOBSHEET_LAYOUT_MIN_DICE = 0.35
