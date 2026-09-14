# Jobsheet 自動化歸檔

這套系統把 Brother 掃描器產生的 Jobsheet PDF，自動拆成每張工作單、讀取資料、在 Asana 找到對應工作，最後存入 OneDrive。

正常情況下，你只需要掃描：

```text
Brother 掃描器
  → Google Drive 暫存
  → 切成一張張 Jobsheet
  → 讀取 CM/PM、醫院、型號、機身編號、訂單號
  → 到 Asana 找相符工作
  → 以正確名稱存入 OneDrive/JOBSHEETS
```

## 檔案卡住時先看這裡

| 位置 | 白話意思 | 要做甚麼 |
|---|---|---|
| `From_BrotherDevice/` | 剛掃描，還沒切頁 | 手動啟動 GitHub 的 `Stage A` |
| `_SPLIT/` | 已切頁，還沒辨認或上傳 | 手動啟動 `Stage B` |
| `_SPLIT_FAILED/` | 找不到可信工作單邊界 | 人工檢查原 PDF |
| `_INCOMPLETE/` | PM 不足工作單加三張 checklist，或疑似重複頁 | 重新掃描，不會上傳 OneDrive |
| `_PENDING/` | 頁面完整，但 Asana 名稱證據不足 | 人工核對或用單檔模式重試 |

在已設定好 rclone 的電腦，可執行這個不會移動檔案的檢查：

```powershell
python -m src.healthcheck
```

## 系統如何保護原檔

- GitHub、網路、雲端硬碟、辨認服務或 Asana 故障時，原檔會留在目前位置等待重跑。
- 只有單據本身的 CM/PM 或頁數無法安全判斷，才會進 `_SPLIT_FAILED/`。
- PM 成品不足四張內容頁會停止，不會以缺頁檔冒充完整工作單。
- 上傳 OneDrive 前會比較實際頁面；重新編碼的同一份不再上傳，內容不同的重掃則用 `_重掃_日期時間` 保留第二版本。
- 同一階段連續失敗 3 次會建立 GitHub Issue；Google Apps Script 另可寄電郵。

正式處理前可手動執行 `Jobsheet Safe Dry Run`，指定入口的一份 PDF。它會切頁、辨認及查 Asana，但不移動檔案，也不寫入 OneDrive。

完整說明及修復步驟在 [CLAUDE.md](CLAUDE.md)。人或 AI 接手都應先讀該檔。

> 本專案不需要、也不應由自動化程式處理任何 GitHub 帳單、付款或訂閱設定。
