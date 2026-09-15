# Jobsheet 自動化歸檔 — 系統手冊

> 最後全面檢查：2026-09-15
> GitHub：<https://github.com/qstuer/jobsheet-automation>
> 已實證：2026-05-31，一份 20 頁掃描（1 份 CM、3 份 PM）全自動切成 4 份並正確歸檔。
> 本次翻新：修正新版 rclone 看不到 PDF、移除假成功、加入冷卻及連敗告警、替換已停用的辨認入口、補上安全重跑及測試。
> 2026-09-10 實檔複驗：切頁結果逐頁正確；同時發現舊 OCR 提示會把範例值誤當答案，已移除所有真實風格範例，並改為兩次獨立辨認一致才可自動配對。
> 2026-09-11 翻新：用 52 頁實檔找出「短 PM」會令固定 6 頁規則錯位；改為尋找下一張工作單作邊界、按頁面內容去除背頁。辨認新增日期/電話/asset 交叉核對，不確定的名稱不再送 OneDrive。
> 2026-09-12 模型更新：圖片辨認首選 `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`，使用官方不思考 OCR 設定及嚴格 JSON 驗證；任何首選模型失聯時，同一批只等待一次，隨後使用已知可工作的 Llama 後備。
> 2026-09-14 安全翻新：PM 不足四張內容頁會停止並進 `_INCOMPLETE`；多輪 OCR 改為先取得欄位共識，Asana 正式使用 PM/CM project 類型；加入視覺防重複、耐久批次報告、PENDING 單檔重試及全程只讀測試。
> 2026-09-15 辨認翻新：用保留的多批實單校準固定欄位，半頁 OCR 改成分格欄位卡；訂單、型號、serial、醫院會高倍獨立複核，只有有爭議的格才再讀。加入醫院／部門語意隔離、serial 形狀安全閘及 DeepSeek 用量報告。

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

## 2. 各資料夾代表的狀態

| 位置 | 代表甚麼 | 下一步 |
|---|---|---|
| `googledrive:From_BrotherDevice/` | 原始掃描 PDF | Stage A 切頁 |
| `googledrive:From_BrotherDevice/_SPLIT/` | 已切成單一工作的 PDF | Stage B 辨認及上傳 |
| `googledrive:From_BrotherDevice/_SPLIT_FAILED/` | 找不到可信工作單邊界 | 人工檢查原始整批掃描 |
| `googledrive:From_BrotherDevice/_INCOMPLETE/` | 已切好，但 PM 缺 checklist 或疑似重複頁 | 重新掃描；不會進 OneDrive |
| `googledrive:From_BrotherDevice/_INCOMPLETE_RAW/` | 含缺頁工作的原始整批掃描 | 保留作查證，不會再次觸發 |
| `googledrive:From_BrotherDevice/_PENDING/` | 已切好，但名稱未能可靠核對 | 人工核對；不會進 OneDrive |
| `googledrive:From_BrotherDevice/_REPORTS/` | 每批目前處理狀態 | 系統使用；完成後歸入子資料夾 |
| `googledrive:From_BrotherDevice/.jobsheet-control/` | 私有 Asana 設備索引及版本 manifest | Stage B 只讀；不放 OneDrive、不放 Git |

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
7. CM 必須有 1 張內容頁；PM 必須有工作單加三張 checklist，共 4 張。頁數不足或 checklist 疑似重複便進 `_INCOMPLETE/`。
8. 完整工作上傳 `_SPLIT/`；同批其他完整工作不會被一份缺頁單阻塞。
9. 寫入 `_REPORTS/` 批次狀態後才處理原始 PDF。全部完整才刪；有缺頁便移入 `_INCOMPLETE_RAW/` 保存。

頁數規則：

```text
CM：通常 2 張掃描頁，成品必須是工作單正面 1 張
PM：通常 6 張掃描頁，成品必須是工作單 + checklist 1/2/3，共 4 張
短 PM：仍可用來尋找下一張工作單邊界，但不再當成完整；隔離到 `_INCOMPLETE`
```

`JOB NATURE` 是手畫圈選，四個選項是 `CM | PM | FCO | INS`。自動流程只接受一個明確的 CM 或 PM。

### 甚麼才會進 `_SPLIT_FAILED`

只限單據本身不能安全切頁：起始圈選讀不清、讀到 FCO/INS、6 頁範圍內找不到可信的下一張工作單邊界，或最後頁數不能按雙面掃描完整收尾。程式中這類情況叫 `SplitError`。

以下不是單據問題，絕不能搬去 `_SPLIT_FAILED`：GitHub 工作未開始、NVIDIA/Google Drive/網路故障、rclone 登入過期、PDF 讀寫失敗或程式意外。這些情況會讓 Stage A 顯示失敗，原始 PDF 留在入口，修好後可安全重跑。

## 4. Stage B：辨認、配對、上傳

入口：`python -m src.processor`
GitHub 工作：`.github/workflows/jobsheet-process.yml`

手動 Stage B 可填 `jobsheet_file`，並選 `source_queue=split|pending`。`dry_run=true` 時必須指定單檔，只做 OCR/Asana 查詢，不移動 Google Drive、不寫 OneDrive、不更新報告。

若人員已逐頁核對原檔及正確名稱，可在同一個單檔模式填 `confirmed_filename`（不含 `.pdf`）。此救援模式只處理指定 PDF，跳過 OCR/Asana；Windows/OneDrive 禁用的 `/` 等字元會轉成 ` - `。沒有同時指定 `jobsheet_file` 時程式拒絕執行，不能用它批量改名。

新掃描尚在入口時，使用獨立的 `Jobsheet Safe Dry Run` workflow，填完整原始 PDF 檔名。它在 runner 臨時目錄切頁、檢查完整性、OCR 及查 Asana，結束後不留下任何雲端改動，也不會觸發正式 Stage B。

流程：

1. 讀 `_SPLIT/` 的單一工作 PDF。
2. 按固定印刷座標分開抄錄訂單號、機身編號候選、型號、醫院、部門/房間、聯絡人、電話、asset、HAWO/WO 及服務日期。
3. 在 Asana 找候選工作，再用上述欄位交叉核對。
4. 上傳 OneDrive，成功後才刪 `_SPLIT/` 來源。

### Asana 設備索引（只作縮小候選）

手動工作 `Jobsheet - Refresh Asana Device Index` 會掃描目前 workspace 的 PM/CM
project，建立近兩年的設備索引。純月份名稱（例如 `2026 Jun`）是現行例行 PM
project，亦必須納入；不能只靠 project 名稱有沒有 `PM` 三個字。每部設備一列，以 `serial + product family` 作主鍵；
`Affiniti 70` 與 `Affiniti 70G` 歸入同系列，但保留所有實際寫法。同一列累積醫院正式名
及簡寫、詳細地點、部門/房間、電話、聯絡人、asset 及日期；每個歷史工作亦獨立保存
當次資料、PM/CM 類型、Asana task GID 及連結，CM 和 PM 聯絡人不會互相覆蓋。沒有
serial 的列標記為 weak，永不自動命名。索引不保存 Order Number，也不提供答案給
視覺模型猜。

程式檔 `src/asana_index.py` 產生三個檔案：

- `asana-device-index.json`：Stage B 使用的設備列及歷史 task GID。
- `asana-device-index.csv`：人工查看用，不含 Order Number。
- `asana-device-index-manifest.json`：schema、涵蓋日期、完成時間、task/device 數量。

三個檔案只寫入 Google Drive `.jobsheet-control/`。JSON/CSV 先替換，manifest 最後
替換；Stage B 下載 JSON 及 manifest 並核對版本，不一致或下載失敗便保留來源檔，
絕不把故障當成「沒有候選」。Stage B 先為設備計算全欄位分數：serial、產品系列、
醫院、部門/房間、電話、聯絡人、asset、日期及 PM/CM。設備最高分至少 60，而且須比
第二名高至少 15 分；serial 錯 1–3 字時，還必須有至少兩類非 serial 證據，其中一類
是產品系列、醫院、完整電話或 asset。鎖定設備後才為該設備的歷史工作另行排名，不會
自動選最新、最舊或未完成工作。最後逐個用 Asana task GID 即時讀完整名稱、描述、
日期、project 和 Order Number，只有即時核對仍唯一通過才命名。索引完全沒有候選時
才使用舊的即時 typeahead 搜尋後備；候選接近則直接留在 `_PENDING`，不擴大搜尋。
索引不能取代 Asana 最終資料，也不會改動正式工作單或 OneDrive。

第一輪把固定格子裁開，組成有清楚標籤及邊框的欄位卡；第二輪只以 4x 高倍獨立複核 `ORDER NO.`、`PRODUCT`、`SERIAL NO.`、`Customer Name`。兩輪不一致或不合格式時，只把有爭議的一格以 5x/6x 重讀；單格圖會裁走大部分印刷標籤及空白，只保留手寫值，並以普通／加強兩種影像避免重複同一誤讀。身分欄仍配不到 Asana 時，才第二次讀 `CONTACT PERSON`、電話、asset、HAWO/WO 及 ACTION DATE 等輔助欄；兩張卡的聯絡人、電話或 ACTION DATE 不一致時，最後只再讀有爭議的一格。

`Customer Name` 是醫院；`Dept./Room No.` 只可是樓層、病房或 asset；`CONTACT PERSON` 獨立抄成 `contact_person_raw`，不能由電話推斷。`Asset# 19130438` 會派生成 asset 證據，並從位置文字移除，絕不當作醫院或地點。醫院及部門內部欄位叫 `hospital_raw`、`department_room_raw`；舊 `customer_raw`、`location_raw` 只作相容映射。

圖片先做黑白自動對比及銳化。訂單號、電話、serial、asset、HAWO/WO 及日期另有本機格式檢查；不合格式的輸出不會送進 Asana。serial 必須以 2 至 3 個英文字母開始、共 8 至 12 位並至少含 4 個數字；`15915F0726`、`S2N22F1275` 這類結果會觸發單格重讀，程式不會擅自補成 `US...` 或 `SZN...`。ACTION DATE 超出最近 93 日或未來超過 7 日亦不作配對證據。

格式不合的 serial 原始抄錄只暫存在記憶體，不用作 Asana 全域搜尋，也不會寫進檔名。設備索引可容許有效形狀的 serial 與固定 serial 相差 1–3 字，但必須同時通過 60 分、領先 15 分及多欄證據安全閘；這是程式比較，不是模型補字。索引不存在該設備而回退即時搜尋時仍只容許一字差異，避免在整個 workspace 以模糊 serial 猜測。

視覺模型只負責照字抄錄，提示詞明確禁止猜醫院、補全機身編號或沿用先前圖片；模型看不到 Asana 候選或醫院答案清單。Actions 日誌只記「哪些欄位看得到」、匿名前三名的分數/serial 距離/證據類別，以及非敏感的呼叫次數、耗時、token 和 DeepSeek 費用上限，不印電話、聯絡人、asset、serial 或客戶內容。

命名次序：

1. Asana 名稱有 8 位訂單號：`SR#訂單號.pdf`。
2. 找到 Asana 工作但沒有訂單號：使用完整 Asana 工作名稱；`/` 等 Windows/OneDrive 禁用字元統一轉成 ` - `。
3. OneDrive 已有視覺相同內容：沿用既有檔，不重複上傳。
4. 同一名稱但內容不同：保留第二版本，命名為 `_重掃_YYYYMMDD-HHMM`，不再用 `(1)`。
5. 無法可靠配對：不碰 OneDrive，完整檔留在 `_PENDING`。

舊版建立的 `[待核對]` 名稱只為歷史相容，新流程不再新增。Asana 連不上或權限失效屬整體系統故障：來源保留在 `_SPLIT`，Stage B 顯示失敗。單一 NVIDIA 辨認暫時失敗會保留並記錄重試，三次後才進 `_PENDING`。

### 重跑及重掃不製造難辨認的重複檔

若 OneDrive 已上傳，但刪除 Google Drive 暫存檔時中斷，重跑會先比 SHA-256，再把每頁轉成低解像度影像比較。重新編碼但畫面相同便沿用原檔；同一訂單內容真的不同才保留 `_重掃_日期時間` 第二版本，永不自動覆蓋舊檔。

## 5. Asana 配對規則

Asana 只找出一小批候選，最後核對在本機完成：

正式 Stage B 會先讀 `.jobsheet-control/asana-device-index.json`。索引先排設備，再排
該設備的歷史 task；命中時只讀取該列內的 task GID，避免以 OCR 的一個錯字把整個
workspace 撈成大候選池。每次仍即時讀取 Asana task 作最後確認。索引沒有任何候選才
回到下面的即時搜尋；索引候選並列、下載失敗或格式錯誤都不會當成空索引。

1. 用醫院、產品、機身編號、訂單號、HAWO/WO、8 位電話及較長 asset 分別找候選；電話與 asset 搜尋只負責擴大候選池，回來後仍須在 Asana 標題/描述中精確核對。
2. 校正常見型號小錯字，例如 `EPLQ 5G` 可校正為 `EPIQ 5G`。
3. 候選會再讀完整 task 及所屬 project/section；明確標為 CM/repair 的候選不能配給 PM 紙，反之亦然。
4. 機身編號完全相同仍須日期、電話、asset、HAWO/WO、「醫院+型號」或相符 project 類型支持，避免挑到同一設備的舊工作。
   若兩次獨立辨認的 serial 只差一個字元（常見 O/0、6/G），保留兩個候選而不先猜；之後必須再有至少兩項證據，且 Asana 最佳候選明顯領先才可命名。
5. 設備索引內可容許 serial 相差 1、2 或 3 字，依次降低分數；每種情況都至少要兩類非 serial 證據，而且產品系列、醫院、完整電話或 asset 至少命中一類。設備總分至少 60、比第二名高至少 15 分才可進歷史工作層。相差 4 字、弱設備列或分數接近一律送 `_PENDING`。
   若 serial 三輪仍無法形成共識，只有電話、asset、ACTION DATE 三項都與同一個有固定 serial 的設備及其完整 Asana task 精確相符時才可通過；即時搜尋後備仍只容許一字差異。
6. 完成/未完成都可以是正確工作。`modified_at` 不代表服務日期；優先比較 ACTION DATE 與 Asana 描述、start/due 日期。
   兩輪都抄到相同服務日期時，即使模型漏填 `date_source`，亦視為 ACTION DATE；兩輪不一致時只重讀日期格，單輪讀數仍不會採用。
   手寫年份若明顯落在三個月範圍外（常見把 `6` 看成 `0/5`），只有在 Asana 候選日期屬最近三個月、月日相差不超過 14 天時才校正年份；最終仍須 serial 及其他證據，不能只靠日期命名。
7. 只有已核對的短醫院碼才可搜尋或加分；`PYN` 與 `PYNEH` 視為同院，會用兩種短寫撈候選。未知短碼（例如模型幻覺的 PN、KWM、PYTV）會觸發醫院格重讀；詳細樓層只屬 Dept./Room，不會混入醫院名。完整醫院名若有最多兩個 OCR 字元誤差，只在已確認清單內存在唯一最近答案時才由程式校正；候選名稱不會提供給圖片模型。
8. 聯絡人忽略大小寫、空格及標點作低權重相似比對；因 CM/PM 對接人可不同，它不能單獨決定設備或工作。Asana 常見的 `25956917 Ms.Yan` 會拆成電話及聯絡人；`wo: 19130438` 視為工作單上的 asset/WO 證據。明確 Asset/WO 及 Order Number 不得再誤當電話。
9. 同一設備的歷史工作再按 ACTION DATE、PM/CM、當次電話、聯絡人、asset 及地點排名；不以建立時間、完成狀態或最新/最舊作決勝。候選並列或證據不足，一律留在 `_PENDING`。

已知型號：`Affiniti 30/50/70`、`EPIQ 5G/7G/7+/Elite/CVx`、`CX30/CX50`。

Asana `typeahead` 可跨 project，不再把每月 project 編號寫死。它不是完整清單，所以程式會用幾個可靠欄位分別搜尋後合併結果。

## 6. Apps Script：發現檔案及防重複

檔案：`apps_script/trigger.gs`

Apps Script 每 5 分鐘看一次 Google Drive。發現 PDF 後等 60 秒，避免掃描器仍在上傳，再通知 GitHub 啟動 Stage A。

- 一次只允許一個檢查程序，避免定時與手動執行撞在一起。
- GitHub 確認收到通知後，同一批依 20、40、80、160、320 分鐘逐步退避，最高約 6 小時。
- GitHub 沒收到通知便不開始冷卻，5 分鐘後可再試。
- queue 以 Google Drive 檔案 ID 和更新時間辨認；新 PDF 不會沿用舊批次的長退避。
- GitHub 已有 Stage A/B 執行或排隊時不重複通知。
- `_SPLIT` 有暫時辨認失敗的文件時亦會按退避重試；三次仍失敗才進 `_PENDING`。
- 入口清空後清除冷卻，下一批可立即開始。

Apps Script 的 Project Settings → Script properties：

| 名稱 | 必要 | 用途 |
|---|---|---|
| `GITHUB_PAT` | 是 | 通知 GitHub及讀取結果；只授權本 repo，`Contents: Read and write`、`Actions: Read-only` |
| `ALERT_EMAIL` | 建議 | 收 Google 端故障電郵；填自己的地址 |

加入 `ALERT_EMAIL` 後，手動執行一次 `checkDriveAndTrigger()` 批准寄信權限。地址不是密鑰，但不要寫死在公開程式碼。

## 7. 告警

`.github/workflows/jobsheet-failure-alert.yml` 分別監看 Stage A、Stage B。只有登入、雲端、網路、程式等整體故障令 workflow 失敗才計入；缺頁、配對證據不足及單檔辨認重試不當成 pipeline 崩潰。連續 3 次才建立 `[Pipeline Alert] ...` Issue，後續失敗只靜默更新同一 Issue。

每批的 `_REPORTS/*.json` 是耐久狀態。Apps Script 只在報告最終包含 `incomplete`、`pending` 或 `versioned` 時寄一封中文摘要；全部成功不寄信。寄出或確認無需通知後，報告移入 `_REPORTS/_REPORTS_SENT/`，不會重寄。

如果 GitHub 連告警工作都無法啟動，Issue 當下也不會建立。因此 Apps Script 會在設定 `ALERT_EMAIL` 後做第二層檢查：某階段連敗 3 次寄一次；恢復後重設。若連續 3 次連 GitHub 狀態也讀不到，會寄另一封通知。寄信失敗不阻止正常處理。

## 8. 設定及過時項目

Asana 設備索引不新增 secret；刷新工作沿用 `ASANA_TOKEN`、`ASANA_WORKSPACE_GID`。
它只在手動啟動 `Jobsheet - Refresh Asana Device Index` 時更新，預設涵蓋今天往前兩年，
不設每日排程。首次建立或索引過舊時可完整重掃；完成後才以 manifest 作版本交換。

GitHub Secrets：

| 名稱 | 必要 | 說明 |
|---|---|---|
| `RCLONE_CONFIG` | 是 | Google Drive / OneDrive 的 rclone 登入設定（base64） |
| `NVIDIA_API_KEY` | 是 | 視覺辨認服務 API key |
| `DEEPSEEK_API_KEY` | 測試時是 | DeepSeek 官方付費 API key；只放 Secret，不寫進變數或 repo |
| `ASANA_TOKEN` | 是 | Asana 存取權杖；需能讀 task、workspace typeahead、project 及 section |
| `OCR_PROVIDER`（Actions variable） | 否 | 正式流程選 `nvidia` 或 `deepseek`；未設定時仍用 NVIDIA |
| `DEEPSEEK_MODEL`（Actions variable） | 否 | DeepSeek 模型；未設定使用 `deepseek-flash`（V4.1 Flash） |
| `NVIDIA_MODEL`（Actions variable） | 否 | 臨時換辨認模型；不填使用程式預設 Nemotron 3 Nano Omni |
| `ASANA_WORKSPACE_GID`（Actions variable） | 否 | 日後搬 Asana workspace 才覆蓋；不填使用已核對的目前 workspace |

不要把值寫進 repo、日誌或文件。若 Asana 日後改成細分權限的 token，至少要有 `tasks:read`、`workspaces.typeahead:read`、`projects:read`、`project_sections:read`；這些仍是同一個 `ASANA_TOKEN`，不需新增 secret 名稱。

2026-09-12 官方狀態檢查：

- 舊模型 `nvidia/llama-3.1-nemotron-nano-vl-8b-v1` 的免費入口已停用。
- 預設使用有免費入口、支援圖片、OCR 及 JSON 輸出的 `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`。
- Jobsheet 是抄錄工作，不需要長篇推理；程式依官方 instruct 設定關閉 thinking，使用 `top_k=1`、1024 輸出上限及較穩定的低溫度。
- 2026-09-12 用新舊 key 實際測試 Kimi K3，均在回傳任何資料前超時；這只證明 Kimi 免費入口當時不可用，不代表 key 本身無效。
- 2026-09-12 GitHub `NVIDIA Model Check` run 34628392304：新 key 呼叫 Nemotron 成功，正確讀出假圖片的六位數字並通過 JSON 驗證；測試沒有讀取 Google Drive、Asana 或 OneDrive。
- 2026-09-12 GitHub Stage B run 34669655930：以單檔安全模式處理一份 4 頁 PM 實單，兩個清晰度命中同一個 Asana 工作；Nemotron 遇到一次 503 後短重試成功，OneDrive 成品與 Google Drive 來源的 SHA-256 完全相同，另外 10 份佇列檔沒有被處理。
- 後備為曾成功取得 HTTP 200 的 `meta/llama-3.2-11b-vision-instruct`：Nemotron 遇到暫時性 503/timeout 會短重試一次才用後備；曾長時間掛起的 Kimi 維持一次即後備。
- 所有要求 JSON 的辨認（包括後備模型）至少保留 1024 個輸出 token，避免模型回覆 HTTP 200、但答案在完整 JSON 結束前被截斷。格式仍由本機嚴格驗證；不完整內容只會等待安全重試。
- 每次 NVIDIA 網路等待最多 45 秒，SDK 不做隱藏重試；全部模型失敗時檔案保留在 `_SPLIT`。
- 欄位辨認要求圖片模型只回指定 JSON：order、serial 候選、產品、醫院/位置、聯絡人、電話候選、asset 候選、HAWO/WO、ACTION DATE 及讀不清欄位；若服務在 JSON 外加短說明或 markdown 外框也能安全讀取，但不會從散文硬猜。
- 提示文字不可放入看似真實的機身編號、型號或醫院範例；模型在字跡難讀時可能直接複製範例，造成錯配。
- 可用 GitHub Actions variable `NVIDIA_MODEL` 暫時覆蓋，不需先改程式；模型名稱不是密鑰，不放 Secrets。
- 2026-09-14 加入 DeepSeek 官方付費入口。`deepseek-flash` 對應
  DeepSeek-V4.1-Flash，支援圖片及 JSON；OCR 關閉 thinking，避免抄錄工作
  產生不必要的推理延遲。加入能力本身不會改正式流程，`OCR_PROVIDER` 未設定
  時仍使用 NVIDIA。
- 2026-09-15 DeepSeek 測試輸入改為固定分格欄位卡，避免模型自行判斷相鄰
  手寫值屬於哪一欄。正式切換條件是：私人實單 dry-run 全部名稱正確、錯誤
  上傳為零；通過前 `OCR_PROVIDER` 仍保持 NVIDIA，通過後才改成 `deepseek`。
- `DeepSeek V4.1 Vision Check` 只讀程式即時產生的假圖片，不接觸任何客戶
  工作單。`Jobsheet Safe Dry Run` 可逐次選 `deepseek` 或 `nvidia`，供同一份
  原始掃描比較；兩者都不移動 Drive 檔案及不寫 OneDrive。
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
- `_INCOMPLETE` 有檔：掃描器漏頁或重複 checklist；重新掃描，不要手動送 OneDrive。
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

正式雲端前的安全測試：手動啟動 `Jobsheet Safe Dry Run`，輸入入口原始 PDF 的完整檔名並選擇 `deepseek` 或 `nvidia`。結果只顯示預計切頁、缺頁及預計名稱，不寫入 OneDrive。

分格 OCR 回歸檢查使用本機 `tmp/` 保留的私人 PDF，不提交 GitHub。已核對的完整成品必須全部得到正確預計名稱，缺頁樣本必須停在不完整狀態，錯誤配對必須為零；達標後才把正式 `OCR_PROVIDER` 改為 `deepseek`。dry-run 報告同時列出每份工作單的圖片呼叫次數、耗時、token 及保守費用上限。

只測 NVIDIA 模型能否看圖及回傳合格 JSON：在 GitHub Actions 手動執行 `NVIDIA Model Check`。它只讀一張程式即時產生的假資料圖片，不會讀 Google Drive、Asana 或 OneDrive。

只測 DeepSeek V4.1 Flash 的付費 Key、圖片輸入及 JSON：手動執行 `DeepSeek V4.1 Vision Check`。這個檢查同樣只使用假圖片；成功後才以 `Jobsheet Safe Dry Run` 測一份真實掃描。

## 11. 程式地圖

| 檔案 | 責任 |
|---|---|
| `src/splitter.py` | Stage A：切頁、上傳 `_SPLIT`、成功後刪原檔 |
| `src/pdf_utils.py` | 工作單邊界、頁面內容判斷、驗算及抽頁 |
| `src/pdf_identity.py` | PDF 頁面視覺指紋；辨認重新編碼的相同內容 |
| `src/batch_state.py` | `_REPORTS` 耐久批次狀態、重試次數及通知結果 |
| `src/processor.py` | Stage B：辨認、Asana 配對、OneDrive 上傳 |
| `src/dry_run.py` | 原始 PDF 全流程只讀預覽 |
| `src/nvidia_client.py` | 固定欄位裁切及 NVIDIA/DeepSeek 圖片呼叫（檔名為歷史相容） |
| `src/asana_index.py` | 建立、驗證及輸出私有 Asana 設備/歷史工作索引 |
| `src/asana_client.py` | 設備與歷史工作兩層模糊排名；最後即時核對 Asana task |
| `src/rclone_helper.py` | 雲端列檔、下載、上傳、比較、刪除 |
| `src/healthcheck.py` | 只讀列出各處理位置現況 |
| `src/config.py` | 路徑、模型、裁切範圍及業務常數 |
| `apps_script/trigger.gs` | 發現新 PDF、防重複、電郵告警 |
| `.github/workflows/jobsheet-split.yml` | Stage A |
| `.github/workflows/jobsheet-process.yml` | Stage B |
| `.github/workflows/jobsheet-asana-index.yml` | 手動建立／增量刷新近兩年設備索引 |
| `.github/workflows/jobsheet-failure-alert.yml` | 連敗 Issue 告警 |
| `.github/workflows/quality-check.yml` | 每次改程式自動測試 |
| `.github/workflows/nvidia-model-check.yml` | 手動、無客戶資料的 NVIDIA 圖片連線測試 |
| `.github/workflows/jobsheet-dry-run.yml` | 指定一份原始 PDF 的全程只讀測試 |

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
11. PM 不足四張內容頁不得進 Stage B；CM 成品固定一張。
12. dry-run 不得移動／刪除 Google Drive、更新報告或寫入 OneDrive。

## 13. 官方參考

- 現用 NVIDIA 模型：<https://build.nvidia.com/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning>
- 舊 NVIDIA 模型：<https://build.nvidia.com/nvidia/llama-3.1-nemotron-nano-vl-8b-v1>
- Asana 搜尋：<https://developers.asana.com/reference/typeaheadforworkspace>
- Asana 限流：<https://developers.asana.com/docs/rate-limits>
- rclone 列檔：<https://rclone.org/commands/rclone_lsf/>
- rclone 單檔刪除：<https://rclone.org/commands/rclone_deletefile/>
- Apps Script 防同時執行：<https://developers.google.com/apps-script/reference/lock>
- Apps Script 寄信：<https://developers.google.com/apps-script/reference/mail/mail-app>
