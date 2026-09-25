"""集中管理所有設定常數"""
import os

# === API Keys (從環境變數讀，GitHub Actions 從 Secrets 注入) ===
NVIDIA_API_KEY      = os.environ.get("NVIDIA_API_KEY", "")
DEEPSEEK_API_KEY    = os.environ.get("DEEPSEEK_API_KEY", "")
ASANA_TOKEN         = os.environ.get("ASANA_TOKEN", "")
# 這個 workspace 編號已用目前連接的 Asana 帳戶實際核對。它不是密鑰；
# Actions variable 可在日後搬 workspace 時覆蓋，空值則必須回到已核對的預設值。
ASANA_WORKSPACE_GID = (
    os.environ.get("ASANA_WORKSPACE_GID")
    or "462226775624951"
)

# === rclone 路徑 ===
GDRIVE_INPUT   = "googledrive:From_BrotherDevice"
# 階段A（splitter）切好的單一 job PDF 落地處；階段B（processor）從這裡讀
GDRIVE_SPLIT   = "googledrive:From_BrotherDevice/_SPLIT"
# 切割失敗（頁數驗算不過）整份原檔搬來這，等人工審查
GDRIVE_SPLIT_FAILED = "googledrive:From_BrotherDevice/_SPLIT_FAILED"
GDRIVE_PENDING = "googledrive:From_BrotherDevice/_PENDING"
# 掃描器已漏頁，但工作單邊界仍可安全分辨。與 _SPLIT_FAILED 分開，避免把
# 「需要重掃」誤說成「切頁程式失敗」。
GDRIVE_INCOMPLETE = "googledrive:From_BrotherDevice/_INCOMPLETE"
GDRIVE_INCOMPLETE_RAW = "googledrive:From_BrotherDevice/_INCOMPLETE_RAW"
# 每個原始掃描一份 JSON 狀態；Apps Script 只會為需要人工處理的完成報告寄信。
GDRIVE_REPORTS = "googledrive:From_BrotherDevice/_REPORTS"
# Asana 設備索引只放在 Google Drive 私有控制資料夾；不放 Git，也不放 OneDrive。
GDRIVE_CONTROL = "googledrive:From_BrotherDevice/.jobsheet-control"
GDRIVE_ASANA_INDEX_JSON = f"{GDRIVE_CONTROL}/asana-device-index.json"
GDRIVE_ASANA_INDEX_CSV = f"{GDRIVE_CONTROL}/asana-device-index.csv"
GDRIVE_ASANA_LOCATION_INDEX_CSV = f"{GDRIVE_CONTROL}/asana-location-index.csv"
GDRIVE_ASANA_INDEX_MANIFEST = f"{GDRIVE_CONTROL}/asana-device-index-manifest.json"
ONEDRIVE_OUTPUT = "onedrive:Hong Kong Sen's Healthcare/JOBSHEETS"

# === Google Drive Folder IDs (給 Apps Script 用) ===
GDRIVE_FROM_BROTHER_FOLDER_ID = "1ls3TQXyr0GTxDOO3MVQgR3QFDPXFM6gn"
GDRIVE_PENDING_FOLDER_ID      = "1Yby1chpl40PYv3Ph9xhnJd971Qwo32en"

# === 視覺 OCR 供應商 ===
# 正式流程在未指定時仍使用已驗證的 NVIDIA，避免加入 DeepSeek 測試能力時
# 意外改動自動上傳。Safe Dry Run 可明確設為 deepseek 做隔離比較。
OCR_PROVIDER = (os.environ.get("OCR_PROVIDER") or "nvidia").strip().lower()

# DeepSeek 官方付費 API。2026-09 的 DeepSeek-V4.1-Flash API 名稱為
# deepseek-flash，原生支援圖片；模型名稱可用非敏感環境變數覆蓋。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash"
DEEPSEEK_REQUEST_TIMEOUT_SECONDS = 60.0
DEEPSEEK_JSON_MAX_TOKENS = 1024

# === NVIDIA NIM 視覺 OCR ===
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Nemotron 3 Nano Omni 是 NVIDIA 的圖片/OCR 模型，支援 JSON 輸出。
# 模型名稱不是密鑰；GitHub 可用 Actions variable NVIDIA_MODEL 暫時覆蓋。
NVIDIA_MODEL = (
    os.environ.get("NVIDIA_MODEL")
    or "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
)
NVIDIA_FALLBACK_MODEL = "meta/llama-3.2-11b-vision-instruct"
# 免費入口不可無限等待。OpenAI SDK 的內建重試關掉，由本程式明確控制，
# 讓一個失聯請求不會再拖住整批工作單數分鐘。
NVIDIA_REQUEST_TIMEOUT_SECONDS = 45.0

# 官方建議純指令工作用 1024 輸出 token、top_k=1 並關閉 thinking。
# Jobsheet 只要忠實抄錄，不需要模型展示推理過程。
NEMOTRON_INSTRUCT_MAX_TOKENS = 1024

# 後備視覺模型有時會先加一小段說明；300 token 可能在完整 JSON 結束前
# 截斷，造成「HTTP 200 但沒有完整 JSON」。所有 JSON OCR 至少保留這個
# 輸出空間，最後仍由本機 parser 嚴格驗證，不會接受散文猜測。
NVIDIA_JSON_MAX_TOKENS = 1024

# Kimi K3 會先推理再給最後答案；過小的 max_tokens 可能只留下推理、沒有
# JSON/CM/PM 結果。保留相容設定，供 Actions variable 臨時改回 Kimi 時使用。
KIMI_TEXT_MAX_TOKENS = 1024
KIMI_JSON_MAX_TOKENS = 4096
KIMI_STREAM_MAX_SECONDS = 90.0

# === Asana ===
ASANA_BASE_URL = "https://app.asana.com/api/1.0"
ASANA_MAX_HYDRATED_CANDIDATES = 40
# 私人設備表的三層安全閘。百分比使用 0–1；日期只在已鎖定設備後選
# 歷史工作，不參與設備排名。
INDEX_PRODUCT_MIN_SIMILARITY = 0.33
INDEX_HOSPITAL_MIN_SIMILARITY = 0.33
INDEX_SERIAL_MIN_SIMILARITY = 0.50
INDEX_SERIAL_CLOSE_GAP = 0.10
INDEX_VISION_CANDIDATE_LIMIT = 10
INDEX_TASK_DATE_MAX_DAYS = 31
# Asset 只加輔助分；編輯相似率以整數百分比四捨五入後比較。
ASSET_MIN_SIMILARITY_PERCENT = 70

# === OCR 設定（訂單、設備、醫院、電話、資產編號、日期）===
OCR_ZOOM_DEFAULT  = 3.0   # 第一張分格欄位卡；不再把半頁表格原樣交給模型
OCR_ZOOM_FALLBACK = 4.0   # 身分欄位（訂單/型號/serial/醫院）第二次高倍複核
OCR_CROP_TOP      = 0.10  # 舊版上半頁裁切，保留相容但正式欄位 OCR 不再使用
OCR_CROP_BOTTOM   = 0.56
OCR_CONTRAST      = 2.0

# Philips Jobsheet 的印刷版面固定。座標是 (left, top, right, bottom)，以整頁
# 寬高的比例表示；每格包含印刷標籤和手寫值，四周留少量空間以容忍掃描偏移。
# 這些範圍已用 2026-09 留下的多批實際掃描首頁逐張核對。
OCR_FIELD_BOXES = {
    "order_no":        (0.220, 0.108, 0.405, 0.162),
    "product_raw":     (0.405, 0.108, 0.570, 0.162),
    "serial_candidates": (0.570, 0.108, 0.770, 0.162),
    "hospital_raw":    (0.055, 0.164, 0.575, 0.207),
    "contact_person_raw": (0.555, 0.164, 0.955, 0.207),
    "department_room_raw": (0.055, 0.198, 0.575, 0.242),
    "phone_candidates": (0.555, 0.198, 0.955, 0.242),
    "service_date_raw": (0.555, 0.312, 0.735, 0.365),
    "fault_symptom":   (0.055, 0.245, 0.955, 0.315),
    "action_taken":    (0.055, 0.307, 0.555, 0.500),
    # Dates repeated beside the two signatures may corroborate an ambiguous
    # ACTION DATE. They are never treated as a replacement date by themselves.
    "engineer_signed_date": (0.215, 0.930, 0.500, 0.980),
    "customer_signed_date": (0.700, 0.930, 0.960, 0.980),
}
# 單格複核只保留手寫值附近，避免印刷標籤與表格線佔去大部分像素。
# 外層欄位卡仍會顯示可靠的欄位名稱，因此模型毋須靠原表格標籤猜欄位。
OCR_FOCUSED_FIELD_BOXES = {
    "order_no":          (0.220, 0.122, 0.405, 0.162),
    "product_raw":       (0.405, 0.122, 0.570, 0.162),
    "serial_candidates": (0.570, 0.120, 0.770, 0.166),
    "hospital_raw":      (0.180, 0.166, 0.575, 0.207),
    "contact_person_raw": (0.670, 0.166, 0.955, 0.207),
    "department_room_raw": (0.180, 0.198, 0.575, 0.242),
    "phone_candidates":  (0.670, 0.198, 0.955, 0.242),
    # Handwritten dates can rise into the printed DATE heading. Starting at
    # 0.323 clipped their upper loops; B08's ambiguous month must be retested.
    "service_date_raw":   (0.555, 0.312, 0.735, 0.365),
}
OCR_PRIMARY_CARD_FIELDS = (
    "order_no", "product_raw", "serial_candidates", "hospital_raw",
    "contact_person_raw", "department_room_raw", "phone_candidates", "service_date_raw",
    "fault_symptom", "action_taken",
)
OCR_IDENTITY_CARD_FIELDS = (
    "order_no", "product_raw", "serial_candidates", "hospital_raw",
)
OCR_SUPPORT_CARD_FIELDS = (
    "contact_person_raw", "department_room_raw", "phone_candidates", "service_date_raw",
    "fault_symptom", "action_taken",
)
OCR_CARD_WIDTH = 1200
OCR_FIELD_LABEL_HEIGHT = 42
OCR_IDENTITY_ZOOM = 4.0
OCR_SUPPORT_ZOOM = 4.0
OCR_FOCUSED_RETRY_ZOOMS = [5.0, 6.0]
OCR_SERVICE_DATE_MAX_AGE_DAYS = 93
OCR_SERVICE_DATE_FUTURE_TOLERANCE_DAYS = 7

# 未知的 2-6 字母短碼很容易是手寫幻覺，只有已由實際工作單核對的短碼可作
# 醫院證據。PYN 與 PYNEH 是同院；詳細樓層仍保留在 Dept./Room 欄。
HOSPITAL_SHORT_ALIASES = {
    "QMH": "QMH", "QEH": "QEH", "KWH": "KWH", "KH": "KH",
    "PYN": "PYNEH", "PYNEH": "PYNEH", "PMH": "PMH",
    "HKCH": "HKCH", "PWH": "PWH", "UCH": "UCH", "TMH": "TMH",
    "NDH": "NDH", "GH": "GH",
}

# === OCR 多輪交叉核對 ===
# 第一輪讀完整分格卡，第二輪只複核身分欄；仍有爭議才精讀單格。
# 舊名稱保留給測試及外部呼叫相容。
OCR_RETRY_ZOOMS = [OCR_ZOOM_DEFAULT, OCR_IDENTITY_ZOOM]
OCR_MATCH_CONFIRMATIONS = 2
# 整張上半頁多輪仍配不到時，只重讀 SERIAL NO. 小格。裁小後可用更高
# 解像度而不增加太多圖片大小，減少模型被其他手寫欄位干擾。
OCR_SERIAL_CROP_LEFT = 0.55
OCR_SERIAL_CROP_RIGHT = 0.77
OCR_SERIAL_CROP_TOP = 0.095
OCR_SERIAL_CROP_BOTTOM = 0.18
OCR_SERIAL_RETRY_ZOOMS = [4.0, 5.0, 6.0]
# 圖片服務連續幾輪工作仍失敗後，停止無限重跑並轉 _PENDING。
OCR_MAX_BATCH_ATTEMPTS = 3

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

# PM 成品必須是工作單 + checklist 1/2/3，共四張有內容頁。頁面數正確但
# checklist 互相近乎相同時亦視為掃描可疑（常見於雙面送紙重複）。
CM_EXPECTED_CONTENT_PAGES = 1
PM_EXPECTED_CONTENT_PAGES = 4
DUPLICATE_PAGE_MAX_HASH_RATIO = 0.015

# PDF 重新編碼會令 SHA-256 不同；用低解像度 dHash 比較實際頁面內容。
PDF_VISUAL_HASH_SIZE = 16
PDF_VISUAL_MAX_HASH_RATIO = 0.025

# 工作單首頁的固定印刷版面會彼此相似，checklist 則明顯不同。先做本機版面
# 比對，通過後才讓視覺模型讀 CM/PM，可避免模型在 checklist 上猜到 PM。
JOBSHEET_LAYOUT_WIDTH = 96
JOBSHEET_LAYOUT_HEIGHT = 128
JOBSHEET_LAYOUT_DARK_THRESHOLD = 210
JOBSHEET_LAYOUT_MIN_DICE = 0.35

# 單檔安全測試。dry-run 必須同時指定一個檔案，且不移動／刪除雲端檔案、
# 不寫 OneDrive、也不更新批次狀態。
JOBSHEET_DRY_RUN_ENV = "JOBSHEET_DRY_RUN"
JOBSHEET_SOURCE_QUEUE_ENV = "JOBSHEET_SOURCE_QUEUE"
