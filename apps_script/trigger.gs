/**
 * Jobsheet Auto Trigger — Google Apps Script
 *
 * 部署方式：
 * 1. 開 https://script.google.com → 新建專案
 * 2. 把這份 code 整段貼到 Code.gs
 * 3. 左側 Project Settings → Script properties 加入：
 *    - GITHUB_PAT: 你的 fine-grained PAT (Actions: Read & Write)
 * 4. 執行一次 checkDriveAndTrigger() 完成 OAuth 授權
 * 5. 設定 Triggers → Add Trigger:
 *    函式: checkDriveAndTrigger | 時間: Every 5 minutes
 */

// ── 設定區（已填入你的資料）──────────────────────────
const FOLDER_ID   = '1ls3TQXyr0GTxDOO3MVQgR3QFDPXFM6gn'; // From_BrotherDevice
const GITHUB_OWNER = 'qstuer';
const GITHUB_REPO  = 'jobsheet-automation';
// ────────────────────────────────────────────────────

function checkDriveAndTrigger() {
  const pat = PropertiesService.getScriptProperties().getProperty('GITHUB_PAT');
  if (!pat) {
    Logger.log('❌ Script Property GITHUB_PAT 沒設');
    return;
  }

  const folder = DriveApp.getFolderById(FOLDER_ID);
  const files = folder.getFiles();

  // 等 60 秒確認掃描器上傳完整（避免抓到一半的 PDF）
  let pdfCount = 0;
  while (files.hasNext()) {
    const f = files.next();
    if (f.getName().toLowerCase().endsWith('.pdf')) {
      pdfCount++;
    }
    if (pdfCount >= 1) break;
  }

  if (pdfCount === 0) {
    Logger.log('沒有新 PDF，跳過');
    return;
  }

  Logger.log(`偵測到 ${pdfCount}+ 個 PDF，等待 60 秒確認上傳完整...`);
  Utilities.sleep(60000);
  Logger.log('等待完成，觸發 GitHub workflow');
  triggerGitHubWorkflow(pat);
}

function triggerGitHubWorkflow(pat) {
  const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/dispatches`;
  const options = {
    method: 'post',
    headers: {
      'Authorization': `Bearer ${pat}`,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({
      event_type: 'jobsheet-uploaded',
      client_payload: { triggered_at: new Date().toISOString() },
    }),
    muteHttpExceptions: true,
  };

  const response = UrlFetchApp.fetch(url, options);
  const code = response.getResponseCode();
  if (code === 204) {
    Logger.log('✅ GitHub workflow 已觸發');
  } else {
    Logger.log(`❌ 觸發失敗 ${code}: ${response.getContentText()}`);
  }
}
