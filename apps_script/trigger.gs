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
const LAST_QUEUE_FINGERPRINT_PROPERTY = 'LAST_QUEUE_FINGERPRINT';
const DISPATCH_ATTEMPT_PROPERTY = 'SAME_QUEUE_DISPATCH_ATTEMPTS';
const BASE_COOLDOWN_MS = 20 * 60 * 1000;
const MAX_COOLDOWN_MS = 6 * 60 * 60 * 1000;
const SPLIT_FOLDER_NAME = '_SPLIT';
const REPORTS_FOLDER_NAME = '_REPORTS';
const REPORTS_SENT_FOLDER_NAME = '_REPORTS_SENT';
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
    processBatchReports(props);

    const folder = DriveApp.getFolderById(FOLDER_ID);
    let snapshot = getQueueSnapshot(folder);
    if (snapshot.items.length === 0) {
      Logger.log('沒有等待處理的 PDF，跳過');
      clearDispatchState(props);
      return;
    }

    const previousFingerprint = props.getProperty(LAST_QUEUE_FINGERPRINT_PROPERTY);
    const isSameQueue = previousFingerprint === snapshot.fingerprint;
    if (!isSameQueue) {
      // 新檔案加入或舊檔已移走，代表 queue 真正改變；不沿用舊批次退避。
      props.setProperty(LAST_QUEUE_FINGERPRINT_PROPERTY, snapshot.fingerprint);
      props.deleteProperty(DISPATCH_ATTEMPT_PROPERTY);
      // 入口有真正的新掃描時可立即接上；只是 Stage A 把同批檔案由 INPUT
      // 轉成 SPLIT，仍保留最近通知時間，避免 Stage B 剛完成便立刻再跑。
      if (snapshot.items.some(item => item.startsWith('INPUT:'))) {
        props.deleteProperty(LAST_TRIGGERED_PROPERTY);
      }
    }

    // 同一批每次仍留在 queue，退避時間倍增；避免 GitHub／模型長時間故障
    // 時每五分鐘重複建立 run。新 queue 則可立即開始。
    const lastTriggeredValue = props.getProperty(LAST_TRIGGERED_PROPERTY);
    if (lastTriggeredValue) {
      const lastTriggeredMs = Number(lastTriggeredValue);
      const elapsedMs = Date.now() - lastTriggeredMs;
      const attempts = Math.max(1, Number(props.getProperty(DISPATCH_ATTEMPT_PROPERTY) || '1'));
      const cooldownMs = Math.min(BASE_COOLDOWN_MS * Math.pow(2, attempts - 1), MAX_COOLDOWN_MS);
      if (Number.isFinite(lastTriggeredMs) && elapsedMs >= 0 && elapsedMs < cooldownMs) {
        const remaining = Math.ceil((cooldownMs - elapsedMs) / 60000);
        Logger.log(`⏳ 同一批第 ${attempts} 次通知後退避中，還剩約 ${remaining} 分鐘`);
        return;
      }

      // 防止手動改壞或系統時鐘倒退造成永久冷卻。
      if (!Number.isFinite(lastTriggeredMs) || elapsedMs < 0) {
        Logger.log('⚠️ 冷卻時間無效，已清除並繼續檢查');
        props.deleteProperty(LAST_TRIGGERED_PROPERTY);
      }
    }

    if (isPipelineBusy(pat)) {
      Logger.log('⏳ GitHub 已有 Jobsheet 工作執行或排隊，不再重複通知');
      return;
    }

    // 等 60 秒確認掃描器上傳完整（避免抓到一半的 PDF）。等待期間新上傳的
    // PDF 也會由同一次 Stage A 掃描處理，不需要逐檔 dispatch。
    Logger.log(`偵測到 ${snapshot.items.length} 個等待檔案，等待 60 秒確認上傳完整...`);
    const fingerprintBeforeWait = snapshot.fingerprint;
    Utilities.sleep(60000);
    snapshot = getQueueSnapshot(folder);
    if (snapshot.items.length === 0) {
      Logger.log('等待期間 queue 已由其他工作清空，毋須觸發');
      clearDispatchState(props);
      return;
    }
    const changedDuringWait = snapshot.fingerprint !== fingerprintBeforeWait;
    props.setProperty(LAST_QUEUE_FINGERPRINT_PROPERTY, snapshot.fingerprint);

    // 等待上傳完成的 60 秒內，可能有人手動啟動或另一個來源先觸發了工作。
    // 再檢查一次，避免剛好在這個時間窗建立重複 run。
    if (isPipelineBusy(pat)) {
      Logger.log('⏳ 等待期間 GitHub 工作已開始，本輪不再重複通知');
      return;
    }
    Logger.log('等待完成，觸發 GitHub workflow');

    // 只有 GitHub 接受 dispatch（HTTP 204）才開始冷卻；API 呼叫失敗時，
    // 保留下一個 5 分鐘輪詢重試的機會。
    if (triggerGitHubWorkflow(pat, snapshot.fingerprint)) {
      props.setProperty(LAST_TRIGGERED_PROPERTY, String(Date.now()));
      // 等待期間有新檔加入就視為新 queue，不沿用舊批次的長退避次數。
      const previousAttempts = isSameQueue && !changedDuringWait
        ? Number(props.getProperty(DISPATCH_ATTEMPT_PROPERTY) || '0')
        : 0;
      props.setProperty(DISPATCH_ATTEMPT_PROPERTY, String(previousAttempts + 1));
    }
  } finally {
    lock.releaseLock();
  }
}

function clearDispatchState(props) {
  props.deleteProperty(LAST_TRIGGERED_PROPERTY);
  props.deleteProperty(LAST_QUEUE_FINGERPRINT_PROPERTY);
  props.deleteProperty(DISPATCH_ATTEMPT_PROPERTY);
}

function findSubfolder(parent, name) {
  const folders = parent.getFoldersByName(name);
  return folders.hasNext() ? folders.next() : null;
}

function collectPdfs(folder, label) {
  if (!folder) return [];
  const files = folder.getFiles();
  const result = [];
  while (files.hasNext()) {
    const file = files.next();
    if (file.getName().toLowerCase().endsWith('.pdf')) {
      result.push(`${label}:${file.getId()}:${file.getLastUpdated().getTime()}`);
    }
  }
  return result;
}

function getQueueSnapshot(rootFolder) {
  const items = collectPdfs(rootFolder, 'INPUT');
  const split = findSubfolder(rootFolder, SPLIT_FOLDER_NAME);
  items.push(...collectPdfs(split, 'SPLIT'));
  items.sort();
  const bytes = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256,
    items.join('|'),
    Utilities.Charset.UTF_8
  );
  const fingerprint = bytes.map(value => {
    const unsigned = value < 0 ? value + 256 : value;
    return (`0${unsigned.toString(16)}`).slice(-2);
  }).join('');
  return { items, fingerprint };
}

function isPipelineBusy(pat) {
  for (const workflow of MONITORED_WORKFLOWS) {
    for (const status of ['in_progress', 'queued']) {
      const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}`
        + `/actions/workflows/${workflow.file}/runs?status=${status}&per_page=1`;
      try {
        const response = UrlFetchApp.fetch(url, {
          method: 'get',
          headers: {
            'Authorization': `Bearer ${pat}`,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
          },
          muteHttpExceptions: true,
        });
        if (response.getResponseCode() !== 200) {
          Logger.log(`⚠️ 無法確認 GitHub 是否忙碌（HTTP ${response.getResponseCode()}），本輪不重複觸發`);
          return true;
        }
        const runs = JSON.parse(response.getContentText()).workflow_runs || [];
        if (runs.length > 0) return true;
      } catch (err) {
        Logger.log(`⚠️ 無法確認 GitHub 是否忙碌：${err}；本輪不重複觸發`);
        return true;
      }
    }
  }
  return false;
}

function processBatchReports(props) {
  const email = (props.getProperty('ALERT_EMAIL') || '').trim();
  const root = DriveApp.getFolderById(FOLDER_ID);
  const reports = findSubfolder(root, REPORTS_FOLDER_NAME);
  if (!reports) return;
  let sent = findSubfolder(reports, REPORTS_SENT_FOLDER_NAME);
  const files = reports.getFiles();
  while (files.hasNext()) {
    const file = files.next();
    if (!file.getName().toLowerCase().endsWith('.json')) continue;
    let report;
    try {
      report = JSON.parse(file.getBlob().getDataAsString('UTF-8'));
    } catch (err) {
      Logger.log(`⚠️ 批次報告無法讀取：${file.getName()}：${err}`);
      continue;
    }
    if (!report.final) continue;
    const sentMarker = `BATCH_REPORT_EMAIL_SENT_${file.getId()}`;
    const alreadyEmailed = Boolean(props.getProperty(sentMarker));
    if (report.action_required && !email) {
      Logger.log(`⚠️ ${file.getName()} 需要處理，但 ALERT_EMAIL 未設定`);
      continue;
    }
    if (report.action_required && !alreadyEmailed) {
      const lines = (report.jobs || [])
        .filter(job => ['incomplete', 'pending', 'versioned'].includes(job.state))
        .map(job => {
          const detail = job.reason || job.onedrive || '';
          return `- Job ${job.index} (${job.type})：${job.state}${detail ? ` — ${detail}` : ''}`;
        });
      const ok = sendPipelineEmail(
        email,
        `Jobsheet 需要處理：${report.source_file}`,
        `原始掃描：${report.source_file}\n共 ${report.jobs.length} 份工作單。\n\n`
          + `${lines.join('\n')}\n\n完整成功的工作單不會逐份寄信。`
      );
      if (!ok) continue;
      // 先留一個寄信標記才搬檔；即使 moveTo 暫時失敗，下輪也只重試搬檔，
      // 不會把同一批摘要再寄一次。搬成功便清掉標記，避免 properties 累積。
      props.setProperty(sentMarker, String(Date.now()));
    }
    if (!sent) sent = reports.createFolder(REPORTS_SENT_FOLDER_NAME);
    file.moveTo(sent);
    props.deleteProperty(sentMarker);
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

function triggerGitHubWorkflow(pat, queueFingerprint) {
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
      client_payload: {
        triggered_at: new Date().toISOString(),
        queue_fingerprint: queueFingerprint,
      },
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
