"""rclone subprocess 包裝
⚠️ 必須用 copyto 不是 copy（JOBSHEETS 有 18000+ 檔案，copy 會先掃描等幾分鐘）
"""
import subprocess
from pathlib import Path
from typing import List


class RcloneError(Exception):
    pass


def run(*args, check=True) -> str:
    """執行 rclone 指令並回傳 stdout"""
    cmd = ["rclone"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RcloneError(f"rclone {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def list_pdfs(remote_path: str, exclude_subdirs: bool = True) -> List[str]:
    """列出 remote 路徑下的 PDF 檔名（只列檔案，不含子資料夾）"""
    args = ["lsf", remote_path, "--include", "*.pdf", "--files-only"]
    if exclude_subdirs:
        args += ["--exclude", "*/**"]
    output = run(*args)
    return [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not line.strip().endswith("/")
    ]


def list_pending(remote_path: str, prefix: str = "PENDING_") -> List[str]:
    """列出 _PENDING 資料夾的特定前綴檔案"""
    output = run("lsf", remote_path, "--include", f"{prefix}*.pdf", "--files-only")
    return [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not line.strip().endswith("/")
    ]


def download(remote_file: str, local_path: Path) -> None:
    """下載單一檔案"""
    run("copyto", remote_file, str(local_path))


def upload(local_path: Path, remote_file: str) -> None:
    """上傳單一檔案（用 copyto，直接指定完整目標路徑）

    注意：copyto 遇同名會「靜默覆蓋」。會撞名的目的地（如 JOBSHEETS）請改用 upload_unique()。
    """
    run("copyto", str(local_path), remote_file)


def remote_exists(remote_file: str) -> bool:
    """檢查 remote 上「單一檔案」是否存在。

    用 lsjson --stat 直接查這一個路徑，不會掃整個資料夾（JOBSHEETS 有 18000+ 檔，很重要）：
      - 存在：rclone 回一個含 Name 的 JSON 物件（exit 0）
      - 不存在：rclone 回 directory not found（exit 3、stdout 為空）
    任何查詢失敗一律當作「不存在」，確保上傳流程不會因為檢查而中斷。
    """
    out = run("lsjson", "--stat", remote_file, check=False)
    return '"Name"' in (out or "")


def upload_unique(local_path: Path, folder: str, filename: str,
                  seen: set = None) -> str:
    """防撞名上傳：若 filename 在本輪已用過、或 OneDrive 上已存在同名，
    就在副檔名前自動加 (1)、(2)... 直到不撞名為止，避免 copyto 把前一份靜默蓋掉。
    回傳「實際使用」的檔名（給報告與日誌用）。

    seen：本輪已配發過的檔名集合（同一次執行內先佔先得，不必等雲端寫入生效）。
    """
    if seen is None:
        seen = set()
    if "." in filename:
        stem, ext = filename.rsplit(".", 1)
        suffix = "." + ext
    else:
        stem, suffix = filename, ""

    candidate = filename
    n = 0
    while candidate in seen or remote_exists(f"{folder}/{candidate}"):
        n += 1
        candidate = f"{stem} ({n}){suffix}"

    run("copyto", str(local_path), f"{folder}/{candidate}")
    seen.add(candidate)
    return candidate


def delete(remote_file: str) -> None:
    """刪除 remote 單一檔案"""
    run("delete", remote_file)


def moveto(src: str, dst: str) -> None:
    """搬移單一檔案（remote→remote 不需重新上傳；用 moveto 指定完整目標路徑）"""
    run("moveto", src, dst)
