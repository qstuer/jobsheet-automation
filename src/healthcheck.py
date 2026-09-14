"""只讀取雲端佇列，讓人或 AI 快速看出檔案卡在哪一站。"""
import logging
import sys

from . import config, rclone_helper

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("healthcheck")


QUEUES = (
    ("等待切割", config.GDRIVE_INPUT),
    ("等待辨認及上傳", config.GDRIVE_SPLIT),
    ("切頁邊界需人工檢查", config.GDRIVE_SPLIT_FAILED),
    ("掃描不完整、需要重掃", config.GDRIVE_INCOMPLETE),
    ("名稱等待人工核對", config.GDRIVE_PENDING),
)


def main() -> int:
    try:
        for label, remote in QUEUES:
            # 新增的狀態資料夾在第一次真正需要前可能尚未存在；rclone 的
            # exit 3 在這裡只代表該資料夾為空／未建立，不應令整個 Stage B
            # 在開始辨認前失敗。登入、網路等其他錯誤仍會正常拋出。
            files = rclone_helper.list_files(remote, "*.pdf")
            log.info(f"{label}：{len(files)} 份")
            for filename in files:
                log.info(f"  - {filename}")
    except rclone_helper.RcloneError as exc:
        log.error(f"雲端連線檢查失敗：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
