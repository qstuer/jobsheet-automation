# Jobsheet 自動化歸檔 — 系統手冊

> 最後全面檢查：2026-09-12
> GitHub：<https://github.com/qstuer/jobsheet-automation>
> 已實證：2026-05-31，一份 20 頁掃描（1 份 CM、3 份 PM）全自動切成 4 份並正確歸檔。
> 本次翻新：修正新版 rclone 看不到 PDF、移除假成功、加入冷卻及連敗告警、替換已停用的辨認入口、補上安全重跑及測試。
> 2026-09-10 實檔複驗：切頁結果逐頁正確；同時發現舊 OCR 提示會把範例值誤當答案，已移除所有真實風格範例，並改為兩次獨立辨認一致才可自動配對。
> 2026-09-11 翻新：用 52 頁實檔找出「短 PM」會令固定 6 頁規則錯位；改為尋找下一張工作單作邊界、按頁面內容去除背頁。辨認新增日期/電話/asset 交叉核對，不確定的名稱不再送 OneDrive。
> 2026-09-12 模型更新：圖片辨認首選 `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`，使用官方不思考 OCR 設定及嚴格 JSON 驗證；任何首選模型失聯時，同一批只等待一次，隨後使用已知可工作的 Llama 後備。

這是本專案唯一主要說明。人或 AI 接手時，先讀完本檔；不要從舊聊天猜目前架構。

## 1. 這套系統做甚麼

使用者把一疊 Jobsheet 放進 Brother 掃描器後，系統自動把 PDF 拆成每份工作單，讀取內容，在 Asana 找到相符工作，再用正確名稱存入 OneDrive。

```text
Brother 掃描器
  ↓
Google Drive / From_BrotherDevice
  ↓  Apps Script 每 5 分鐘查看一次
GitHub Stage A（切頁）
  ↓
Google Drive / From_BrotherDevice / _SPLIT
  ↓  Stage A 成功後自動接力
GitHub Stage B（辨認、Asana 配對、上傳）
  ↓
OneDrive / Hong Kong Sen's Healthcare / JOBSHEETS
```

旁邊有兩層告警：GitHub 內連續失敗 3 次會建立 Issue；Google 端的 Apps Script 也可寄電郵，補足 GitHub 工作本身無法啟動的情況。

GitHub 沒有定時空跑。平時由 Apps Script 發現 PDF 後通知 GitHub；Stage A、Stage B 也保留手動啟動按鈕。

## 2. 四個位置就是四種狀態

| 位置 | 代表甚麼 | 下一步 |
|---|---|---|
| `googledrive:From_BrotherDevice/` | 原始掃描 PDF | Stage A 切頁 |
| `googledrive:From_BrotherDevice/_SPLIT/` | 已切成單一工作的 PDF | Stage B 辨認及上傳 |
| `googledrive:From_BrotherDevice/_SPLIT_FAILED/` | 找不到可信工作單邊界 | 人工檢查原始整批掃描 |
| `googledrive:From_BrotherDevice/_PENDING/` | 已切好，但名稱未能可靠核對 | 人工核對；不會進 OneDrive |

最終位置：`onedrive:Hong Kong Sen's Healthcare/JOBSHEETS/`

切頁後檔名：`{原檔名}__job{次序}_{CM或PM}.pdf`，例如 `20260531112532_001__job2_PM.pdf`。

## 3. Stage A：只負責安全切頁

入口：`python -m src.splitter`
GitHub 工作：`.github/workflows/jobsheet-split.yml`

流程：

1. 只看 `From_BrotherDevice/` 最外層 PDF，不掃子資料夾。
2. 下載一份原始 PDF。
3. 讀每份工作的第一頁 `JOB NATURE` 圈選，判斷 CM 或 PM。
4. 先用固定印刷版面確認候選頁真的是工作單首頁，再讀 CM/PM；checklist 不會再因出現類似字樣而冒充新工作。
5. 先看標準長度位置；若不是下一張工作單，按雙面掃描的 2 頁步幅向前找真正邊界。
6. 在每段內保留工作單正面及所有有內容的 checklist，去掉空白背頁。
7. 上傳每份切頁 PDF 到 `_SPLIT/`。
8. 全部上傳成功後才刪原始 PDF。

頁數規則：

```text
CM：通常 2 張掃描頁，保留工作單正面
PM：通常 6 張掃描頁，標準結果為工作單 + 3 頁 checklist
短 PM：現場若少附 checklist，可為 2 或 4 張掃描頁；保留其中所有真正有內容的頁
```

`JOB NATURE` 是手畫圈選，四個選項是 `CM | PM | FCO | INS`。自動流程只接受一個明確的 CM 或 PM。

### 甚麼才會進 `_SPLIT_FAILED`

只限單據本身不能安全切頁：起始圈選讀不清、讀到 FCO/INS、6 頁範圍內找不到可信的下一張工作單邊界，或最後頁數不能按雙面掃描完整收尾。程式中這類情況叫 `SplitError`。

以下不是單據問題，絕不能搬去 `_SPLIT_FAILED`：GitHub 工作未開始、NVIDIA/Google Drive/網路故障、rclone 登入過期、PDF 讀寫失敗或程式意外。這些情況會讓 Stage A 顯示失敗，原始 PDF 留在入口，修好後可安全重跑。

## 4. Stage B：辨認、配對、上傳

入口：`python -m src.processor`
GitHub 工作：`.github/workflows/jobsheet-process.yml`

手動測試可填 `jobsheet_file`，精確指定 `_SPLIT` 內一份 PDF。此模式只處理
該檔，也不會順帶重試 `_PENDING`；留空或 Stage A 自動接力才維持正常批次。

流程：

1. 讀 `_SPLIT/` 的單一工作 PDF。
2. 忠實抄錄訂單號、機身編號候選、型號、醫院/位置、電話、asset、HAWO/WO 及服務日期。
3. 在 Asana 找候選工作，再用上述欄位交叉核對。
4. 上傳 OneDrive，成功後才刪 `_SPLIT/` 來源。

辨認會用 2.0、2.5、3.0 倍清晰度交叉核對。同一個 Asana 工作至少要在兩個清晰度都命中才接受；只命中一次、兩次命中不同工作，或證據不足，一律移到 Google Drive `_PENDING`，不會自動歸錯設備。

視覺模型只負責照字抄錄，提示詞明確禁止猜醫院、補全機身編號或沿用先前圖片。Actions 日誌只記「哪些欄位看得到」，不印電話、asset、serial 或客戶內容。

命名次序：

1. Asana 名稱有 8 位訂單號：`SR#訂單號.pdf`。
2. 找到 Asana 工作但沒有訂單號：使用完整 Asana 工作名稱。
3. 無法可靠配對：不碰 OneDrive，完整檔留在 `_PENDING`。

舊版建立的 `[待核對]` 名稱只為歷史相容，新流程不再新增。Asana 連不上、權限失效或回覆錯誤屬系統故障：來源保留在 `_SPLIT`，Stage B 顯示失敗，不會偽裝成配對不到。

### 重跑不製造重複檔

若 OneDrive 已上傳，但刪除 Google Drive 暫存檔時中斷，重跑會比較檔案大小和完整內容。相同便沿用原檔，不建立 `(1)`；內容不同才用 `(1)`、`(2)` 避免覆蓋另一份單據。

## 5. Asana 配對規則

Asana 只找出一小批候選，最後核對在本機完成：

1. 用醫院、產品、機身編號、訂單號及 HAWO/WO 分別找候選；電話與 asset 在候選的 Asana 描述中核對。
2. 校正常見型號小錯字，例如 `EPLQ 5G` 可校正為 `EPIQ 5G`。
3. 機身編號完全相同仍須日期、電話、asset、HAWO/WO 或「醫院+型號」支持，避免挑到同一設備的舊工作。
4. 機身編號只可容許 1 個字的辨認差異；此時至少還要兩組證據支持。
5. 完成/未完成都可以是正確工作，不能再用「未完成優先」挑選；以單據最近三個月的服務日期為主。
6. 候選並列、證據不足，或另一個清晰度沒有再次命中同一工作，一律留在 `_PENDING`。

已知型號：`Affiniti 30/50/70`、`EPIQ 5G/7G/Elite`、`CX30/CX50`。

Asana `typeahead` 可跨 project，不再把每月 project 編號寫死。它不是完整清單，所以程式會用幾個可靠欄位分別搜尋後合併結果。

## 6. Apps Script：發現檔案及防重複

檔案：`apps_script/trigger.gs`

Apps Script 每 5 分鐘看一次 Google Drive。發現 PDF 後等 60 秒，避免掃描器仍在上傳，再通知 GitHub 啟動 Stage A。

- 一次只允許一個檢查程序，避免定時與手動執行撞在一起。
- GitHub 確認收到通知後，20 分鐘內不重複通知。
- GitHub 沒收到通知便不開始冷卻，5 分鐘後可再試。
- 冷卻期間的新 PDF 留在原資料夾，冷卻後一起處理，不會遺失。
- 入口清空後清除冷卻，下一批可立即開始。

Apps Script 的 Project Settings → Script properties：

| 名稱 | 必要 | 用途 |
|---|---|---|
| `GITHUB_PAT` | 是 | 通知 GitHub及讀取結果；只授權本 repo，`Contents: Read and write`、`Actions: Read-only` |
| `ALERT_EMAIL` | 建議 | 收 Google 端故障電郵；填自己的地址 |

加入 `ALERT_EMAIL` 後，手動執行一次 `checkDriveAndTrigger()` 批准寄信權限。地址不是密鑰，但不要寫死在公開程式碼。

## 7. 告警

`.github/workflows/jobsheet-failure-alert.yml` 分別監看 Stage A、Stage B。失敗、超時、啟動失敗或需要人工批准都計入；連續 3 次便建立 `[Pipeline Alert] ...` Issue，繼續失敗只靜默更新同一個 Issue，不再每次留言洗 Gmail；下一次成功才留言並自動關閉。它只用 GitHub 內建權限，不需新增密鑰。

如果 GitHub 連告警工作都無法啟動，Issue 當下也不會建立。因此 Apps Script 會在設定 `ALERT_EMAIL` 後做第二層檢查：某階段連敗 3 次寄一次；恢復後重設。若連續 3 次連 GitHub 狀態也讀不到，會寄另一封通知。寄信失敗不阻止正常處理。

## 8. 設定及過時項目

GitHub Secrets：

| 名稱 | 必要 | 說明 |
|---|---|---|
| `RCLONE_CONFIG` | 是 | Google Drive / OneDrive 的 rclone 登入設定（base64） |
| `NVIDIA_API_KEY` | 是 | 視覺辨認服務 API key |
| `ASANA_TOKEN` | 是 | Asana 存取權杖 |
| `NVIDIA_MODEL`（Actions variable） | 否 | 臨時換辨認模型；不填使用程式預設 Nemotron 3 Nano Omni |
| `ASANA_WORKSPACE_GID`（Actions variable） | 否 | 日後搬 Asana workspace 才覆蓋；不填使用已核對的目前 workspace |

不要把值寫進 repo、日誌或文件。

2026-09-12 官方狀態檢查：

- 舊模型 `nvidia/llama-3.1-nemotron-nano-vl-8b-v1` 的免費入口已停用。
- 預設使用有免費入口、支援圖片、OCR 及 JSON 輸出的 `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`。
- Jobsheet 是抄錄工作，不需要長篇推理；程式依官方 instruct 設定關閉 thinking，使用 `top_k=1`、1024 輸出上限及較穩定的低溫度。
- 2026-09-12 用新舊 key 實際測試 Kimi K3，均在回傳任何資料前超時；這只證明 Kimi 免費入口當時不可用，不代表 key 本身無效。
- 2026-09-12 GitHub `NVIDIA Model Check` run 34628392304：新 key 呼叫 Nemotron 成功，正確讀出假圖片的六位數字並通過 JSON 驗證；測試沒有讀取 Google Drive、Asana 或 OneDrive。
- 2026-09-12 GitHub Stage B run 34669655930：以單檔安全模式處理一份 4 頁 PM 實單，兩個清晰度命中同一個 Asana 工作；Nemotron 遇到一次 503 後短重試成功，OneDrive 成品與 Google Drive 來源的 SHA-256 完全相同，另外 10 份佇列檔沒有被處理。
- 後備為曾成功取得 HTTP 200 的 `meta/llama-3.2-11b-vision-instruct`：Nemotron 遇到暫時性 503/timeout 會短重試一次才用後備；曾長時間掛起的 Kimi 維持一次即後備。
- 每次 NVIDIA 網路等待最多 45 秒，SDK 不做隱藏重試；全部模型失敗時檔案保留在 `_SPLIT`。
- 欄位辨認要求 NVIDIA 只回指定 JSON：order、serial 候選、產品、醫院/位置、電話候選、asset 候選、HAWO/WO、ACTION DATE 及讀不清欄位；若服務在 JSON 外加短說明或 markdown 外框也能安全讀取，但不會從散文硬猜。
- 提示文字不可放入看似真實的機身編號、型號或醫院範例；模型在字跡難讀時可能直接複製範例，造成錯配。
- 可用 GitHub Actions variable `NVIDIA_MODEL` 暫時覆蓋，不需先改程式；模型名稱不是密鑰，不放 Secrets。
- rclone 固定 1.75.0，不再每次下載未知的新版本。
- GitHub 內的下載版本變數必須叫 `JOBSHEET_RCLONE_RELEASE`；不可改成 `RCLONE_VERSION`，否則 rclone 會誤當成自己的開關而啟動失敗。
- Python 套件固定在 `requirements.txt`，避免數月後自動升級而失效。
- 舊版 `src/orchestrator.py` 會過早刪原檔，已移除；不要從歷史版本恢復使用。

## 9. 壞了時最短處理方法

先看檔案卡在哪，不要先搬或刪：

```powershell
cd "C:\Users\Chowc\Downloads\Jobsheet 自動化歸檔 AI 代理系統\jobsheet-automation"
python -m src.healthcheck
```

- `等待切割` 有檔：手動啟動 `Jobsheet Stage A - Split`。
- `等待辨認及上傳` 有檔：手動啟動 `Jobsheet Stage B - Process`。
- `_SPLIT_FAILED` 有檔：人工看頁數和 CM/PM 圈選。
- `_PENDING` 有檔：切頁已完成，但 Asana 名稱證據不足；先人工核對，不要搬進 OneDrive 猜名。
- GitHub 很快顯示成功但入口完全沒動：看 `Show queue before processing` 是否真的列出檔案；若是 0，檢查 rclone 列檔。
- GitHub 3 至 4 秒便結束且 Python 未開始：這是 GitHub 工作層問題，不是 PDF 問題；原檔不會進 `_SPLIT_FAILED`。
- Google Drive 回 `403 rateLimitExceeded`：先停止手動連續查詢並稍後再試，不要反覆啟動流程。若日常運行也經常出現，才建立自己的 Google OAuth client，更新本機 rclone 及 GitHub `RCLONE_CONFIG`；`client_secret` 只可由使用者放進設定，不能寫入 repo。

修好後可安全重跑。不要手動刪入口原檔。

## 10. 測試

本機、不移動雲端檔案：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python -m compileall -q src tests
```

雲端只讀檢查：`python -m src.healthcheck`

歷史 PDF 辨認抽查：將測試 PDF 放入 `tests/sample_pdfs/`，設定本機 `NVIDIA_API_KEY`，執行 `python -m tests.test_ocr_local`。

只測 NVIDIA 模型能否看圖及回傳合格 JSON：在 GitHub Actions 手動執行 `NVIDIA Model Check`。它只讀一張程式即時產生的假資料圖片，不會讀 Google Drive、Asana 或 OneDrive。

## 11. 程式地圖

| 檔案 | 責任 |
|---|---|
| `src/splitter.py` | Stage A：切頁、上傳 `_SPLIT`、成功後刪原檔 |
| `src/pdf_utils.py` | 工作單邊界、頁面內容判斷、驗算及抽頁 |
| `src/processor.py` | Stage B：辨認、Asana 配對、OneDrive 上傳 |
| `src/nvidia_client.py` | 圖片裁切及 NVIDIA 呼叫 |
| `src/asana_client.py` | Asana 候選搜尋及 serial/日期/電話/asset 多欄核對 |
| `src/rclone_helper.py` | 雲端列檔、下載、上傳、比較、刪除 |
| `src/healthcheck.py` | 只讀列出四個位置現況 |
| `src/config.py` | 路徑、模型、裁切範圍及業務常數 |
| `apps_script/trigger.gs` | 發現新 PDF、防重複、電郵告警 |
| `.github/workflows/jobsheet-split.yml` | Stage A |
| `.github/workflows/jobsheet-process.yml` | Stage B |
| `.github/workflows/jobsheet-failure-alert.yml` | 連敗 Issue 告警 |
| `.github/workflows/quality-check.yml` | 每次改程式自動測試 |
| `.github/workflows/nvidia-model-check.yml` | 手動、無客戶資料的 NVIDIA 圖片連線測試 |

## 12. 接手時不可破壞的原則

1. 不碰 GitHub 帳單、付款或訂閱。
2. 不把外部服務故障包成 `SplitError`。
3. 不在全部切頁上傳成功前刪原始 PDF。
4. 不在 OneDrive 上傳成功前刪 `_SPLIT` 來源。
5. 不把 Asana 故障當成找不到工作。
6. 名稱未可靠確認時不碰 OneDrive，留在 `_PENDING`。
7. 單檔傳送用 `rclone copyto`，不要用 `copy` 掃描有 18,000 多檔的資料夾。
8. 根目錄列檔用 `--max-depth 1`，不要混用 `--include` 和 `--exclude`。
9. 不確定業務規則時先問；可先做不改資料的檢查。
10. 改完先展示 diff；獲准後可 commit。除非使用者明確授權，否則不要自行 push。

## 13. 官方參考

- 現用 NVIDIA 模型：<https://build.nvidia.com/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning>
- 舊 NVIDIA 模型：<https://build.nvidia.com/nvidia/llama-3.1-nemotron-nano-vl-8b-v1>
- Asana 搜尋：<https://developers.asana.com/reference/typeaheadforworkspace>
- Asana 限流：<https://developers.asana.com/docs/rate-limits>
- rclone 列檔：<https://rclone.org/commands/rclone_lsf/>
- rclone 單檔刪除：<https://rclone.org/commands/rclone_deletefile/>
- Apps Script 防同時執行：<https://developers.google.com/apps-script/reference/lock>
- Apps Script 寄信：<https://developers.google.com/apps-script/reference/mail/mail-app>
