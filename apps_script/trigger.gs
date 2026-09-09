/**
 * Jobsheet Auto Trigger — Google Apps Script
 *
 * 部署方式：
 * 1. 開 https://script.google.com → 新建專案
 * 2. 把這份 code 整段貼到 Code.gs
 * 3. 左側 Project Settings → Script properties 加入：
 *    - GITHUB_PAT: 你的 fine-grained PAT
 *      (Contents: Read and write；Actions: Read-only；只授權本 repo)
 *    - ALERT_EMAIL: 收故障通知的電郵（建議；不填仍有 GitHub Issue 告警）
 * 4. 執行一次 checkDriveAndTrigger() 完成 OAuth 授權
 * 5. 設定 Triggers → Add Trigger:
 *    函式: checkDriveAndTrigger | 時間: Every 5 minutes
 */

// ── 設定區（已填入你的資料）──────────────────────────
const FOLDER_ID   = '1ls3TQXyr0GTxDOO3MVQgR3QFDPXFM6gn'; // From_BrotherDevice
const GITHUB_OWNER = 'qstuer';
const GITHUB_REPO  = 'jobsheet-automation';
const LAST_TRIGGERED_PROPERTY = 'LAST_TRIGGERED_MS';
const COOLDOWN_MS = 20 * 60 * 1000; // GitHub 接受 dispatch 後，20 分鐘內不重複觸發
const FAILURE_ALERT_THRESHOLD = 3;
const MONITORED_WORKFLOWS = [
  { key: 'STAGE_A', name: 'Stage A（切頁）', file: 'jobsheet-split.yml' },
  { key: 'STAGE_B', name: 'Stage B（辨認及上傳）', file: 'jobsheet-process.yml' },
];
// ────────────────────────────────────────────────────

function checkDriveAndTrigger() {
  // 時間觸發器、手動執行可能重疊；鎖住整段「檢查 → 等待 → dispatch」，
  // 避免兩個 instance 同時讀到尚未寫入的冷卻時間而各送一次 dispatch。
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    Logger.log('⏳ 已有另一個 checkDriveAndTrigger 執行中，跳過本輪');
    return;
  }

  try {
    const props = PropertiesService.getScriptProperties();
    const pat = props.getProperty('GITHUB_PAT');
    if (!pat) {
      Logger.log('❌ Script Property GITHUB_PAT 沒設');
      return;
    }

    // 這個檢查在 Google 端執行。即使 GitHub Actions 本身不能啟動，
    // 只要 GitHub API 仍可讀取，仍能從 Gmail 主動寄出連敗通知。
    checkPipelineHealth(pat, props);

    // 冷卻期間上傳的新 PDF 不會遺失：檔案留在原資料夾，冷卻結束後
    // 下一次 dispatch 會讓 Stage A 一併處理。這裡不另開第二條 workflow。
    const lastTriggeredValue = props.getProperty(LAST_TRIGGERED_PROPERTY);
    if (lastTriggeredValue) {
      const lastTriggeredMs = Number(lastTriggeredValue);
      const elapsedMs = Date.now() - lastTriggeredMs;
      if (Number.isFinite(lastTriggeredMs) && elapsedMs >= 0 && elapsedMs < COOLDOWN_MS) {
        const remaining = Math.ceil((COOLDOWN_MS - elapsedMs) / 60000);
        Logger.log(`⏳ 冷卻中，還剩約 ${remaining} 分鐘，跳過`);
        return;
      }

      // 防止手動改壞或系統時鐘倒退造成永久冷卻。
      if (!Number.isFinite(lastTriggeredMs) || elapsedMs < 0) {
        Logger.log('⚠️ 冷卻時間無效，已清除並繼續檢查');
        props.deleteProperty(LAST_TRIGGERED_PROPERTY);
      }
    }

    const folder = DriveApp.getFolderById(FOLDER_ID);
    const files = folder.getFiles();

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
      // 來源已清空代表上一批處理完；下批 PDF 可立即開始新的冷卻週期。
      props.deleteProperty(LAST_TRIGGERED_PROPERTY);
      return;
    }

    // 等 60 秒確認掃描器上傳完整（避免抓到一半的 PDF）。等待期間新上傳的
    // PDF 也會由同一次 Stage A 掃描處理，不需要逐檔 dispatch。
    Logger.log(`偵測到 ${pdfCount}+ 個 PDF，等待 60 秒確認上傳完整...`);
    Utilities.sleep(60000);
    Logger.log('等待完成，觸發 GitHub workflow');

    // 只有 GitHub 接受 dispatch（HTTP 204）才開始冷卻；API 呼叫失敗時，
    // 保留下一個 5 分鐘輪詢重試的機會。
    if (triggerGitHubWorkflow(pat)) {
      props.setProperty(LAST_TRIGGERED_PROPERTY, String(Date.now()));
    }
  } finally {
    lock.releaseLock();
  }
}

function checkPipelineHealth(pat, props) {
  const email = (props.getProperty('ALERT_EMAIL') || '').trim();
  if (!email) return; // GitHub Issue 告警仍會獨立運作

  let apiFailed = false;
  for (const workflow of MONITORED_WORKFLOWS) {
    const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}`
      + `/actions/workflows/${workflow.file}/runs?status=completed&per_page=10`;
    let response;
    try {
      response = UrlFetchApp.fetch(url, {
        method: 'get',
        headers: {
          'Authorization': `Bearer ${pat}`,
          'Accept': 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
        },
        muteHttpExceptions: true,
      });
    } catch (err) {
      // 狀態檢查失敗不能阻止下面正常檢查 Drive 和 dispatch。
      apiFailed = true;
      Logger.log(`⚠️ 無法連線讀取 ${workflow.name} 狀態：${err}`);
      continue;
    }

    if (response.getResponseCode() !== 200) {
      apiFailed = true;
      Logger.log(`⚠️ 無法讀取 ${workflow.name} 狀態：HTTP ${response.getResponseCode()}`);
      continue;
    }

    let runs;
    try {
      runs = JSON.parse(response.getContentText()).workflow_runs || [];
    } catch (err) {
      apiFailed = true;
      Logger.log(`⚠️ ${workflow.name} 狀態格式無法讀取：${err}`);
      continue;
    }
    if (runs.length === 0) continue;

    const failureStates = new Set([
      'failure', 'timed_out', 'startup_failure', 'action_required',
    ]);
    let streak = 0;
    for (const run of runs) {
      if (!failureStates.has(run.conclusion)) break;
      streak++;
    }

    const alertProperty = `FAILURE_ALERT_ACTIVE_${workflow.key}`;
    if (runs[0].conclusion === 'success') {
      props.deleteProperty(alertProperty);
    } else if (streak >= FAILURE_ALERT_THRESHOLD && !props.getProperty(alertProperty)) {
      const latest = runs[0];
      const sent = sendPipelineEmail(
        email,
        `Jobsheet 系統告警：${workflow.name} 已連續失敗 ${streak} 次`,
        `${workflow.name} 已連續失敗 ${streak} 次。\n\n`
          + `最近一次：${latest.html_url}\n`
          + '掃描檔會留在 Google Drive，不會因這類系統故障搬到人工切頁區。\n'
          + '請先檢查 GitHub Actions；修好後手動再跑一次即可。'
      );
      if (sent) props.setProperty(alertProperty, String(latest.id));
    }
  }

  const apiFailureCountProperty = 'GITHUB_STATUS_CHECK_FAILURE_COUNT';
  const apiAlertProperty = 'GITHUB_STATUS_ALERT_ACTIVE';
  if (!apiFailed) {
    props.deleteProperty(apiFailureCountProperty);
    props.deleteProperty(apiAlertProperty);
    return;
  }

  const count = Number(props.getProperty(apiFailureCountProperty) || '0') + 1;
  props.setProperty(apiFailureCountProperty, String(count));
  if (count >= FAILURE_ALERT_THRESHOLD && !props.getProperty(apiAlertProperty)) {
    const sent = sendPipelineEmail(
      email,
      'Jobsheet 系統告警：無法讀取 GitHub 處理狀態',
      `Google 端已連續 ${count} 次無法讀取 GitHub Actions 狀態。\n\n`
        + '這可能是 GitHub 服務、帳戶狀態或存取權限問題。'
    );
    if (sent) props.setProperty(apiAlertProperty, String(Date.now()));
  }
}

function sendPipelineEmail(to, subject, body) {
  try {
    MailApp.sendEmail(to, subject, body);
    Logger.log(`📧 已寄出告警：${subject}`);
    return true;
  } catch (err) {
    // 告警寄送失敗不能阻止正常 dispatch；記錄後下輪再試。
    Logger.log(`❌ 告警電郵寄送失敗：${err}`);
    return false;
  }
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
    return true;
  } else {
    Logger.log(`❌ 觸發失敗 ${code}: ${response.getContentText()}`);
    return false;
  }
}
