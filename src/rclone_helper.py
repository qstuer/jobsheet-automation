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
    """列出 remote 路徑下的 PDF 檔名（預設不含子資料夾）"""
    args = ["lsf", remote_path, "--include", "*.pdf"]
    if exclude_subdirs:
        args += ["--exclude", "*/**"]
    output = run(*args)
    return [line.strip() for line in output.splitlines() if line.strip()]


def list_pending(remote_path: str, prefix: str = "PENDING_") -> List[str]:
    """列出 _PENDING 資料夾的特定前綴檔案"""
    output = run("lsf", remote_path, "--include", f"{prefix}*.pdf")
    return [line.strip() for line in output.splitlines() if line.strip()]


def download(remote_file: str, local_path: Path) -> None:
    """下載單一檔案"""
    run("copyto", remote_file, str(local_path))


def upload(local_path: Path, remote_file: str) -> None:
    """上傳單一檔案（用 copyto，直接指定完整目標路徑）"""
    run("copyto", str(local_path), remote_file)


def delete(remote_file: str) -> None:
    """刪除 remote 單一檔案"""
    run("delete", remote_file)
