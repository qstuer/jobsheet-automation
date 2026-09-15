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
        self.assertIn("${{ vars.NVIDIA_MODEL }}", split_text)
        self.assertNotIn("secrets.NVIDIA_MODEL", split_text)

    def test_paid_deepseek_can_be_selected_without_changing_default(self):
        for workflow_name in ("jobsheet-split.yml", "jobsheet-process.yml"):
            text = (ROOT / ".github" / "workflows" / workflow_name).read_text(
                encoding="utf-8"
            )
            self.assertIn("vars.OCR_PROVIDER", text)
            self.assertIn("secrets.DEEPSEEK_API_KEY", text)
            self.assertIn("vars.DEEPSEEK_MODEL", text)

        dry_run = (ROOT / ".github" / "workflows" / "jobsheet-dry-run.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("ocr_provider:", dry_run)
        self.assertIn("default: deepseek", dry_run)

        smoke = (
            ROOT / ".github" / "workflows" / "deepseek-model-check.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("OCR_PROVIDER:       deepseek", smoke)
        self.assertIn("secrets.DEEPSEEK_API_KEY", smoke)
        self.assertNotIn("RCLONE_CONFIG", smoke)
        self.assertNotIn("ASANA_TOKEN", smoke)

    def test_stage_b_manual_run_can_target_exactly_one_pdf(self):
        workflow = ROOT / ".github" / "workflows" / "jobsheet-process.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("jobsheet_file:", text)
        self.assertIn("JOBSHEET_TARGET_FILE: ${{ inputs.jobsheet_file }}", text)
        self.assertIn("source_queue:", text)
        self.assertIn("dry_run:", text)
        self.assertIn("JOBSHEET_DRY_RUN:", text)
        self.assertIn("confirmed_filename:", text)
        self.assertIn("JOBSHEET_CONFIRMED_FILENAME:", text)
        self.assertIn("ocr_provider:", text)
        self.assertIn("inputs.ocr_provider || vars.OCR_PROVIDER", text)

    def test_raw_dry_run_is_separate_and_cannot_trigger_stage_b(self):
        workflow = ROOT / ".github" / "workflows" / "jobsheet-dry-run.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("repository_dispatch", text)
        self.assertNotIn("jobsheet-process", text.lower())
        self.assertIn("python -m src.dry_run", text)

    def test_asana_workspace_id_is_not_treated_as_a_secret(self):
        for workflow_name in (
            "jobsheet-process.yml", "jobsheet-order-lookup.yml",
            "jobsheet-asana-audit.yml",
        ):
            workflow = ROOT / ".github" / "workflows" / workflow_name
            text = workflow.read_text(encoding="utf-8")
            self.assertIn("vars.ASANA_WORKSPACE_GID", text)
            self.assertNotIn("secrets.ASANA_WORKSPACE_GID", text)

    def test_asana_index_can_discard_stale_parsed_values_on_full_rebuild(self):
        workflow = ROOT / ".github" / "workflows" / "jobsheet-asana-index.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("full_rebuild:", text)
        self.assertIn("if: ${{ !inputs.full_rebuild }}", text)
        self.assertIn("if: ${{ inputs.full_rebuild }}", text)


class AppsScriptSafetyTests(unittest.TestCase):
    def test_dispatch_busy_state_is_checked_before_and_after_upload_wait(self):
        text = (ROOT / "apps_script" / "trigger.gs").read_text(encoding="utf-8")
        self.assertEqual(2, text.count("if (isPipelineBusy(pat))"))
        self.assertIn("changedDuringWait", text)

    def test_batch_email_is_marked_before_report_is_moved(self):
        text = (ROOT / "apps_script" / "trigger.gs").read_text(encoding="utf-8")
        marked = text.index("props.setProperty(sentMarker")
        moved = text.index("file.moveTo(sent)")
        cleared = text.index("props.deleteProperty(sentMarker)")
        self.assertLess(marked, moved)
        self.assertLess(moved, cleared)


if __name__ == "__main__":
    unittest.main()
