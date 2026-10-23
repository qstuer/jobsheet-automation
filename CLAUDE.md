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
> 2026-09-15 配對翻新：私人 Asana 索引改為真正一個 Serial 一行；程序先按產品大類、醫院及 Serial 分層模糊搜尋，只有前兩名非常接近時才把最多 10 行交給圖片模型作受限複核。
> 2026-09-16 回測工具：新增手動 `Jobsheet 20-Sample Backtest`，从 Google Drive 私人控制目录读取 20 份匿名实单及答案，只做 OCR／Asana 核对；不会写入或移动 Google Drive／OneDrive，公开摘要只显示 B01–B20、结果及用量。
> 2026-10-05 正式配對更新：Stage B 使用 `evidence-live-v2`；四份新 PM 各做三次只讀測試，12/12 的設備、具體工作、檔名及 PM／CM 均正確。這不是所有未見樣本的保證，也不代表 OCR 每格均讀準。

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
project，亦必須納入；不能只靠 project 名稱有沒有 `PM` 三個字。索引是一個 Serial 一行；
同一 Serial 曾寫成 `Affiniti 70`、`Affiniti 70G` 或其他型號，也只保留一行並保存所有實際寫法。同一列累積醫院正式名
及簡寫、詳細地點、部門/房間、電話、聯絡人、asset 及日期；每個歷史工作亦獨立保存
當次資料、PM/CM 類型、Asana task GID 及連結，CM 和 PM 聯絡人不會互相覆蓋。沒有
serial 的列標記為 weak，永不自動命名。索引不保存 Order Number，也不提供答案給
視覺模型猜。

程式檔 `src/asana_index.py` 產生三個檔案：

- `asana-device-index.json`：Stage B 使用的設備列及歷史 task GID。
- `asana-device-index.csv`：人工查看用，不含 Order Number。
- `asana-location-index.csv`：人工查看用；每間醫院一行，分開正式名／已確認簡寫、歷史詳細地點、部門／房間及未確認短碼。
- `asana-device-index-manifest.json`：schema、涵蓋日期、完成時間、task/device 數量。

統一地點表亦嵌在設備 JSON，Stage B 不必多下載一個檔案。未知短碼會留在地點 CSV
供人工查看，但標為不可配對；同一設備曾搬院亦不會令兩間完整醫院名稱變成同義詞。
只有簡寫與完整名稱在至少兩個不同 Serial 重複並存，才可由程序學成別名。
Asana 標題前的 `(Cancel)`、`(Aug)`、`(**BESS)`、`(Office)` 等狀態標籤會被移除；
`QMH K3`、`TKO MB-G-A` 這類「大寫醫院碼 + 院內位置」會拆成醫院及詳細位置。
沒有完整括號的狀態文字只保留供人工查看，不參與自動配對。

目前索引 schema 是 `3`。四個檔案只寫入 Google Drive `.jobsheet-control/`。JSON/CSV 先替換，manifest 最後
替換；Stage B 下載 JSON 及 manifest 並核對版本，不一致或下載失敗便保留來源檔，
絕不把故障當成「沒有候選」。Stage B 先要求產品大類及醫院相似率各達 33%，取兩者交集；
建立時遇到 Asana 429/5xx 或暫時連線錯誤會自動退避重試最多五次；仍失敗時會在建立步驟立即停止，不會進入上傳步驟。
再要求 Serial 相似率達 50%。第一名領先第二名超過 10 個百分點便由固定程序鎖定；差距
不超過 10 點時，才把最多 10 個候選及工作單欄位交給同一圖片服務作一次受限複核。
模型只能回候選編號、吻合欄及衝突欄，不能產生表外答案；表示不確定便留 `_PENDING`。
電話是強輔助、聯絡人是低權重輔助、Asset 有填才加強。ACTION DATE 通常不參與選設備；
唯一例外是 Serial 只差一個字元，而同一筆歷史工作上的產品大類、醫院組、完整電話、
PM/CM 及正式 Asana 到期／完成日期（與工作單相差不超過一天）全部吻合。只有一部設備
完整通過才可糾正 Serial；缺少任一證據或多部設備通過便留 `_PENDING`。鎖定設備後才用
日期在該 Serial 的歷史工作中找前後不超過 31 日的工作，再核對 PM/CM、
當次電話、聯絡人、Asset 和地點，不會自動選最新、最舊或未完成工作。最後逐個用 Asana
task GID 即時讀完整名稱、描述、日期、project 和 Order Number，只有仍唯一通過才命名。
索引完全沒有候選時才使用舊的即時 typeahead 搜尋後備。
索引不能取代 Asana 最終資料，也不會改動正式工作單或 OneDrive。

更新解析規則後，手動刷新索引時必須勾選 `full_rebuild`，忽略舊索引並從 Asana
完整重建；否則增量模式會沿用舊版已誤分類的電話／asset。平日資料更新則不勾選，
只重新處理新增或修改過的工作。

### 20 份私人實單回測

手動工作 `Jobsheet 20-Sample Backtest` 只用作改版后的回归检查。20 份 PDF 和私人答案
放在 `.jobsheet-control/backtest-20/`，文件只以 `B01`–`B20` 识别；工作会先拒绝完全重复的
PDF，再使用当前设备索引逐份执行正式 OCR 与 Asana 最终读取。它直接调用只读辨认核心，
不会读取正式 `_SPLIT` 队列，也没有任何 OneDrive 上传、Google Drive 移动或删除路径。
公开 Actions 摘要只显示匿名编号、PASS/FAIL、图片调用次数、时间、tokens 和费用上限；
Serial、电话、联系人、地点、Asana task 及预期答案都留在私人运行记忆体内。回测不通过
属于模型品质结果，不会触发正式 pipeline 的连续失败告警；基础设施或私人清单损坏才令
workflow 失败。

第一輪把固定格子裁開，組成有清楚標籤及邊框的欄位卡；第二輪只以 4x 高倍獨立複核 `ORDER NO.`、`PRODUCT`、`SERIAL NO.`、`Customer Name`。兩輪不一致或不合格式時，只把有爭議的一格以 5x/6x 重讀；單格圖會裁走大部分印刷標籤及空白，只保留手寫值，並以普通／加強兩種影像避免重複同一誤讀。身分欄仍配不到 Asana 時，才第二次讀 `CONTACT PERSON`、電話、asset、HAWO/WO 及 ACTION DATE 等輔助欄；兩張卡的聯絡人、電話或 ACTION DATE 不一致時，最後只再讀有爭議的一格。

`Customer Name` 是醫院；`Dept./Room No.` 只可是樓層、病房或 asset；`CONTACT PERSON` 獨立抄成 `contact_person_raw`，不能由電話推斷。`Asset# 19130438` 會派生成 asset 證據，並從位置文字移除，絕不當作醫院或地點。醫院及部門內部欄位叫 `hospital_raw`、`department_room_raw`；舊 `customer_raw`、`location_raw` 只作相容映射。

圖片先做黑白自動對比及銳化。訂單號、電話、serial、asset、HAWO/WO 及日期另有本機格式檢查；不合格式的輸出不會送進 Asana。serial 必須以 2 至 3 個英文字母開始、共 8 至 12 位並至少含 4 個數字；`15915F0726`、`S2N22F1275` 這類結果會觸發單格重讀，程式不會擅自補成 `US...` 或 `SZN...`。ACTION DATE 超出最近 93 日或未來超過 7 日亦不作配對證據。

格式不合的 serial 原始抄錄只暫存在記憶體，不用作 Asana 全域搜尋，也不會寫進檔名。設備索引容許 OCR serial 有數個錯字，但正規化相似率必須達 50%，並已先通過產品大類及醫院兩道 33% 閘門；這是程式比較，不是模型補字。索引不存在該設備而回退即時搜尋時仍只容許一字差異，避免在整個 workspace 以模糊 serial 猜測。

一般視覺辨認只負責照字抄錄，提示詞明確禁止猜醫院、補全機身編號或沿用先前圖片。只有固定程序判定前兩個索引候選太接近時，模型才會額外看最多 10 行私人候選表，而且只能在表內選編號或表示不確定。Actions 日誌只記「哪些欄位看得到」、匿名相似率/證據類別，以及非敏感的呼叫次數、耗時、token 和 DeepSeek 費用上限，不印電話、聯絡人、asset、serial、候選表或客戶內容。

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

### 現行正式入口：evidence-live-v2（2026-10-05）

`jobsheet-process.yml` 明確設定 `JOBSHEET_MATCHING_RULES=evidence-live-v2`，
由 `processor._ocr_and_match` 呼叫 `src/matching_rules.py`。
以下規則優先於本章後方的舊版細則；未設定這個變數才使用舊入口，方便回退。
圖片供應商、切割方法、Asana 私人索引存放位置及 OneDrive 防重複流程不變。

- 保存多次讀取的有效候選，不因一次重讀不同永久丟棄。模型不會收到 task 或檔名答案。
- 先按 Serial 找私有索引設備，完全相同的 Serial 不被舊索引的產品誤解析擋下；
  再即時讀該設備的歷史 task，以現行產品解析、PM／CM 專案及正式日期核對。
- Serial 差一至三字仍可留候選，但必須有多欄支持、唯一設備及唯一工作；
  已確認的醫院／產品衝突、候選並列都不放行。最多 10 部設備、80 個歷史工作。
- 醫院缺失／未確認、Serial 只差一字時，必須有重複產品大類、完整電話、
  可靠原件日期及唯一的同類工作才能確認；這條路徑不能借上傳日期後備放行。
- Asset 缺失／不符不扣分；四捨五入達 70% 才加輔助分，不能代替精確電話。
- 可靠原件日期與 Asana 正式到期／完成日期前後 14 天核對；PM 未能確認便讀
  第二頁 checklist 固定 Customer／System／sn／Date／Asset No.。跨頁確定身份衝突停下。
- 日期仍不能讀穩才用原始上傳日前 70 天至後 30 天範圍，挑唯一最近工作；
  完成或未完成都可考慮。不用 `modified_at` 或重跑日冒充服務／上傳日期。
- Stage A 以 rclone 只讀 metadata 的 Drive `btime` 保存 `source_uploaded_at`，
  Stage B 按香港時區換算日期。批次 `created_at` 是處理時間，不是上傳時間。
  舊報告缺此欄時視為未知，不猜日期，也不以切割後 PDF 的新建立時間替代。
- 完全沒有圖片服務回應時走原有耐久重試，不把基礎設施故障當作配對不足。
  索引／Asana／Drive 故障仍保留來源；只有唯一工作通過才進原有上傳及防重複路徑。
- 此入口拒絕配對後不再借舊 matcher 的中性設備名稱繞過；仍留 `_PENDING`。
- 正式公開報告隱藏客戶檔名原文，意外錯誤只記類型，不輸出含客戶資料的 traceback；
  真正名稱及耐久處理結果仍寫入原有私人 OneDrive／Drive 位置。

實驗旗標 `JOBSHEET_EVIDENCE_LIVE_RULES=1` 繼續限定 `JOBSHEET_DRY_RUN=1`，
不能因此啟動上傳。測試分支的真實結果：第一版 8/12 正確、4/12 待確認；
第二版 `81ecaa9` 為 12/12 正確、0/12 待確認；兩版均零錯配、零 OneDrive 寫入。
第二版九次使用可靠原件日期、三次使用原始上傳日期後備；部分 Serial 仍讀錯一字。
主／後備模型回應比例不同，同批資料亦參與過診斷，不能宣稱已完全排除過擬合。
上線驗證及日後觀察不得用挑出的最好一輪取代完整結果。

### 舊版配對細則（僅未設定新入口時使用）

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
5. 設備索引先用產品大類與醫院各 33% 的門檻縮小表格，再以 serial 50% 門檻排名；型號數字及後綴不參與產品大類比較。第一名領先超過 10 個百分點才由程式鎖定；差距較小則讓圖片模型只在最多 10 個表內候選中複核一次，不確定便送 `_PENDING`。
   若 serial 完全讀不到，只有產品大類、醫院、完整電話和 Asset 四項都精確而且唯一命中同一行時才可繼續；即時搜尋後備仍只容許一字差異。
6. 完成/未完成都可以是正確工作。`modified_at` 不代表服務日期；ACTION DATE 通常只在設備確定後比較該 Serial 下面的 Asana 正式到期／完成日期，正式欄位存在時不會用描述內的舊日期取代。唯一跨設備例外是 Serial 只差一字元，且同一歷史工作上的產品大類、醫院、完整電話、PM/CM 和相差不超過一天的正式日期全部吻合；候選必須唯一，否則留 `_PENDING`。
   兩輪都抄到相同服務日期時，即使模型漏填 `date_source`，亦視為 ACTION DATE；兩輪不一致時只重讀日期格，單輪讀數仍不會採用。
   手寫年份若明顯落在三個月範圍外（常見把 `6` 看成 `0/5`），只有在 Asana 候選日期屬最近三個月、月日相差不超過 14 天時才校正年份；最終仍須 serial 及其他證據，不能只靠日期命名。
7. 只有已核對的短醫院碼才可搜尋或加分；`PYN` 與 `PYNEH` 視為同院，會用兩種短寫撈候選。未知短碼（例如模型幻覺的 PN、KWM、PYTV）會觸發醫院格重讀；詳細樓層只屬 Dept./Room，不會混入醫院名。完整醫院名若有最多兩個 OCR 字元誤差，只在已確認清單內存在唯一最近答案時才由程式校正；候選名稱不會提供給圖片模型。
8. 聯絡人忽略大小寫、空格及標點作低權重相似比對；因 CM/PM 對接人可不同，它不能單獨決定設備或工作。Asana 常見的 `25956917 Ms.Yan` 會拆成電話及聯絡人；`wo: 19130438` 視為工作單上的 asset/WO 證據。明確 Asset/WO 及 Order Number 不得再誤當電話。
   電話或 asset 若因 OCR／Asana 人手輸入多一位、少一位或錯一位，只給較低的模糊分，不能單獨決勝；`EPIQ 7 Plus` 與 `EPIQ 7+` 視為同一產品系列。
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
| `src/device_table_search.py` | 私人設備表多條件查詢；設備歷史別名與當次工作分開 |
| `src/device_search_agent.py` | 明確只讀模式下的受控模型查表協定；不產生 OCR 票或上傳授權 |
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

## 13. 2026-10-13 獨立測試分支，尚未部署 main

- 從正式 `c750f6b` 建立獨立分支 `codex/parser-ingress-fix-20261013`；原有未提交的實驗 checkout 保持原狀。經使用者確認 diff 後，修正已提交並推到此分支，`main` 未更新。
- 修正 OCR 的舊產品白名單：私人索引中安全的產品寫法／大類可作比較證據，但不代表型号已獲人工確認，也不會補造數字或後綴。弱設備列只能提供詞彙，不能確認設備身分。
- 6–7 字元的完整混合 Serial 只保留為視覺證據；不更改一般 Serial 格式閘。短 Serial 的即時查詢需重複產品及醫院證據與完整電話；接受時還需重複且完全相同的 Serial、正確產品大類／醫院／完整電話、PM／CM 與唯一日期工作。不能模糊改字或補成長 Serial。
- Asana 的短 Serial 只從獨立 Product／Serial 標題欄解析；多個合理短號不選第一個。產品／標題解析版本升至 2，後續更新索引必須完整重建，不能增量混用舊欄位。本次未更新雲端索引。
- 只讀結果增加白名單化的配對原因、數量及吻合布林值，不輸出電話、客戶、Serial、Asset、task GID 或原始 OCR。原有工作／檔名核對碼保留。
- 原始修正的本機與 GitHub 程式測試為 287 項通過；加上測試入口、日期及即時工作刷新回歸測試後，本機為 294 項通過。這不是實單準確率。
- Stage B 停用時 GitHub 拒絕手動 dispatch，因此本分支的 `Jobsheet Safe Dry Run` 增設僅此分支可用的 `_SPLIT` 單檔只讀入口；強制 dry-run，不啟動正式上傳 workflow。
- 三次診斷發現實驗旗標覆蓋原始上傳日期；結果保留，不作最後完整五份結果。現改為保留 source manifest 傳入的日期，僅無此日期時才用實驗環境後備。
- 舊索引不一定含最近新增工作。現僅用已取得的候選設備完整 Serial 補查 Asana；仍限制 10 部設備／80 個工作，API 故障停止，不能廣搜電話／Asset。官方已確認的 HKBH 短碼亦補入名稱對照；未知碼限制不變。
- `b5b1ecc` 的完整五份真單只讀結果：正確工作及檔名 2/5，待核對 3/5，錯誤自動配對 0。64 次圖片呼叫、273.8 秒累計呼叫耗時、339866 tokens；首選 503 十次、四份使用後備。費用未查證，供應商仍為 NVIDIA。
- 這五份已參與診斷，是開發集，不可當作未見驗證；亦不可挑出最好一輪宣稱全部自動通過。人工正確抄錄的 parser-only 檢查亦不是 OCR／配對準確率。
- 使用者另授權測試後歸檔。五份原件及最新 Asana 資料由代理逐項獨立覆核，11 張成品頁均與原掃描一致；5/5 已用單檔不覆蓋方式交付 OneDrive，下載 SHA-256 全相符。三份屬代理覆核後協助歸檔，不能算系統自動通過。
- 已保留原始整批及五份成品來源至 Google Drive 私人控制資料夾，成功結果先寫入耐久批次報告，再把已交付的五份移出 `_SPLIT` 防重跑；未刪原件、未更改分享權限、未覆蓋 OneDrive 舊檔。
- 正式 Stage B 仍停用，`main` 仍是原版；不要以手動交付成功當成改良版本可上線的證據。客戶名稱、電話、Serial、Task GID、私人報告及下載憑證都不得提交 Git。

### 2026-10-13 後續 OCR 改良：僅本機、尚未提交或推送

- 已核實的產品字詞與設備身分品質分開：保留 IU22、TC20、EPIQ 5W、Affiniti 50W 等寫法，不因弱設備列或頻率前 12 名限制而漏掉。四字元短型號最多校正一字，不改數字及後綴。未知寫法不靠出現次數升格。
- 首頁兩輪一致的產品原文可作字形錨點，不代表產品已確認。單格重讀跨到無關系列時記 `product_reread_drift`，保留原文但不加配對票；同數字、同後綴且至少一半字形接近的拼寫更正不因此被擋下。PM 可用獨立 checklist 補證，真正跨頁身份衝突仍停止。
- 短 Serial 的 O/0 歧義僅保留原字串，不產生新字串、不補前綴。只可與已取得的真實 6–7 字元 Serial 比對單一 O/0 差異，並要求重複醫院／產品、重複完整電話、可靠日期 ±14 天、PM/CM 與唯一設備及工作；不能借上傳日期後備放行。
- 舊弱索引列只提供受限 task GID 線索，不當作設備答案。產品／醫院／完整電話須在同一歷史 task 相符；若舊 parser 把產品欄丟空，額外要求該 task 的完整電話及精確 Asset 同時相符。即時讀真實 task 後，才可用其確實存在的短 Serial 查新工作。候選 10 部／80 個工作上限不變，API 故障仍停止。
- 只有產品／醫院單格讀取容許恢復固定句式中的唯一引號值；通用 JSON parser 仍嚴格。恢復值標為輔助，不是強共識；一般長 Serial 重複完全相同、另一身份欄及完整電話重複、類型與可靠日期等 live 證據才可佐證一個輔助欄，兩個輔助欄不能互相證明。
- 本機測試與保存回覆的離線重播分開報告；離線重播不是新 OCR／即時 Asana 回測，不能計成完整自動準確率。正式 main、Stage B、雲端索引與 OneDrive 不因這些未提交改動而改變。

本機全套 358 項測試通過（含 42 項新合成／接線／反例測試）。保存的三份 OCR 回覆在已知 Asana 快照上可選對工作與名稱；舊索引部分歷史 GID 沒有最新快照，尚未驗證完整即時候選池，亦未重新呼叫視覺模型。不作 3/3 正式自動通過或未見準確率宣稱。已保留早兩次離線重播的失敗原因與這次結果；須展示 diff 並獲准後，才提交／推送本測試分支及進行真實只讀回測。

### 2026-10-13 已完成的固定版本十五次只讀試驗

- 上述四項 OCR 改良經使用者確認 diff 後，已提交／推送至獨立測試分支 `66fd22a`；不是 main 部署。前述 358 項及離線重播描述是推送前的階段紀錄。
- 同五份已切分開發文件各測三輪，固定程式、來源、索引及原始上傳日期：正確設備／工作／類型／名稱 9/15，待核對 5/15，服務故障 1/15，錯誤自動配對 0。只兩份文件三輪均通過，不把挑出的成功輪次當成全部穩定通過。
- 166 次實際圖片 API 嘗試，主模型 503 共 32 次、超時 4 次；12/15 試驗使用後備。這不是未見驗證，也不是切頁／圈選／上傳全流程；不能用 workflow Success 代替配對成功。Drive 和 OneDrive 寫入皆為零，舊失敗、加密原文及逐次核對結果全保留。

### 2026-10-13 未確認英數院碼分類：僅本機、尚未提交／推送

- 單一英數混合字串是院碼形狀，不因字數達五便算完整院名。先查精確已確認別名；未知碼保留為原始抄錄並標示 `unconfirmed_short_code`，不作醫院搜尋、共識、加分或已知院名衝突，也不套用完整院名模糊校正。
- 院碼與樓層／房間分開；建立地点表或學習別名時，未知混合碼不會冒充完整院名。明確確認的私人別名、現有正式院碼及完整院名仍可使用。不加入客戶特定錯碼修正。
- 本機全套 372 項測試通過（新增 14 項）。包含原文保留、未知／歧義／已確認院碼、院內位置、索引分類、復核接線及仍需停下的反例；沒有放寬電話、Serial、日期、候選唯一性或數量限制。
- 保存的十五次讀數離線檢查顯示一個試驗原先被未知院碼錯誤形成共識、跳過醫院重讀；修正後可進入已有的最多兩次醫院單格復核。其餘已知醫院身分沒有被移除。此次沒有新圖片／Asana 呼叫，不能宣稱難例已通過或把既有 9/15 結果改高。
- 未部署 main、未恢復 Stage B、未重建雲端索引、未再上傳。須展示本次 diff 並獲准後，才提交／推送及進行新的只讀模型試驗。

### 2026-10-16 數字線索保留及錯電話共識重讀：僅本機、尚未提交／推送

- 前述英數院碼修正經確認後已推到獨立分支 `957084b`，不是 main。其固定兩份文件各三輪只讀試驗為正確 3/6、待核對 3/6、錯配 0；沒有穩定三輪皆通過的文件。這是已參與診斷的開發集，不是未見準確率，不能改寫前十五次 9/15 的紀錄。
- 使用者同意四項改良方向。本階段只實作第 1 項「受控保留單格數字線索」及第 2 項「配對失敗時復核錯電話共識」；第 3 項受限候選檢索及第 4 項日期專項補證仍未實作。不得把此同意當成提交、推送、新模型呼叫或部署授權。
- 電話／ACTION DATE 單格若只回唯一數字或允許的固定句式，通過既有格式／日期驗證後可保留在私人 `_auxiliary_candidates`。配對相容欄位仍為空，不能形成強共識或直接提供配對電話／日期；一般多欄 JSON、任意散文、部分電話、多答案及不合法日期仍拒絕。同圖格式重試只產生一筆 reader 結果，不算兩張獨立證據。
- 有重複產品、醫院及 Serial 視覺證據但仍未配對時，即使完整電話已有共識，也可進入原有最多兩次電話單格 reader 復核。已由合理即時工作佐證的電話、候選超限、設備／工作並列及明確身分／訂單／日期衝突不以此重讀解決。舊讀數保留，不用新讀數覆蓋；不是無限重試，底層既有格式／服務重試也不冒充獨立票數。
- 新增 25 項合成／接線／反例測試，全套本機 397 項通過。包含兩次同錯後可復核、只有一次正確仍不足、輔助線索不放行、缺少醫院、日期超窗、候選並列及格式重試票數；不更改短 Serial 的重複完整電話、可靠日期 ±14 天、類型、唯一性及最終 Asana 核對門檻。
- 保存回覆的三項離線核對通過：格式錯誤的正確電話保留為零票輔助；舊版跳過電話單格的個案可安排受限復核；已讀兩次的個案不加第三輪。舊來源／輸入圖片雜湊已核對，日期仍固定原上傳日。缺少的復核回覆不編造，不能據此說三份改好或實單準確率提高；原六次 3/6 結果不變。
- 此階段沒有新模型／Asana 呼叫、雲端讀寫或 OneDrive 操作；未操作 main、Stage B 或雲端索引。私人原文及離線報告只留 ignored `tmp/`。展示 diff 並獲准後，才可提交／推送及安排新的固定版本只讀試驗。

### 2026-10-16 第 3 項修正：私人設備表多條件搜尋，仍僅本機

- 使用者澄清：私人表應先找設備，Asana 再選當次工作。正常設備列以固定 Serial 聚合所有產品寫法、醫院組、詳細地點、歷史電話及聯絡人；搜尋設備時不要求這些歷史線索同屬某一次 PM/CM。舊弱列沒有可靠 Serial，仍只能提供受限歷史 task 線索，不能冒充已確認設備。
- `device_table_search` 提供同一個只讀搜尋函數，容許 Serial 精確／模糊／原文片段與產品、醫院、電話、聯絡人、Asset 等不同條件組合。固定程式先保留原 Serial 優先路徑，找不到時再作有限的原文片段或產品＋醫院搜尋。電話不符、沒有 Asset、日期或工作類型不會直接封死設備查找；產品大類及醫院已確認別名仍沿用既有正規化，未知院碼不升格。
- 格式恢復的電話及明確保留 `?` 的 Serial 原文可作私表搜尋線索，但不修改 OCR 正規欄位或增加票數；原字串不補寫、不刪問號。表中名稱／Serial 與圖片抄錄分開保存在私人 `_device_table_audit`，搜尋結果不等於工作或檔名通過。
- `device_search_agent` 是以既有視覺服務 JSON 回覆驅動的受控函數協定，不依賴供應商原生 tool-calling。僅當 `JOBSHEET_DEVICE_SEARCH_AGENT=1` 且 `JOBSHEET_DRY_RUN=1` 才接入；非只讀模式在任何 OCR 前拒絕。既有讀取及配對仍無法通過時，模型可以換查詢欄位及 Serial 比較方式，不能傳入新造的查詢值、執行其他函數、改表或上傳。
- 每份最多三次查表、四次模型協定回覆，累計最多十個設備候選；底層既有服務重試另記，不算獨立抄錄。回覆只可選已返回候選 ID 或表示不確定；超限、格式不合、表外答案、明確衝突或設備／工作並列都不能藉此放行。模型不能刪除之前已取得的競爭候選；即時程式若選中與模型提議不同的設備，也會停止。
- 最終 `assess_task`／`select_task` 接受條件未放寬：仍用原 OCR 證據與真實 Asana 資料核對 Serial、產品、醫院、當次資料、專案 PM/CM、可靠日期 ±14 天及唯一性。仍限制八十個工作，API 故障不當作查無資料；Order Number 只來自最後 Asana。第 4 項日期專項補證尚未修改。
- 新增 34 項合成／協定／實際接線／反例測試，全套本機 431 項通過。涵蓋跨歷史聯絡資料查設備、醫院別名與樓層、搬院不合併醫院、錯電話共識不抹除設備候選、缺 Serial 仍不直接放行、原文片段／輔助電話零票、共用電話、候選及呼叫上限、表外答案、服務故障、非只讀模式拒絕及模型／程式設備衝突。公開診斷只輸出白名單原因、數量及布林值，私人候選和原文不進日誌。
- 凍結舊索引及六次已保存讀數的離線核對另存 ignored `tmp/`，沒有新模型、Asana 或雲端呼叫。舊表共有 4596 列，其中 489 列是弱資料；一部難例沒有完整 Serial 設備列。新增 context 路徑雖可多找出候選，但該次不是正解，不能當改善成功。舊六次正式結果仍為 3/6 正確，未重新量測全流程準確率。
- 私表完整性是下一個必要驗證項：完整重建須用修正後 parser 從 Asana 重新取得資料，不能把舊弱列靠模型猜成設備。這次未重建／替換雲端索引，未提交／推送，未操作 main／Stage B／OneDrive。須展示 diff 並獲准後，才進行固定版本的真實只讀回測或雲端索引更新。

### 2026-10-16 私人索引完整重建：獨立本機草稿，尚未發佈

- 經使用者授權下一步後，沿用現有 builder 的未封存 PM/CM 專案範圍，重新只讀取得 105 個專案、344 頁、26954 筆工作出現紀錄，去重為 25241 個 task。每個專案都讀至分頁結束，取得數量與開始時的 task count 相同；不是將舊弱列猜成完整設備，也不是增量沿用舊解析結果。多分鐘的 API 讀取不是原子快照，不包含已封存專案。
- 以修正後 parser 完整建立 2024-10-16 至 2026-10-16 的 schema 3／parser 2 草稿：3998 個唯一 Serial 設備列，另 234 個弱身分列，共 4232 列；269 個地點組，其中 197 組可參與配對。弱列仍不能確認設備；有 1 個範圍內 task 無法解析，不冒稱整批設備都已人工核實。
- 已核實的五部設備都各有唯一 Serial 列，並保留正確歷史 task、產品大類、醫院及電話。JSON／manifest 回載、歷史電話／聯絡人／Asset 不覆蓋及 CSV 列數／欄名／字串身分檢查通過。舊表的 141 個 Serial 在新表不再出現，逐項核對其 150 個 task 均因兩年滑動範圍由 2024-09-16 移至 2024-10-16 而排除；原 task 仍存在於本次來源，沒有未解釋的漏收。新增 32 個 Serial，不能將總列數變化當作準確率。
- 首次草稿發現前置工作註解被當成地點，且帶入另一工作的訂單號。現讓索引地點解析與既有醫院解析一致，去掉前置括號註解，保留真正醫院、樓層及房間；不更改 Asana 原文。首次草稿保留，另建修正草稿。新增三個通用反例／保留測試，全套本機 434 項通過；不是實單準確率。
- 兩份開發文件的六次既有 OCR 讀數離線重播：正確設備候選 5/6、正確工作 3/6、待核對 3/6、錯選工作 0。這不是六份獨立文件，也沒有新圖片呼叫或即時 typeahead 搜尋。按舊答案檔名只有 2/6 相符；其中一個正確 task 現已在 Asana 新增訂單號，應依現行規則用 SR# 命名。另取真實 task 獨立覆核此變更，不偷偷改舊答案，也不把快照重播當正式最新命名驗收。
- 額外掃描發現四筆非測試 task 標題含多個不同的八位訂單號，其中一筆另取真實 task 核對；現行命名函數取第一個，可能誤用註解中的其他工作訂單。已保留私人證據，未在此次索引修正中更改全域命名規則；正式上傳前須另行修正或攔截，不能因 task GID 已核實便忽略此風險。
- 所有新來源、索引、CSV、失敗草稿及核對報告均只存 ignored 私人 `tmp/`。Google Drive 正式索引／manifest／控制資料夾的前後中繼資料一致，分享仍關閉；本輪雲端寫入與模型呼叫為零，沒有存取 OneDrive，未操作 main、Stage B，亦未提交／推送。下一次真圖片只讀試驗須先展示完整 diff，取得提交／推送測試分支及試驗授權；不能把本機重建當成正式索引已更新。第 4 項日期專項補證仍未實作。

### 2026-10-23 分項續作：測試環境已恢復，仍只讀

- 在 ignored `tmp/readonly-repair-20261023/.venv` 建立獨立 Python 3.12 環境，依專案鎖定的正式及加密診斷依賴安裝；不修改系統 Python 或正式 workflow 的供應商設定。
- 先不修改命名／日期規則，完整重現 434 項本機測試通過及 `pip check` 成功。保留本次獨立日誌；它不是新 OCR／即時 Asana 回測，不改寫先前失敗分母。
- 新增 `scripts/test_local.ps1`，可用 `-Setup` 在 checkout 內建立隔離環境，再以同一解譯器驗證依賴及執行測試，避免誤用無依賴的系統 Python。
- 尚未提交／推送，未修改 main、Stage B、正式索引或 OneDrive；接續分別處理多訂單號命名及日期補證。

### 2026-10-23 第二項：多訂單號命名本機修正

- 不再取標題中第一個八位號碼。完整、平衡的前置括號註解另行排除；只有主標題剩下唯一訂單號才可命名。註解中唯一訂單號、括號未閉合或主標題多個不同訂單號，皆標為歧義，不退回完整標題或中性名稱繞過。
- 明確標為電話、Asset、HAWO／WO 的數字不是訂單號；同一訂單號重複出現不構成多訂單。沒有訂單號的普通標題仍沿用完整 Asana 標題。
- 即時工作選擇保留有歧義的競爭工作，不先刪掉它讓別的工作勝出；最終命名及上傳入口另有攔截。公開日誌只記 `ambiguous_order_numbers`，不印原標題或號碼。
- 新增 11 項純合成測試，全套 445 項本機通過；這不是新圖片回測。尚未提交／推送、上線或操作 OneDrive。

### 2026-10-23 第三項及只讀回測接線：尚未提交／推送

- 新增 opt-in `JOBSHEET_DATE_CORROBORATION=1`，必須同時為 `JOBSHEET_DRY_RUN=1`，否則在任何 OCR 前拒絕。首頁／既有 PM checklist 日期仍不足時，且即時工作已通過設備身分核對，才讀固定簽署日期；不給模型 Asana 候選日期，不改寫 ACTION DATE 或原 OCR 讀數。
- 一個簽署日期格的 5x／6x 是一致性檢查，不是兩個物理來源。至少兩個不同日期格都各自讀兩次一致、而且同一天，才可作獨立補證；首頁先讀，PM 需要時才讀四頁文件最後一頁的兩個簽署日期，排除工具校準日期列。CM 最多四次 reader、PM 最多八次，既有底層服務／格式重試另外記錄。不一致、來源不合、日期超過正式工作 ±14 天或工作並列仍停下；缺電話、錯醫院及 Serial 超過既有門檻均不藉日期放寬。
- 修復候選模型圖片卡要求不存在的獨立 Asset 格的接線錯誤；Asset 線索仍來自既有部門／房間資料及私表，不捏造新的印刷欄位。新增使用真實 renderer 的合成 PDF 測試，避免只 mock 裁圖而漏掉錯誤。
- 本分支的二十份判分器原來仍是舊版，已合入主工作區之前寫好的嚴格版本並保留原工作區不變：必須同時核對設備、task GID、命名及專案類型；圈選由圖片辨認，答案只作事後評分。補上實際私表候選清單量測、逐份上傳日期隔離、服務故障繼續、缺頁零呼叫攔截、歧義命名不崩潰、匿名 checkpoint，以及真正選中工作／檔名／原始 OCR 的加密保存。不能把設備相同而月份錯誤算通過。
- `readonly_test_index` 只允許獨立測試分支及 dry-run，在 runner 的 ignored 私人目錄重建臨時索引；不沿用舊 parser、不呼叫 Drive 發佈或 OneDrive。對外 artifact 只包含 RSA recipient 加密的索引／manifest 及 OCR 證據，私鑰留本機，正式雲端索引不變。
- 難例 encrypted workflow 及二十份 workflow 接上上述開關；二十份只讀 workflow 限測試分支，下載固定 B01–B20，不遞迴收取其他控制檔。使用已獨立覆核的 `manifest-reviewed-20260925.json`，SHA-256 固定為 `81db79be…a4355e`；本輪只讀下載核對雲端與本機完整 SHA 一致，沒有修改答案或上傳日期。不是誤用舊 `manifest.json`。
- 按 PDF 技能檢查真正 renderer 產生的日期欄位卡，包含不同批次、較高的手寫字；末頁日期框上緣保留筆畫，仍排除校準日期。不是只檢查全頁縮圖。新增反例確保上升筆畫不被裁掉。
- 目前完整本機測試 513 項通過、`pip check` 通過；雲端 Code Check 亦改為安裝既有加密診斷依賴。這些不是新圖片回測結果。Google Drive 正式索引、manifest 和兩份難例的大小／修改時間／父資料夾及私有分享狀態與保存紀錄一致。
- 本輪沒有新圖片呼叫、Asana 索引重建、雲端測試 dispatch、commit、push、main／Stage B 修改或 OneDrive 存取。須先展示完整累積 diff 並獲准提交／推送獨立測試分支，才凍結版本跑新的難例；難例未通過不得啟動全二十份。舊六次 3/6 與舊二十份失敗紀錄不改寫。

### 2026-10-23 候選池及日期讀取續修：僅獨立測試分支

- 上一固定版本 `2422107` 的二十份只讀結果保留：16 份完整且答案已核實的文件中，9 正確、3 待核對、4 圖片服務錯誤、0 錯配；另 3 份缺頁及 1 份答案未核實分開報告。不能用之前兩份難例的 6/6 取代這個結果，也不能將四次服務故障都歸咎筆跡。
- 候選歷史資源限制改為每個 Serial 最多 80 筆、最多 10 部設備、總讀取最多 800 筆；不再因多部設備的真實歷史合計超過 80 而整池拒絕。所有競爭設備仍須讀取，不按舊日期、舊類型或完成狀態刪去競爭者。弱身分共用原來的 80 筆限制；同一真實 Serial 合流後超過 80 筆仍停下。公開 `live_task_count` 表示總讀取數，上限相應為 800，不是將單部設備限制放大。
- 首頁客戶簽署日期裁切向上及右側保留完整筆畫，主要從水平方向避開印章。以六份既有原件逐一檢查 5x/6x 共十二張真實欄位卡，仍有原件印章遮疊的情況；裁切修正不等於保證每格 OCR 正確。
- 僅簽署日期 reader 兼容唯一完整日期的裸字串或固定單日期句式；最多一次格式重讀。多答案、不確定字句、非法日期、額外欄位仍拒絕，一般 OCR／候選模型的 JSON 限制不變。單格兩次縮放仍只是一個物理來源，兩個不同來源一致、正式日期 ±14 天、設備及工作核對等原有接受門檻未放寬。
- 加密私有 trace 保存即時 typeahead 結果以核對超限來源，不輸出公開日誌、不增加證據票。回測呼叫數以實際請求 trace 計數，包含核心計數器重設前的圈選呼叫；不將重試當新證據。
- 本次使用者僅授權提交／推送 `codex/parser-ingress-fix-20261013`：先凍結版本跑 B02/B03/B06/B07/B09/B10/B15 七份難例，全部通過才跑二十份。答案、上傳日期及原件不修改；NVIDIA 主／後備供應商不更換。main、停用中的 Stage B、正式 Drive 索引及 OneDrive 不操作。模型結果須另行如實記錄，不以合成測試通過代替。

### 2026-10-23 傳輸恢復及身分單格複核：僅獨立只讀測試

- 保留 `30c9b4f` 七份難例結果：B02/B03/B15 正確，B06/B07/B09/B10 服務中斷，0 錯配；72 次實際請求中有 18 次 503、10 次逾時。B02 真實候選池為 112 筆，B15 兩個簽署日期格各雙讀一致；B07/B09 未走到原來需驗證的補讀階段。B10 中斷前 Serial 距離仍為二、沒有吻合完整電話，不能宣稱單靠服務恢復就必過。未執行二十份，舊分母不變。
- 新增 opt-in `JOBSHEET_OCR_TRANSPORT_RECOVERY=1`，只允許 `JOBSHEET_DRY_RUN=1` 及既有 NVIDIA 供應商；僅二十份只讀 workflow 啟用，不改正式流程或模型。SDK 仍零隱藏重試、單次請求最多 45 秒；每個呼叫最多六個實際請求、180 秒期限，每份文件最多十二次失敗。所有重試只產生原 reader 的一份結果，不增加共識票。
- 主／既有後備暫時失敗後採短退避與十五秒冷卻，允許一次半開恢復探測，不永久封鎖整份文件；同圖、同提示及舊成功讀數保留。尊重 `Retry-After`，超過三十秒便安全停下而不換模型繞過等待；401/403/設定錯誤不重試。不聲稱程式可以修復外部服務本身。
- 符合產品／醫院／類型且 Serial 在既有距離範圍內的即時候選，都不支持目前重複 Serial 時，可安排原有最多兩次 Serial 單格複核；精確 Serial 已有支持、設備／工作並列及明確衝突不重讀繞過。仍保留舊候選，不提供表中 Serial 或電話給模型，也不放寬接受條件。
- 電話單格保留完整電話欄，不因去掉印刷標籤而切走靠左書寫的數字；Serial／電話提示只引導逐字／完整分組抄錄，不指定字元、號碼或案例答案。已檢查三份原件的真實 5x/6x 欄位卡，另有靠左筆畫、鄰接聯絡人排除等合成反例。
- 最後的查表模型故障不再令此前的 OCR／即時候選診斷全部消失。成功讀數、原文及服務未完成標記保留；未選出工作且服務未完成仍判 `SERVICE_ERROR`，不把它改成通過或普通紙本不足。新退避／恢復事件及真正 timeout 只記加密私有 trace。
- 本輪仍先凍結版本跑 B02/B03/B06/B07/B09/B10/B15，七份全通過才啟動全二十份。原件、私人答案、未知上傳日期、正式索引、main、Stage B 及 OneDrive 不改；不使用個案答案覆寫，不以反覆重跑挑出的成功替換完整結果。

### 2026-10-23 後續 Serial 格式弱提示：仍只讀

- `b70ac27` 的同七份新試驗完整保留：4 正確、2 待核對、1 服務中斷、0 錯配，未跑二十份。89 次請求有 17 次 503、1 次 429、3 次逾時；有兩次半開恢復探測。比前輪少服務中斷不是未見準確率或服務已穩定的保證。
- B10 Serial 從兩字誤差變成一字（首字母被讀為數字），電話仍不能支持正確工作；B07 的已讀日期其實符合正確工作，但該工作未通過身分支持，不能把回傳的日期衝突碼直接解作日期 OCR 錯誤。B15 同時有年份讀數衝突及服務失敗，不能只歸咎網路。
- 新增 opt-in `JOBSHEET_SERIAL_SHAPE_REFERENCE=1`，只限 dry-run。從已確認產品寫法的設備列，按目前產品大類統計 L（字母）／D（數字）位置；至少三個不同 Serial 才提供、最多四種模式。重複工作或同設備重複列不加樣本，弱設備和未確認產品不納入；不傳完整 Serial、前綴字母、電話、客戶或 task。
- 只替換原有 Serial 6x 單格複核，不增加 reader 數；5x 仍無提示。清楚筆畫及少見格式優先，不根據模式補字或修正字串。模型的純模式輔助猜測另存私人 audit，不進 Serial 候選、搜尋或共識；只有實際抄錄經原格式驗證後參與原有接受閘。不放寬電話、日期、唯一性或矛盾攔截。
- 仍固定新版本重新測同七份，全部通過才跑二十份。不得將兩個版本挑出的成功個案拼成通過；main、Stage B、正式 Drive 索引和 OneDrive 維持不變。

### 2026-10-23 電話補讀接線及產品／Serial 同看：仍只讀

- `a6427fa` 七份難例完整保留：B02/B03/B06/B09/B15 正確，B07/B10 待核對，0 錯配，沒有服務未完成；102 次請求仍有 26 次 503、7 次逾時。這不是供應商已穩定，也不是全二十份結果；未啟動二十份。格式弱提示未救回 B10，不能當作已驗證改善。
- 真實 trace 顯示 B10 在多個不同 Serial 讀數下，原本要求「Serial 先有共識」的條件跳過了重複但無 Asana 支持的電話單格讀取。Serial 未共識的新補讀路徑仍需重複產品／醫院及可見 Serial 線索，而且同類即時工作支持產品、醫院、Serial 距離不超過三字，才可安排原有 5x／6x 兩次電話補讀；缺身分線索、明確衝突、已確認電話或候選並列不藉此加讀。這只改排程，不改 `assess_task`／`select_task` 的接受規則，也不要求補讀結果必須與表中電話一致。
- 原有 6x Serial 弱提示 reader 同時顯示原件產品格與 Serial 格，不提供任何完整 Serial 或前綴字母；5x 仍單獨抄 Serial。已逐一檢查兩份原件實際產生的聯合卡，兩格完整分開。模式輔助猜測仍另存零票，不增加 reader、放寬日期或覆蓋舊讀數。
- 新增七項無網路測試，驗證 Serial 未共識可安排受限電話補讀、錯類型／距離超限／缺資料不加讀、舊讀數保留、單次結果仍是一票，以及錯電話／日期衝突不放行。完整本機測試 563 項通過；實單結果須固定新版本另測，不沿用上輪五份成功。
- 仍僅推獨立測試分支，重測同七份且全部通過才跑二十份；main、Stage B、正式 Drive 索引和 OneDrive 不操作。原件、答案及未知原上傳日期保留。

### 2026-10-23 保留最後失敗額度給已成功模型：仍只讀

- `d926465` 的同七份完整保留：B02/B03/B06/B09 正確，B07/B10 待核對，B15 服務中斷，0 錯配；88 次請求有 25 次 503，沒有記錄逾時。B10 真的執行兩次電話補讀、共四次請求，但讀數仍不吻合正確工作或該設備歷史，Serial 亦沒有正確共識；不能將排程修正當成實單已救回。未啟動二十份。
- B15 的十二次失敗全來自主模型，前面同時已有十次主模型及四次既有後備成功回覆；累積額度在讀 checklist／日期補證前用完。這不是證明原件錯誤，也不能把前一版本的 B15 成功挪來補本輪分數。
- 仍限原有十二次傳輸失敗、六次請求／呼叫、180 秒呼叫期限及 45 秒單次 timeout；只剩最後一次失敗額度時，優先選擇本文件中曾成功、其後沒有傳輸錯誤且不在 cooldown 的既有允許模型。若它再失敗便耗盡第十二次額度，全份停止，不增加失敗額度、不重設累計數，也不在沒有成功模型時憑空授權新請求。成功回覆仍經原格式／證據／Asana 閘門，不等於辨認正確。
- 不啟用原先禁止的後備，不繞過伺服器 Retry-After 或 cooldown，不更換供應商／模型；文件之間清除可達模型狀態。新增六項合成測試，本機完整 569 項通過；須固定新版本再測同七份，不能用合成測試宣稱服務或 B10 已穩定。
- 僅獨立測試分支、維持只讀；main、Stage B、正式索引、原件／答案和 OneDrive 不改。七份未全部通過前不跑二十份。

### 2026-10-23 PM header 分格及短期可達模型優先：仍只讀

- 保留 `5e45e83` 七份完整結果：B02/B03/B06/B09 正確、B07/B10 待核對、B15 服務未完成，0 錯配；107 次實際請求有 32 次 503、9 次逾時，未啟動二十份。B07 正確工作已在候選池，日期亦吻合，但當次電話不同且精確 Serial 未共識；不能說是日期太遠。B10 已真的補讀電話，仍沒有吻合電話或精確 Serial 共識。
- 新增 opt-in `JOBSHEET_PM_FIELD_CARDS=1`，必須 dry-run，僅二十份只讀 workflow 啟用。將原有兩次 PM checklist header 橫條讀取換成同頁五個有標籤的分格，不增加 reader、不給候選答案；保留產品、醫院、機身 Serial、Date、Asset 各自原文及 page 2 來源。Serial 高筆畫、尾字及橫線下 Asset 更正保留；不讀末頁工具 Serial。六份原件全頁及十二張最終 5x/6x 卡已逐一視覺檢查，首次裁切草稿亦保留，不等於 OCR 已讀對。
- 在既有只讀傳輸恢復中，主模型剛失敗、既有後備曾成功且未在 cooldown 時，六十秒內優先用該後備；期限從主模型失敗時計，後備成功不續期，到期仍可正常探測主模型。原供應商／模型、每份十二次失敗、每呼叫六請求／180 秒、單次 45 秒及 Retry-After 限制不變，不啟用原本禁止的後備、不繞過長等待、不增加證據票。
- 新增十四項合成測試，完整本機 583 項、語法及依賴檢查通過。涵蓋真 renderer 分格、缺頁／非只讀拒絕、來源／票數、未知 Serial、矛盾仍停下，以及短期後備優先、期限不續期、失敗上限與跨文件重置。這不是實單通過或服務已修復的證明。
- 僅提交／推送獨立測試分支；固定新版重跑同七份，全通過才跑二十份。接受門檻、±14 天日期窗、原件、答案及未知上傳日期不改；main、Stage B、正式 Drive 索引與 OneDrive 不操作。

### 2026-10-23 保留已可通過的 Serial 假設及提早後備：仍只讀

- `8c66baf` 七份實測保留：B02/B06/B07/B09/B10 正確、B03 日期補證衝突待核對、B15 服務未完成，0 錯配；110 次實際請求有 29 次 503、6 次逾時。B07/B10 的新 PM 分格確實各讀兩次，B10 取得兩次精確 Serial；B07 用既有模糊 Serial／精確 Asset 及正式日期規則通過。不能把這輪與其他版本拼成七份全過，未啟動二十份。
- 上輪原文與即時 Asana 的獨立核對確認 B10 的原件電話與目前 task 電話確實不同，不是可以要求 OCR 照抄 Asana 修正的號碼；這項人工觀察沒有提供給模型或判分器。Serial 可讀穩時仍按既有規則核對，不更改原件或 Asana。
- 發現 Serial 假設判定的非單調問題：一個精確但未重複的讀數會使程式只走 distance=0 分支，反而抹掉另一個已符合原有規則的模糊 Serial＋同次工作精確 Asset 路徑。現保留所有原讀數，只有本來存在 1–3 字誤差的完整長 Serial、重複產品／醫院及當次精確 Asset 均支持時，才讓原有模糊路徑繼續；仍按實際支持的 80／60／40 分，不借單次精確讀數取得 100 分。沒有模糊讀數、Asset 缺失／模糊、短 Serial、明確衝突、日期超窗或並列皆不藉此放行。
- 主模型的短期優先期結束後，若再次探測失敗，而允許的既有後備在本文件曾成功、之後未失敗且不在 cooldown，遵守相同退避／Retry-After 後可直接轉後備，不先耗第二次主模型失敗。原十二次失敗、六次請求／180 秒、單次 45 秒及長等待停止限制不變；無可達後備、後備被禁止或冷卻時仍按原邏輯處理。
- 新增十項合成／反例測試，完整本機 593 項、語法及依賴檢查通過；固定新版重跑同七份，全過才跑二十份。不刪舊失敗、不修改答案、原上傳日期或來源，不改供應商／模型、日期窗及唯一性要求；只推獨立測試分支，不部署、不操作 OneDrive 或正式索引。

## 14. 官方參考

- 現用 NVIDIA 模型：<https://build.nvidia.com/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning>
- 舊 NVIDIA 模型：<https://build.nvidia.com/nvidia/llama-3.1-nemotron-nano-vl-8b-v1>
- Asana 搜尋：<https://developers.asana.com/reference/typeaheadforworkspace>
- Asana 限流：<https://developers.asana.com/docs/rate-limits>
- rclone 列檔：<https://rclone.org/commands/rclone_lsf/>
- rclone 單檔刪除：<https://rclone.org/commands/rclone_deletefile/>
- Apps Script 防同時執行：<https://developers.google.com/apps-script/reference/lock>
- Apps Script 寄信：<https://developers.google.com/apps-script/reference/mail/mail-app>
