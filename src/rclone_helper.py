"""rclone subprocess 包裝。

⚠️ 必須用 copyto 不是 copy（JOBSHEETS 有 18000+ 檔案，copy 會先掃描等幾分鐘）。
⚠️ 查詢失敗不能當成「檔案不存在」，否則可能覆蓋 OneDrive 既有檔案。
"""
import hashlib
import json
import subprocess
from pathlib import Path
from typing import List, Optional


class RcloneError(Exception):
    pass


def run_result(*args) -> subprocess.CompletedProcess:
    """執行 rclone，保留 return code、stdout、stderr 給需要判斷結果的呼叫者。"""
    cmd = ["rclone"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def _raise_for_result(args, result: subprocess.CompletedProcess) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "沒有錯誤內容").strip()
    raise RcloneError(
        f"rclone {' '.join(args)} failed (exit {result.returncode}):\n{detail}"
    )


def run(*args) -> str:
    """執行 rclone 指令；失敗時拋錯，不允許靜默略過。"""
    result = run_result(*args)
    _raise_for_result(args, result)
    return result.stdout


def list_pdfs(remote_path: str, exclude_subdirs: bool = True) -> List[str]:
    """列出 remote 路徑下的 PDF 檔名（只列檔案，不含子資料夾）"""
    args = ["lsf", remote_path, "--include", "*.pdf", "--files-only"]
    if exclude_subdirs:
        # 不混用 --include / --exclude：新版 rclone 會警告規則順序不確定，
        # 甚至可能遞迴掃描子資料夾後回傳空清單。max-depth=1 明確只看根目錄。
        args += ["--max-depth", "1"]
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


def remote_stat(remote_file: str) -> Optional[dict]:
    """取得遠端單一檔案資料；確定不存在時回 None，其他失敗一律拋錯。

    用 lsjson --stat 直接查這一個路徑，不會掃整個資料夾（JOBSHEETS 有 18000+ 檔，很重要）：
      - 存在：exit 0，回 JSON
      - 路徑不存在：exit 3，回 None
      - 登入過期、網路中斷等：拋 RcloneError，停止處理並保留來源檔
    """
    args = ("lsjson", "--stat", remote_file)
    result = run_result(*args)
    if result.returncode == 3:
        return None
    _raise_for_result(args, result)
    try:
        data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RcloneError(f"rclone 回傳的檔案資料無法解析：{remote_file}") from exc
    if not isinstance(data, dict):
        raise RcloneError(f"rclone 回傳的檔案資料格式不正確：{remote_file}")
    return data


def remote_exists(remote_file: str) -> bool:
    """檢查遠端單一檔案是否存在；查詢故障時不會誤報為不存在。"""
    return remote_stat(remote_file) is not None


def _sha256_local(local_path: Path) -> str:
    digest = hashlib.sha256()
    with local_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remote_matches(local_path: Path, remote_file: str, stat: dict = None) -> bool:
    """比較本機與遠端檔案內容。

    只在目的檔名已存在時使用。先比大小，再請 rclone 下載計算 SHA-256；
    這讓「已上傳成功、但來源刪除失敗」的重跑能認出同一份檔案，不會再生 (1)。
    """
    stat = stat if stat is not None else remote_stat(remote_file)
    if stat is None or stat.get("IsDir"):
        return False
    if stat.get("Size") != local_path.stat().st_size:
        return False

    output = run("hashsum", "SHA-256", "--download", remote_file).strip()
    remote_hash = output.split(maxsplit=1)[0].lower() if output else ""
    return bool(remote_hash) and remote_hash == _sha256_local(local_path)


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
    while True:
        if candidate in seen:
            n += 1
            candidate = f"{stem} ({n}){suffix}"
            continue

        remote_file = f"{folder}/{candidate}"
        stat = remote_stat(remote_file)
        if stat is None:
            run("copyto", str(local_path), remote_file)
            seen.add(candidate)
            return candidate

        # 上一輪可能已完成上傳，只在刪除 Google Drive 來源時失敗。
        # 內容相同就直接沿用原檔名，不重複上傳。
        if remote_matches(local_path, remote_file, stat=stat):
            seen.add(candidate)
            return candidate

        n += 1
        candidate = f"{stem} ({n}){suffix}"


def delete(remote_file: str) -> None:
    """刪除 remote 單一檔案"""
    run("deletefile", remote_file)


def moveto(src: str, dst: str) -> None:
    """搬移單一檔案（remote→remote 不需重新上傳；用 moveto 指定完整目標路徑）"""
    run("moveto", src, dst)
