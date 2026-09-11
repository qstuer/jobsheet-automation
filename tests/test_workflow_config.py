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

    def test_failure_alert_does_not_comment_on_every_later_failure(self):
        workflow = ROOT / ".github" / "workflows" / "jobsheet-failure-alert.yml"
        text = workflow.read_text(encoding="utf-8")
        # One recovery comment is intentional; the repeated-failure branch must update silently.
        self.assertEqual(1, text.count("github.rest.issues.createComment"))
        self.assertIn("without a new comment", text)


if __name__ == "__main__":
    unittest.main()
