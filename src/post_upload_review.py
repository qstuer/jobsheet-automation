"""Company-order checks after a confirmed upload; append-only Asana notes.

File delivery and this follow-up have separate outcomes. All customer content
stays in the private batch report; callers log only the returned status code.
"""
from datetime import datetime, timedelta, timezone
import html
import os
import re
import time

import requests

from . import asana_client, config

MODE_ENV = "JOBSHEET_ORDER_REVIEW_MODE"
POLICY = "order-description-v1"
HONG_KONG = timezone(timedelta(hours=8))
FIELDS = ("gid,name,notes,html_notes,due_on,due_at,modified_at,permalink_url,"
          "memberships.project.name,memberships.section.name,"
          "custom_fields.name,custom_fields.text_value,custom_fields.number_value")


class ReviewError(Exception):
    pass


class AsanaNotesClient:
    def __init__(self):
        if not config.ASANA_TOKEN:
            raise ReviewError("Asana credential unavailable")
        self.headers = {"Authorization": "Bearer " + config.ASANA_TOKEN}

    def read(self, gid):
        if not re.fullmatch(r"\d+", str(gid)):
            raise ReviewError("Invalid task identifier")
        for attempt in range(2):
            try:
                response = requests.get(f"{config.ASANA_BASE_URL}/tasks/{gid}",
                    headers=self.headers, params={"opt_fields": FIELDS}, timeout=20)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 0:
                        time.sleep(2)
                        continue
                response.raise_for_status()
                task = response.json()["data"]
                if not isinstance(task, dict) or str(task.get("gid")) != str(gid):
                    raise ReviewError("Unexpected task response")
                return task
            except (requests.RequestException, ValueError, KeyError) as exc:
                if attempt == 0 and isinstance(exc, requests.RequestException) \
                        and not isinstance(exc, requests.HTTPError):
                    time.sleep(2)
                    continue
                raise ReviewError("Asana description read failed") from None
        raise ReviewError("Asana description read failed")

    def write_html(self, gid, value):
        # Never blindly retry a timed-out write. The caller reads back first.
        if not re.fullmatch(r"\d+", str(gid)):
            raise ReviewError("Invalid task identifier")
        try:
            response = requests.put(f"{config.ASANA_BASE_URL}/tasks/{gid}",
                headers=self.headers, json={"data": {"html_notes": value}}, timeout=20)
            response.raise_for_status()
        except requests.RequestException:
            raise ReviewError("Asana description write not confirmed") from None


def company_order(task):
    for field in task.get("custom_fields") or []:
        if re.sub(r"[^a-z]", "", field.get("name", "").lower()) in {
                "ordernumber", "orderno", "serviceordernumber"}:
            value = field.get("text_value") or field.get("number_value")
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and 10000000 <= value <= 99999999 and int(value) == value:
                value = str(int(value))
            if value is not None and re.fullmatch(r"[56]\d{7}", str(value)):
                return str(value)
    # Hospital references are not company orders, even if eight digits long.
    name = re.sub(r"\b(?:HAWO|WO)\s*[#.:\-]?\s*\d+", "", task.get("name", ""), flags=re.I)
    return asana_client.extract_order_no_from_name({"name": name})


def schedule(task):
    try:
        value = datetime.fromisoformat(task["due_at"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            return None
        return value.astimezone(HONG_KONG)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def fault_text(notes):
    kept = []
    for line in re.sub(r"https?://\S+", "", notes or "").splitlines():
        line = line.strip()
        if not line:
            if kept:
                break
            continue
        if re.match(r"(?:contact|asset|tel|phone|聯絡|電話)\s*[:：]", line, re.I) \
                or re.fullmatch(r"[\d\s()+-]+", line) \
                or re.match(r"(?:mr|ms|miss|mrs)\.?\s+.*\d", line, re.I):
            continue
        if re.match(r"\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?\s", line):
            continue
        kept.append(re.sub(r"^fault\s*:\s*", "", line, flags=re.I))
    return "\n".join(kept).strip()


def short_issue(text):
    for pattern, label in (
        (r"probe|探頭|探头", "Probe issue"),
        (r"button|keyboard|按鍵|按键|按鈕|制建|制鍵|掣|\bcw\b.*\bcolor\b", "Button issue"),
        (r"boot|開唔到|開不到|開不了|無法開機|无法开机|cannot\s+(?:power|turn)\s+on", "bootup issue"),
        (r"signal|訊號|信號|信号", "signal issue"),
        (r"printer|打印|印表", "printer issue"),
        (r"capture", "capture issue"),
        (r"\btgc\b|gain|增益", "TGC Gain issue"),
        (r"battery|電池|电池", "Battery issue"),
        (r"display|screen|monitor.*line|螢幕|屏幕|畫面|画面|雪花", "Display issue"),
        (r"software|diagnostic|message\s+box|pop\s+out|系統錯誤|系统错误", "software issue"),
        (r"measurement|測量|测量", "Measurement issue"),
    ):
        if re.search(pattern, text or "", re.I):
            return label
    return None


def has_service_record(notes, day):
    text = re.sub(r"https?://\S+", "", notes or "")
    pattern = r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?[ \t]+(\d{1,2}):(\d{2})(?!\d)|(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?[ \t]+(\d{2})(\d{2})(?!\d)"
    for match in re.finditer(pattern, text):
        values = match.groups()[:5] if match.group(1) is not None else match.groups()[5:]
        d, m, year, hour, minute = values
        d, m, hour, minute = int(d), int(m), int(hour), int(minute)
        if m > 12 and d <= 12:  # Unambiguous legacy month/day spelling only.
            d, m = m, d
        if year and int(year) % 100 != day.year % 100:
            continue
        if (d, m) == (day.day, day.month) and hour < 24 and minute < 60:
            return True
    return False


def image_ids(value):
    return sorted(re.findall(r"<img\b[^>]*\bdata-asana-gid=[\"']([^\"']+)", value or "", re.I))


def preserved(before, after, block):
    old = [v.strip() for v in (before.get("notes") or "").splitlines() if v.strip()]
    new = [v.strip() for v in (after.get("notes") or "").splitlines() if v.strip()]
    position = 0
    for line in new:
        if position < len(old) and line == old[position]:
            position += 1
    return (position == len(old)
        and (after.get("notes") or "").strip().endswith(block)
        and image_ids(before.get("html_notes")) == image_ids(after.get("html_notes")))


def _version(task):
    return tuple(task.get(key) for key in ("modified_at", "notes", "name", "due_at"))


def review(matched_task, job_type, *, mode=None, client=None, fault_reader=None,
           before_write=None, prior_block=None):
    mode = mode if mode is not None else os.environ.get(MODE_ENV, "off")
    if mode == "off":
        return {"status": "disabled", "needs_attention": False}
    if mode not in {"preview", "apply"}:
        raise ValueError("Invalid order review mode")
    gid = str((matched_task or {}).get("gid") or "")
    base = {"policy": POLICY, "mode": mode, "task_gid": gid,
            "task_url": f"https://app.asana.com/0/0/{gid}" if gid else None}

    def outcome(status, message, *, attention=True, **details):
        return {**base, "status": status, "needs_attention": attention,
                "message": message, **details}

    if not gid or job_type not in {"CM", "PM"}:
        return outcome("task_unconfirmed", "工作未能唯一確認，未自動修改 Asana。")
    try:
        client = client or AsanaNotesClient()
        for _ in range(2):
            current = client.read(gid)
            if company_order(current):
                return outcome("company_order_exists", "已有公司 Order Number。", attention=False)
            if asana_client._task_job_type(current) != job_type:
                return outcome("task_type_changed", "任務類型已變更，未修改描述，請核對。")
            if job_type == "PM":
                return outcome("pm_missing_order", "PM 未見公司 Order Number；請確認是否需要請 admin 開單。")
            if prior_block and prior_block in (current.get("notes") or ""):
                return outcome("description_complete", "CM 未見公司單號；這份工作單先前的紀錄仍在，未重複補寫，請聯絡 admin 開單。")
            if current.get("name") != matched_task.get("name"):
                return outcome("task_changed", "任務名稱已變更，未修改描述，請核對後安排開單。")
            when = schedule(current)
            if when is None:
                return outcome("missing_schedule", "CM 未見公司單號，Asana 未標記明確時間；請先核對排程。")
            fault = fault_text(current.get("notes"))
            if fault and has_service_record(current.get("notes"), when.date()):
                return outcome("description_complete", "CM 未見公司單號；原描述已有問題及當次日期時間，未重複補寫，請聯絡 admin 開單。")
            issue = short_issue(fault)
            if not issue and fault_reader is not None:
                issue = fault_reader()
            if not isinstance(issue, str) or not re.fullmatch(r"[A-Za-z]+(?:[ -][A-Za-z]+){1,4}", issue) \
                    or not issue.lower().endswith(" issue"):
                return outcome("issue_needs_review", "CM 未見公司單號；故障簡述未能可靠分類，未編造內容，請核對後安排開單。")
            stamp = f"{when.day}/{when.month} {when:%H:%M}"
            block = issue + "\n" + stamp
            if mode == "preview":
                return outcome("preview", "CM 未見公司單號；試行草稿尚未寫入 Asana，請核對後安排開單。", draft=block)
            original_html = current.get("html_notes")
            if not original_html and not current.get("notes"):
                original_html = "<body></body>"
            if not isinstance(original_html, str) or not re.search(r"</body>\s*$", original_html, re.I):
                return outcome("rich_text_unavailable", "CM 未見公司單號；無法完整保留現有格式及圖片，未修改描述。")
            closing = original_html.lower().rfind("</body>")
            updated = original_html[:closing] + "\n\n" + html.escape(block) + "\n" + original_html[closing:]
            latest = client.read(gid)
            if _version(latest) != _version(current):
                continue  # Rebase on a new read; never overwrite a known edit.
            if before_write is not None:
                before_write(current, block)
            try:
                client.write_html(gid, updated)
            except ReviewError:
                pass  # A timed-out request may have committed; read it back.
            after = client.read(gid)
            if preserved(current, after, block):
                asana_client._task_cache.pop(gid, None)
                return outcome("description_updated", "CM 未見公司單號；已補寫並讀回驗證，請聯絡 admin 開單。", appended=block)
            return outcome("update_unverified", "檔案已上傳，但 CM 描述補寫未能驗證；請核對後安排開單。")
        return outcome("concurrent_edit", "CM 描述在檢查期間有其他更新，未覆蓋同事內容，請核對後安排開單。")
    except (ReviewError, requests.RequestException):
        return outcome("service_unavailable", "檔案已上傳，但 Asana 單號及描述檢查暫時失敗，請核對。")
