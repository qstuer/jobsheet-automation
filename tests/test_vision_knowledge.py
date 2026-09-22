import base64
import json
import unittest
from unittest.mock import patch

from src import asana_index, nvidia_client, processor, vision_experiment, vision_knowledge


def device(serial, product="EPIQ Elite", visits=1):
    ref = {"serial": serial, "product_variant": product, "product_parse_status": "confirmed"}
    return {"serial": serial, "task_refs": [dict(ref) for _ in range(visits)]}


def index(*devices):
    return {"product_parser_version": 1, "devices": list(devices)}


class VisionKnowledgeTests(unittest.TestCase):
    def examples(self):
        return index(*(device(f"US123B456{i}") for i in (7, 8, 9)))

    def test_three_distinct_devices_produce_advisory_pattern_not_full_serial(self):
        result = vision_knowledge.build_knowledge(self.examples())
        pattern = result["common_patterns"][0]
        self.assertEqual(3, pattern["device_count"])
        self.assertEqual("US", pattern["prefix"])
        self.assertEqual("LLDDDLDDDD", pattern["layout"])
        self.assertEqual({"1": "U", "2": "S", "6": "B"}, pattern["fixed_letters"])
        for serial in ("US123B4567", "US123B4568", "US123B4569"):
            self.assertNotIn(serial, json.dumps(result))

    def test_repeat_visits_and_duplicate_rows_never_reach_minimum(self):
        result = vision_knowledge.build_knowledge(index(device("US123B4567", visits=30), device("US123B4567")))
        self.assertEqual([], result["common_patterns"])
        self.assertEqual(1, result["rare_patterns"][0]["device_count"])

    def test_hold_out_removes_every_visit_and_can_demote_pattern_to_rare(self):
        source = self.examples()
        source["devices"].append(device("US123B4567", visits=20))
        result = vision_knowledge.build_knowledge(source, excluded_serials=["US123B4567"])
        self.assertEqual([], result["common_patterns"])
        self.assertEqual(2, result["rare_patterns"][0]["device_count"])
        self.assertEqual([], vision_knowledge.prompt_reference(result)["common_serial_formats"])

    def test_legacy_index_is_refused_not_silently_learned(self):
        with self.assertRaises(ValueError):
            vision_knowledge.build_knowledge({"schema_version": 3, "devices": []})

    def test_product_conflict_even_across_duplicate_serial_rows_is_excluded(self):
        result = vision_knowledge.build_knowledge(index(device("US123B4567"), device("US123B4567", "Affiniti 70")))
        self.assertEqual([], result["models"])
        self.assertEqual(1, result["excluded_reason_counts"]["unconfirmed_or_conflicting_product"])

    def test_unknown_product_and_wrong_ref_serial_are_excluded(self):
        for value in ("EPIQ Elite G", "HAWO 12345678"):
            result = vision_knowledge.build_knowledge(index(device("US123B4567", value)))
            self.assertEqual([], result["models"])
        row = device("US123B4567")
        row["task_refs"][0]["serial"] = "US123B4568"
        self.assertEqual([], vision_knowledge.build_knowledge(index(row))["models"])

    def test_only_vetted_projection_reaches_prompt(self):
        knowledge = vision_knowledge.build_knowledge(self.examples())
        knowledge["secret_task_answer"] = "PRIVATEANSWER"
        knowledge["models"].append({"name": "INJECTED INSTRUCTION"})
        knowledge["common_patterns"][0]["notes"] = "PRIVATEANSWER"
        knowledge["common_patterns"][0]["fixed_letters"]["3"] = "1"
        projection = vision_knowledge.prompt_reference(knowledge)
        self.assertNotIn("PRIVATEANSWER", json.dumps(projection))
        self.assertNotIn("INJECTED", json.dumps(projection))
        self.assertNotIn("3", projection["common_serial_formats"][0]["fixed_letters"])

    def test_pattern_limit_is_bounded(self):
        knowledge = vision_knowledge.build_knowledge(self.examples())
        knowledge["common_patterns"] *= 100
        self.assertEqual(40, len(vision_knowledge.prompt_reference(knowledge)["common_serial_formats"]))

    def test_uncertain_character_is_not_deleted_into_valid_matching_serial(self):
        value = "US123B45?7"
        reading = nvidia_client._normalize_ocr_data({"serial_candidates": [value]})
        self.assertEqual([], reading["serial_candidates"])
        self.assertEqual([], reading["serial_visual_candidates"])
        self.assertEqual([value], reading["_ocr_audit"]["raw"]["serial_candidates"])
        self.assertEqual([], processor._near_serial_consensus([
            {"serial_candidates": [value]}, {"serial_candidates": [value]}]))

    def test_joint_reader_keeps_transcription_and_assistance_separate(self):
        raw = {"product_raw": "EPIQ", "serial_candidates": ["US123B45?7"], "hospital_raw": "QMH"}
        aided = {**raw, "serial_candidates": ["US123B4567"]}
        reply = json.dumps({"transcription": raw, "assisted": aided, "relation": "uncertain"})
        with patch.object(nvidia_client, "_call_vision_once", return_value=reply) as call:
            result = nvidia_client.read_joint_identity_image("image", vision_knowledge.build_knowledge(self.examples()))
        self.assertEqual([], result["transcription"]["serial_candidates"])
        self.assertEqual(["US123B4567"], result["assisted"]["serial_candidates"])
        self.assertIn("Clear strokes override", call.call_args.args[0])
        self.assertIn("known_products", call.call_args.args[0])
        self.assertNotIn("US123B4567", call.call_args.args[0])
        call.assert_called_once()

    def test_invalid_joint_reply_is_rejected_not_filled_from_knowledge(self):
        with patch.object(nvidia_client, "_call_vision_once", return_value=json.dumps({
                "transcription": {}, "assisted": None, "relation": "consistent"})):
            with self.assertRaises(nvidia_client.NvidiaResponseError):
                nvidia_client.read_joint_identity_image("image")

    def test_pair_uses_identical_pixels_and_can_counterbalance_order(self):
        image = base64.b64encode(b"same pixels").decode()
        knowledge = vision_knowledge.build_knowledge(self.examples())
        for guided_first in (False, True):
            with patch.object(nvidia_client, "crop_jobsheet_field_card", return_value=image) as crop, \
                 patch.object(nvidia_client, "read_joint_identity_image", return_value={"synthetic": True}) as read:
                result = vision_experiment.compare(None, knowledge, guided_first=guided_first)
            crop.assert_called_once()
            self.assertEqual(2, read.call_count)
            self.assertTrue(all(call.args[0] == image for call in read.call_args_list))
            self.assertEqual(knowledge if guided_first else None, read.call_args_list[0].args[1])
            self.assertEqual("NOT_SCORED", result["accuracy"])
            self.assertTrue(result["both_read"])

    def test_failed_arm_is_not_retried_or_leaked(self):
        image = base64.b64encode(b"same pixels").decode()
        with patch.object(nvidia_client, "crop_jobsheet_field_card", return_value=image), \
             patch.object(nvidia_client, "read_joint_identity_image", side_effect=[RuntimeError("PRIVATEANSWER"), {}]) as read:
            result = vision_experiment.compare(None, vision_knowledge.build_knowledge(self.examples()))
        self.assertEqual(2, read.call_count)
        self.assertFalse(result["both_read"])
        self.assertNotIn("PRIVATEANSWER", json.dumps(result))

    def test_cli_holdout_proof_is_bound_to_the_pdf_not_filename(self):
        knowledge = {"held_out_pdf_sha256": "abc", "device_holdout_applied": True}
        vision_experiment.validate_holdout(knowledge, "abc")
        for payload, digest in ((knowledge, "changed"), ({}, "abc"),
                                ({**knowledge, "device_holdout_applied": False}, "abc")):
            with self.assertRaises(ValueError):
                vision_experiment.validate_holdout(payload, digest)


if __name__ == "__main__":
    unittest.main()
