"""Equipment search is not a service-visit answer. Synthetic identities only."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import date
import json
import unittest
from unittest.mock import MagicMock, patch

import fitz

from src import asana_client as ac, asana_index, config, device_search_agent as agent
from src import device_table_search as search, matching_rules as rules, nvidia_client as vision
from src import rclone_helper
from tests.test_matching_rules import reading, task
from tests.test_ocr_resilience import vocabulary_index


def device(**kwargs):
    return asana_index.task_to_record(task(**kwargs))


class DeviceTableTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)
        self.row = device()
        self.index = vocabulary_index([self.row])

    def table(self, reads=None, index=None):
        reads = [reading()] * 2 if reads is None else reads
        return search.DeviceTable(index or self.index, search.collect_clues(rules.evidence(reads), reads))

    def test_same_serials_history_can_support_search_across_visits_without_mutation(self):
        second = device(gid="102", product="Affiniti 70G", phone="28654321", kind="CM")
        asana_index._merge_record(self.row, second)
        before = deepcopy(self.index)
        table = self.table([reading(product="Affiniti 50", phone="28654321")] * 2)
        result = table.search_devices(["serial", "product", "phone"])
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, result["count"])
        self.assertEqual(["Affiniti 70", "Affiniti 70G"], self.row["product_variants"])
        self.assertEqual(2, len(self.row["task_refs"]))
        self.assertEqual(before, self.index)

    def test_phone_mismatch_and_missing_asset_do_not_veto_equipment_search(self):
        table = self.table([reading(phone="28654321", asset=None)] * 2)
        self.assertEqual(1, table.search_devices(["serial", "product"])["count"])
        self.assertEqual(0, table.search_devices(["serial", "phone"])["count"])
        rows, _ = rules.retrieve_rows(self.index, rules.evidence([reading(phone=None)] * 2))
        self.assertEqual([self.row], rows)

    def test_known_hospital_aliases_and_internal_floor_are_same_group(self):
        row = device(hospital="PYNEH")
        row["locations"].append("PYNEH-3F-Xray")
        table = self.table([reading(hospital="PYN")] * 2, vocabulary_index([row]))
        self.assertEqual(1, table.search_devices(["product", "hospital"])["count"])
        self.assertIn("PYNEH-3F-Xray", row["locations"])

    def test_unknown_codes_do_not_supply_hospital_search(self):
        for name in ("PN", "KWM", "PYTV", "AB12C"):
            table = self.table([reading(hospital=name)] * 2)
            self.assertEqual("insufficient_clues", table.search_devices(["product", "hospital"])["status"])

    def test_equipment_moves_do_not_merge_hospital_alias_groups(self):
        other = device(gid="102", hospital="Other Hospital", phone="28654321")
        asana_index._merge_record(self.row, other)
        table = self.table([reading(hospital="Other Hospital", phone="23456789")] * 2)
        # Cross-visit history is allowed ONLY at the equipment layer.
        self.assertEqual(1, table.search_devices(["hospital", "phone"])["count"])
        self.assertNotEqual(ac.hospital_core("Test Hospital"), ac.hospital_core("Other Hospital"))
        ev = rules.evidence([reading(hospital="Other Hospital")] * 2)
        self.assertIsNone(rules.select_task([task()], ev, "PM", None)[0])

    def test_dates_type_and_completion_do_not_filter_equipment(self):
        old = device(due="2020-01-01", kind="CM", completed=True)
        rows, _ = rules.retrieve_rows(vocabulary_index([old]), rules.evidence([reading(day=None)] * 2))
        self.assertEqual([old], rows)

    def test_context_can_find_equipment_without_serial_phone_or_asset(self):
        reads = [reading(serial=None, phone=None, asset=None, day=None)] * 2
        rows, reason = rules.retrieve_rows(self.index, rules.evidence(reads))
        self.assertEqual(([self.row], "table_context_retrieval"), (rows, reason))
        # Retrieval is not permission to accept the missing-serial document.
        self.assertIsNone(rules.select_task([task()], rules.evidence(reads), "PM", date(2026, 10, 5))[0])

    def test_wrong_phone_consensus_does_not_erase_equipment_candidates(self):
        reads = [reading(serial=None, phone="28654321")] * 2
        rows, reason = rules.retrieve_rows(self.index, rules.evidence(reads))
        self.assertEqual(([self.row], "table_context_retrieval"), (rows, reason))

    def test_partial_serial_remains_a_literal_search_clue_not_an_ocr_vote(self):
        reads = [{**reading(serial=None, phone=None), "_ocr_audit": {"raw": {"serial_candidates": ["ABC1"]}}}]
        table = self.table(reads)
        self.assertEqual(1, table.search_devices(["serial", "product"], "fragment")["count"])
        rows, reason = rules.retrieve_rows(self.index, rules.evidence(reads), reads)
        self.assertEqual(([self.row], "table_fragment_retrieval"), (rows, reason))
        self.assertFalse(rules.evidence(reads)["serial_candidates"])
        self.assertEqual(["ABC1"], reads[0]["_ocr_audit"]["raw"]["serial_candidates"])

    def test_unknown_character_is_not_removed_or_completed(self):
        reads = [{**reading(serial=None), "_ocr_audit": {"raw": {"serial_candidates": ["ABC12?3456"]}}}]
        table = self.table(reads)
        self.assertEqual(["ABC12?3456"], table.clues["serial"])
        self.assertEqual(0, table.search_devices(["serial"], "fuzzy")["count"])
        self.assertEqual(1, table.search_devices(["serial"], "fragment")["count"])
        self.assertFalse(rules.evidence(reads)["serial_candidates"])

    def test_serial_labels_short_fragments_and_arbitrary_prose_not_search_clues(self):
        for raw in ("ABC", "Serial ABC12X3456", "ABC12X3456 or ABC12X3457", "../../secret", "????12????"):
            reads = [{"_ocr_audit": {"raw": {"serial_candidates": [raw]}}}]
            self.assertEqual([], self.table(reads).clues["serial"])

    def test_numeric_auxiliary_phone_may_search_but_cannot_confirm_identity(self):
        with patch.object(vision, "_today", return_value=date(2026, 9, 30)):
            auxiliary = vision._recover_numeric_field_literal("23456789", "phone_candidates")
        reads = [reading(serial="ABC12X3450", phone=None)] * 2 + [auxiliary] * 2
        table = self.table(reads)
        self.assertEqual(1, table.search_devices(["phone", "product"])["count"])
        ev = rules.evidence(reads)
        self.assertFalse(ev["phone_candidates"])
        self.assertIsNone(rules.select_task([task()], ev, "PM", None)[0])

    def test_same_phone_never_uniquely_identifies_multiple_devices(self):
        index = vocabulary_index([self.row, device(serial="ABC12X3457", gid="102")])
        result = self.table(index=index).search_devices(["phone", "hospital"])
        self.assertEqual(2, result["count"])

    def test_context_overflow_stops_instead_of_returning_best_ten(self):
        index = vocabulary_index([device(serial=f"ABC12X34{number:02}", gid=str(number)) for number in range(11)])
        reads = [reading(serial=None, phone=None)] * 2
        result = self.table(reads, index).search_devices(["product", "hospital"])
        self.assertEqual(("candidate_limit", 11, []), (result["status"], result["count"], result["entries"]))
        self.assertEqual(([], "device_candidate_limit"), rules.retrieve_rows(index, rules.evidence(reads)))

    def test_weak_rows_are_not_pretended_to_be_known_serial_equipment(self):
        self.row.update({"weak_identity": True, "serial": ""})
        self.assertEqual(0, self.table().search_devices(["product", "hospital"])["count"])
        self.assertEqual([], self.table().entries)

    def test_candidate_view_has_no_task_date_type_order_or_undisclosed_truncation(self):
        self.row["contacts"] = [f"Person {i}" for i in range(14)]
        self.row["order_no"] = "61234567"
        view = self.table().candidate_view(self.table().entries[0])
        text = json.dumps(view)
        for forbidden in ("task_refs", "due_on", "completed", "61234567", "work_dates", "job_types"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(2, view["truncated_counts"]["contacts"])

    def test_corrupt_index_is_failure_not_an_empty_search(self):
        for index in (None, {}, {"devices": [None]}):
            with self.assertRaises(asana_index.AsanaIndexError):
                search.DeviceTable(index, {})


class SearchSessionTests(unittest.TestCase):
    setUp = DeviceTableTests.setUp
    table = DeviceTableTests.table

    def test_query_cannot_invent_values_or_call_writes(self):
        session = search.SearchSession(self.table())
        for request in ({"action": "delete", "fields": ["serial"], "serial_mode": "exact"},
                        {"action": "search_devices", "fields": ["serial"], "serial_mode": "exact", "serial": "BAD12345"}):
            with self.assertRaises(ValueError):
                session.call(request)

    def test_query_field_and_mode_validation(self):
        for fields, mode in (([], "fuzzy"), (["date"], "exact"), (["serial", "serial"], "fuzzy"),
                             ([{}], "fuzzy"), (["serial"], []), (["serial"], "regex")):
            with self.assertRaises(ValueError):
                self.table().search_devices(fields, mode)

    def test_three_queries_is_a_hard_limit(self):
        session = search.SearchSession(self.table())
        request = {"action": "search_devices", "fields": ["serial"], "serial_mode": "exact"}
        for _ in range(3):
            self.assertEqual("ok", session.call(request)["status"])
        self.assertEqual("query_limit", session.call(request)["status"])
        self.assertEqual(3, session.search_count)

    def test_query_cannot_discard_rivals_returned_by_an_earlier_search(self):
        second = device(gid="102", serial="ABC12X3457", phone="28654321")
        session = search.SearchSession(self.table(index=vocabulary_index([self.row, second])))
        session.call({"action": "search_devices", "fields": ["product", "hospital"], "serial_mode": "fuzzy"})
        session.call({"action": "search_devices", "fields": ["phone", "product"], "serial_mode": "exact"})
        self.assertEqual(2, len(session.seen))

    def test_ten_candidates_is_a_cumulative_limit(self):
        rows = [device(serial=f"ABC12X34{n:02}", gid=str(n), product="EPIQ 7G" if n < 6 else "Affiniti 70") for n in range(12)]
        clues = search.collect_clues(rules.evidence([reading(), reading(product="EPIQ 7G")]))
        session = search.SearchSession(search.DeviceTable(vocabulary_index(rows), clues))
        session.table.clues["product"] = ["EPIQ"]
        self.assertEqual(6, session.call({"action": "search_devices", "fields": ["product", "hospital"], "serial_mode": "fuzzy"})["count"])
        session.table.clues["product"] = ["AFFINITI"]
        result = session.call({"action": "search_devices", "fields": ["product", "hospital"], "serial_mode": "fuzzy"})
        self.assertEqual("candidate_limit", result["status"])
        self.assertEqual([], result["candidates"])
        self.assertTrue(session.stopped)


class ModelBridgeTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)
        self.index = vocabulary_index([device(), device(gid="102", serial="ABC12X3457")])
        self.reads = [reading()] * 2
        self.ev = rules.evidence(self.reads)

    def run_agent(self, replies, initial=()):
        with patch.dict("os.environ", {config.JOBSHEET_DRY_RUN_ENV: "1"}), \
             patch.object(vision, "crop_jobsheet_field_card", return_value="synthetic-image"), \
             patch.object(vision, "_call_vision", side_effect=[json.dumps(reply) for reply in replies]) as call:
            result = agent.run_search(None, self.index, self.ev, self.reads, initial)
        return result, call

    def test_real_card_renderer_uses_only_existing_panels(self):
        # Asset clues are in the document's department/room field, not a
        # separately printed asset cell. Mocking the renderer hid this error.
        with fitz.open() as doc:
            doc.new_page(width=600, height=800)
            with patch.dict("os.environ", {config.JOBSHEET_DRY_RUN_ENV: "1"}), \
                 patch.object(vision, "_call_vision", return_value='{"action":"uncertain"}') as call:
                result = agent.run_search(doc, self.index, self.ev, self.reads)
        self.assertEqual("uncertain", result["status"])
        call.assert_called_once()

    def test_model_can_change_search_fields_but_proposal_does_not_erase_rival(self):
        selected = {"action": "select_candidate", "candidate_id": "D1", "matched_fields": ["serial", "hospital"], "conflicting_fields": []}
        result, call = self.run_agent([
            {"action": "search_devices", "fields": ["product", "hospital"], "serial_mode": "fuzzy"},
            {"action": "search_devices", "fields": ["serial", "phone"], "serial_mode": "exact"}, selected])
        self.assertEqual(("proposal", 2, 3), (result["status"], len(result["rows"]), call.call_count))
        self.assertEqual("D1", result["proposed_id"])
        self.assertEqual(2, result["audit"]["query_count"])
        prompt = call.call_args.args[0]
        self.assertNotIn("task_refs", prompt)
        self.assertNotIn("due_on", prompt)
        self.assertEqual(self.ev, rules.evidence(self.reads))

    def test_table_external_answer_is_rejected(self):
        result, _ = self.run_agent([{"action": "select_candidate", "candidate_id": "NEW_SERIAL", "matched_fields": ["serial"], "conflicting_fields": []}])
        self.assertEqual("device_search_protocol_rejected", result["reason"])

    def test_explicit_uncertainty_and_visible_conflict_never_select(self):
        for reply in ({"action": "uncertain"}, {"action": "select_candidate", "candidate_id": "D1", "matched_fields": ["serial"], "conflicting_fields": ["hospital"]}):
            result, _ = self.run_agent([reply], self.index["devices"])
            self.assertEqual("uncertain", result["status"])

    def test_fourth_model_search_stops_without_fifth_model_turn(self):
        query = {"action": "search_devices", "fields": ["serial"], "serial_mode": "exact"}
        result, call = self.run_agent([query] * 4)
        self.assertEqual(("uncertain", 4, 3), (result["status"], call.call_count, result["audit"]["query_count"]))

    def test_model_service_failure_propagates_not_pending_or_success(self):
        with patch.dict("os.environ", {config.JOBSHEET_DRY_RUN_ENV: "1"}), \
             patch.object(vision, "crop_jobsheet_field_card", return_value="synthetic-image"), \
             patch.object(vision, "_call_vision", side_effect=vision.NvidiaResponseError("offline")):
            with self.assertRaises(vision.NvidiaResponseError):
                agent.run_search(None, self.index, self.ev, self.reads)

    def test_non_read_only_agent_fails_before_ocr(self):
        with patch.dict("os.environ", {agent.ENABLED_ENV: "1", config.JOBSHEET_DRY_RUN_ENV: "0"}), \
             patch.object(vision, "ocr_jobsheet_fields") as reader:
            with self.assertRaises(ValueError):
                rules.read_and_match(None, "PM")
        reader.assert_not_called()

    def test_public_audit_does_not_leak_equipment_candidates_or_model_replies(self):
        audit = {"version": rules.RULESET_VERSION, "equipment_search": {
            "candidate_count": 2, "legacy_reference_count": 0, "model_invoked": True,
            "raw_replies": ["23456789"], "serial": "ABC12X3456", "hospital": "Test Hospital"}}
        projected = rules.public_diagnostic(audit)
        self.assertEqual(2, projected["equipment_search"]["candidate_count"])
        for value in ("23456789", "ABC12X3456", "Test Hospital", "raw_replies"):
            self.assertNotIn(value, str(projected))


class SearchPipelineTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)

    def test_retrieved_context_does_not_supply_missing_phone_serial_asset_or_date(self):
        row = device()
        ac.set_device_index(vocabulary_index([row]))
        fields = vision._normalize_ocr_data({"product_raw": "Affiniti 70", "hospital_raw": "Test Hospital"})
        doc = MagicMock()
        doc.__len__.return_value = 4
        with ExitStack() as stack:
            stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")))
            stack.enter_context(patch.object(vision, "_call_vision", side_effect=AssertionError("Model forbidden")))
            for name in ("upload", "upload_unique", "delete", "moveto", "run", "run_result"):
                stack.enter_context(patch.object(rclone_helper, name, side_effect=AssertionError("Cloud forbidden")))
            for name in ("ocr_jobsheet_fields", "ocr_jobsheet_identity_fields", "ocr_jobsheet_support_fields"):
                stack.enter_context(patch.object(vision, name, return_value=fields))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_context_field", return_value={}))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_focused_field", return_value={}))
            stack.enter_context(patch.object(rules, "_checklist_read", return_value={}))
            stack.enter_context(patch.object(ac, "_typeahead", return_value=[]))
            stack.enter_context(patch.object(ac, "_fetch_task", return_value=task()))
            result, confidence, ocr = rules.read_and_match(doc, "PM", date(2026, 10, 5))
        self.assertIsNone(result)
        self.assertEqual(0, confidence)
        self.assertEqual(1, ocr["rules_audit"]["equipment_search"]["candidate_count"])
        self.assertEqual("ABC12X3456", ocr["_device_table_audit"]["candidates"][0]["serial"])
        self.assertEqual([], ocr["serial_candidates"])
        self.assertEqual([], ocr["phone_candidates"])
        self.assertFalse(ocr["rules_audit"]["equipment_search"]["model_invoked"])

    def pipeline(self, *, conflict=False, tie=False, model_id="D1", late_different_device=False):
        rows = [device()]
        if tie:
            rows.append(device(gid="102", serial="ABC12X3457"))
        ac.set_device_index(vocabulary_index(rows))
        fields = reading(serial="ABC12X3450" if tie else "ABC12X3456",
                         hospital="Other Hospital" if conflict else "Test Hospital")
        doc = MagicMock()
        doc.__len__.return_value = 4
        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {agent.ENABLED_ENV: "1", config.JOBSHEET_DRY_RUN_ENV: "1"}))
            stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")))
            for name in ("upload", "upload_unique", "delete", "moveto", "run", "run_result"):
                stack.enter_context(patch.object(rclone_helper, name, side_effect=AssertionError("Cloud forbidden")))
            for name in ("ocr_jobsheet_fields", "ocr_jobsheet_identity_fields", "ocr_jobsheet_support_fields"):
                stack.enter_context(patch.object(vision, name, return_value=fields))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_context_field", return_value={}))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_focused_field", return_value={}))
            stack.enter_context(patch.object(rules, "_checklist_read", return_value={}))
            stack.enter_context(patch.object(vision, "crop_jobsheet_field_card", return_value="synthetic-image"))
            model = stack.enter_context(patch.object(vision, "_call_vision", return_value=json.dumps({
                "action": "select_candidate", "candidate_id": model_id, "matched_fields": ["serial"], "conflicting_fields": []})))
            stack.enter_context(patch.object(ac, "_typeahead", return_value=[]))
            stack.enter_context(patch.object(ac, "_fetch_task", side_effect=lambda gid:
                task(gid=gid, serial="ABC12X3457" if gid == "102" else "ABC12X3456", due="2026-08-01" if not conflict and not tie else "2026-09-28")))
            if late_different_device:
                # Isolate the glue: a late deterministic selection must not
                # silently contradict the model's equipment proposal.
                stack.enter_context(patch.object(rules, "select_task", side_effect=[
                    (None, "live_identity_not_confirmed")] * 4 + [(task(serial="ABC12X3457"), "action_date_live_visit_confirmed")]))
            result = rules.read_and_match(doc, "PM", date(2026, 10, 5))
        return result, model.call_count

    def test_actual_model_search_wiring_cannot_override_wrong_hospital(self):
        (selected, _, ocr), calls = self.pipeline(conflict=True)
        self.assertIsNone(selected)
        self.assertEqual(1, calls)
        self.assertEqual("proposal", ocr["_device_table_audit"]["model_search"]["status"])
        self.assertEqual("Other Hospital", ocr["_ocr_audit"]["readings"][0]["raw"]["hospital_raw"])

    def test_model_equipment_proposal_cannot_override_wrong_service_date(self):
        (selected, _, ocr), calls = self.pipeline()
        self.assertIsNone(selected)
        self.assertEqual(1, calls)
        self.assertIn("live_visit_date_conflict", ocr["rules_audit"]["trace"])

    def test_known_device_tie_is_not_sent_to_agent_to_discard_a_rival(self):
        (selected, _, ocr), calls = self.pipeline(tie=True)
        self.assertIsNone(selected)
        self.assertEqual(0, calls)
        self.assertIn("live_devices_tied", ocr["rules_audit"]["trace"])

    def test_late_live_selection_cannot_silently_disagree_with_model_device(self):
        (selected, _, ocr), calls = self.pipeline(late_different_device=True)
        self.assertIsNone(selected)
        self.assertEqual(1, calls)
        self.assertIn("device_search_disagrees", ocr["rules_audit"]["trace"])


if __name__ == "__main__":
    unittest.main()
