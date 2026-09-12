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

    def test_model_name_uses_non_secret_repository_variable(self):
        workflow = ROOT / ".github" / "workflows" / "jobsheet-process.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("NVIDIA_MODEL:         ${{ vars.NVIDIA_MODEL }}", text)
        self.assertNotIn("secrets.NVIDIA_MODEL", text)

        split_text = (ROOT / ".github" / "workflows" / "jobsheet-split.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("NVIDIA_MODEL: ${{ vars.NVIDIA_MODEL }}", split_text)
        self.assertNotIn("secrets.NVIDIA_MODEL", split_text)

    def test_stage_b_manual_run_can_target_exactly_one_pdf(self):
        workflow = ROOT / ".github" / "workflows" / "jobsheet-process.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("jobsheet_file:", text)
        self.assertIn("JOBSHEET_TARGET_FILE: ${{ inputs.jobsheet_file }}", text)

    def test_asana_workspace_id_is_not_treated_as_a_secret(self):
        for workflow_name in (
            "jobsheet-process.yml", "jobsheet-order-lookup.yml",
            "jobsheet-asana-audit.yml",
        ):
            workflow = ROOT / ".github" / "workflows" / workflow_name
            text = workflow.read_text(encoding="utf-8")
            self.assertIn("vars.ASANA_WORKSPACE_GID", text)
            self.assertNotIn("secrets.ASANA_WORKSPACE_GID", text)


if __name__ == "__main__":
    unittest.main()
