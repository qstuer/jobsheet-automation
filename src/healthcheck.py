"""只讀取雲端佇列，讓人或 AI 快速看出檔案卡在哪一站。"""
import logging
import sys

from . import config, rclone_helper

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("healthcheck")


QUEUES = (
    ("等待切割", config.GDRIVE_INPUT),
    ("等待辨認及上傳", config.GDRIVE_SPLIT),
    ("單據內容需人工檢查", config.GDRIVE_SPLIT_FAILED),
    ("舊版待處理區", config.GDRIVE_PENDING),
)


def main() -> int:
    try:
        for label, remote in QUEUES:
            files = rclone_helper.list_pdfs(remote, exclude_subdirs=True)
            log.info(f"{label}：{len(files)} 份")
            for filename in files:
                log.info(f"  - {filename}")
    except rclone_helper.RcloneError as exc:
        log.error(f"雲端連線檢查失敗：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
