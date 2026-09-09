"""GitHub workflow 已發生過的設定陷阡回歸測試。"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "jobsheet-split.yml",
    ROOT / ".github" / "workflows" / "jobsheet-process.yml",
)


class WorkflowConfigTests(unittest.TestCase):
    def test_rclone_download_version_does_not_use_reserved_prefix(self):
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertNotIn("\n  RCLONE_VERSION:", text)
                self.assertIn("JOBSHEET_RCLONE_RELEASE", text)


if __name__ == "__main__":
    unittest.main()
