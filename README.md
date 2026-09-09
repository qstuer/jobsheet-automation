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
| `_SPLIT_FAILED/` | 單據內容或頁數真的無法安全判斷 | 人工檢查原 PDF |
| OneDrive 的 `[待核對]...pdf` | 已送達，但無法肯定配對名稱 | 人工改名 |

在已設定好 rclone 的電腦，可執行這個不會移動檔案的檢查：

```powershell
python -m src.healthcheck
```

## 系統如何保護原檔

- GitHub、網路、雲端硬碟、辨認服務或 Asana 故障時，原檔會留在目前位置等待重跑。
- 只有單據本身的 CM/PM 或頁數無法安全判斷，才會進 `_SPLIT_FAILED/`。
- 上傳 OneDrive 後若刪除暫存檔失敗，下一次會認出相同內容，不再製造重複檔。
- 同一階段連續失敗 3 次會建立 GitHub Issue；Google Apps Script 另可寄電郵。

完整說明及修復步驟在 [CLAUDE.md](CLAUDE.md)。人或 AI 接手都應先讀該檔。

> 本專案不需要、也不應由自動化程式處理任何 GitHub 帳單、付款或訂閱設定。
