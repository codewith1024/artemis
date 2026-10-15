"""Tests for leaf-category shard aggregation."""

import csv
import json
import os
import tempfile
import time
import unittest
import uuid
from unittest import mock

import qnh_capture_common as C
import qnh_category_pipeline as category_pipeline
from tests.unit.test_qnh_manifest_pipeline import complete_row


class CategoryAggregationTests(unittest.TestCase):
    def make_segment(self, root, order, top, leaf, rows):
        directory = os.path.join(root, f"{order:03d}")
        os.makedirs(directory)
        prefix = os.path.join(directory, "qnh_scan_segment")
        csv_path = prefix + ".csv"
        for index, row in enumerate(rows, 1):
            row["序号"] = str(index)
            row["证据前缀"] = prefix
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        profile = C.load_profile("xiniu-changan")
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()),
            state="success", profile=profile, device_id="R28M31LJX0E",
            scope={"list_title": leaf, "category": f"{top}/{leaf}",
                   "expected_product_count": len(rows), "requested_count": 0},
            capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                     "run_started_at": "2026-10-09T10:00:00",
                     "finished_at": "2026-10-09T10:01:00",
                     "collector_version": "test", "pace": "ultra",
                     "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False,
                     "dismiss_order_dialogs": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix,
                       "video_path": None, "csv_sha256": C.sha256_file(csv_path)},
            rows=rows)
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest)
        return manifest_path

    def test_merge_deduplicates_cross_category_and_maps_primary_category(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.make_segment(
                root, 1, "休闲零食", "薯片薯条",
                [complete_row(1, "商品A", "6900000000001"),
                 complete_row(2, "商品B", "6900000000002")])
            second = self.make_segment(
                root, 2, "精选业务", "精选业务",
                [complete_row(1, "商品B", "6900000000002")])
            state = {
                "capture_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
                "dismiss_order_dialogs": False,
                "completed": {
                    "1": {"manifest_path": first},
                    "2": {"manifest_path": second},
                },
            }
            state_path = os.path.join(root, "capture.state.json")
            C.atomic_write_json(state_path, state)
            inventory = {"full_store_count": 2}
            segments = [
                {"order": 1, "top": "休闲零食", "leaf": "薯片薯条", "expected_count": 2},
                {"order": 2, "top": "精选业务", "leaf": "精选业务", "expected_count": 1},
            ]
            manifest_path = category_pipeline.merge_segments(
                root, inventory, segments, state_path)
            report = C.validate_manifest(
                manifest_path, require_complete=True,
                expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.row_count, 2)
            scope = report.manifest["scope"]
            self.assertEqual(scope["raw_segment_product_count"], 3)
            self.assertEqual(scope["cross_category_duplicate_count"], 1)
            self.assertEqual(C.row_category_map(report.manifest, 2),
                             [("休闲零食", "薯片薯条"),
                              ("休闲零食", "薯片薯条")])

    def test_merge_preserves_duplicate_barcode_occurrences_within_leaf(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_segment(
                root, 1, "休闲零食", "薯片薯条",
                [complete_row(1, "规格A", "6900000000001"),
                 complete_row(2, "规格B", "6900000000001")])
            state_path = os.path.join(root, "capture.state.json")
            C.atomic_write_json(state_path, {
                "capture_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
                "completed": {"1": {"manifest_path": manifest}},
            })
            output = category_pipeline.merge_segments(
                root, {"full_store_count": 2},
                [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                  "expected_count": 2}], state_path)
            report = C.validate_manifest(output, require_complete=True,
                                         expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual([row["商品名称"] for row in report.rows], ["规格A", "规格B"])

    def test_merge_keeps_same_barcode_different_specs_across_leaves(self):
        with tempfile.TemporaryDirectory() as root:
            first_row = complete_row(1, "同条码规格A", "6900000000001")
            second_row = complete_row(1, "同条码规格B", "6900000000001")
            first_row["规格"] = "100g"
            second_row["规格"] = "200g"
            first = self.make_segment(root, 1, "食品", "规格A", [first_row])
            second = self.make_segment(root, 2, "食品", "规格B", [second_row])
            state_path = os.path.join(root, "capture.state.json")
            C.atomic_write_json(state_path, {
                "capture_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
                "completed": {"1": {"manifest_path": first},
                              "2": {"manifest_path": second}},
            })
            output = category_pipeline.merge_segments(
                root, {"full_store_count": 2},
                [{"order": 1, "top": "食品", "leaf": "规格A", "expected_count": 1},
                 {"order": 2, "top": "食品", "leaf": "规格B", "expected_count": 1}],
                state_path)
            report = C.validate_manifest(output, require_complete=True,
                                         expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual([row["规格"] for row in report.rows], ["100g", "200g"])

    def test_merge_collapses_blank_spec_twin_with_same_sku(self):
        with tempfile.TemporaryDirectory() as root:
            concrete = complete_row(1, "同一商品", "6900000000001")
            blank = complete_row(2, "同一商品", "6900000000001")
            concrete["规格"] = "100g"
            blank["规格"] = "无"
            blank["SKU"] = concrete["SKU"]
            manifest = self.make_segment(
                root, 1, "食品", "测试类目", [concrete, blank])
            state_path = os.path.join(root, "capture.state.json")
            C.atomic_write_json(state_path, {
                "capture_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
                "completed": {"1": {"manifest_path": manifest}},
            })
            output = category_pipeline.merge_segments(
                root, {"full_store_count": 1},
                [{"order": 1, "top": "食品", "leaf": "测试类目",
                  "expected_count": 2}], state_path)
            report = C.validate_manifest(
                output, require_complete=True,
                expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.row_count, 1)
            self.assertEqual(report.rows[0]["规格"], "100g")

    def test_merge_collapses_different_blank_spec_renderings(self):
        first = complete_row(1, "同一商品", "6900000000001")
        second = complete_row(2, "同一商品", "6900000000001")
        first["规格"] = ""
        second["规格"] = "无"
        second["SKU"] = first["SKU"]
        self.assertEqual(
            category_pipeline._redundant_spec_row_indexes([first, second]), {0})

    def test_merge_keeps_blank_spec_when_sku_differs(self):
        with tempfile.TemporaryDirectory() as root:
            concrete = complete_row(1, "同一商品", "6900000000001")
            blank = complete_row(2, "同一商品", "6900000000001")
            concrete["规格"] = "100g"
            blank["规格"] = ""
            blank["SKU"] = "OTHER-SKU"
            manifest = self.make_segment(
                root, 1, "食品", "测试类目", [concrete, blank])
            state_path = os.path.join(root, "capture.state.json")
            C.atomic_write_json(state_path, {
                "capture_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
                "completed": {"1": {"manifest_path": manifest}},
            })
            output = category_pipeline.merge_segments(
                root, {"full_store_count": 2},
                [{"order": 1, "top": "食品", "leaf": "测试类目",
                  "expected_count": 2}], state_path)
            report = C.validate_manifest(
                output, require_complete=True,
                expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.row_count, 2)

    def test_merge_records_realtime_count_difference_without_rejecting_union(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_segment(
                root, 1, "休闲零食", "薯片薯条",
                [complete_row(1, "商品A", "6900000000001")])
            state_path = os.path.join(root, "capture.state.json")
            C.atomic_write_json(state_path, {
                "capture_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
                "completed": {"1": {"manifest_path": manifest}},
            })
            output = category_pipeline.merge_segments(
                root, {"full_store_count": 2},
                [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                  "expected_count": 1}], state_path)
            report = C.validate_manifest(
                output, require_complete=True,
                expected_profile=C.load_profile("xiniu-changan"))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.expected_count, 1)
            self.assertEqual(report.manifest["scope"]["realtime_full_store_count"], 2)
            self.assertEqual(report.manifest["scope"]["realtime_count_difference"], -1)
            self.assertIn("时间窗并集", report.manifest["scope"]["count_note"])


class SupervisorProgressTests(unittest.TestCase):
    """2026-10-12 审计修复回归：停滞判定按有效完成行数，不按 mtime。"""

    SEGMENT = {"order": 44, "top": "雪糕冰品", "leaf": "雪糕",
               "expected_count": 182}

    def write_csv(self, directory, name, statuses):
        path = os.path.join(directory, name)
        rows = []
        for index, status in enumerate(statuses, 1):
            row = complete_row(index, f"商品{index}", f"69000000000{index:02d}")
            row["状态"] = status
            if status == "未打开":
                for column in C.DETAIL_COLUMNS:
                    row[column] = ""
            row["证据前缀"] = os.path.splitext(path)[0]
            rows.append(row)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_attempt_progress_counts_only_done_rows_after_start(self):
        with tempfile.TemporaryDirectory() as directory:
            old = self.write_csv(directory, "qnh_scan_old.csv", ["成功"] * 3)
            started_after = os.path.getmtime(old)
            new = self.write_csv(directory, "qnh_scan_new.csv",
                                 ["成功", "未打开", "自建商品"])
            # 同秒 mtime 粒度太粗：把新 CSV 推到 started_after 之后，
            # 模拟"当前 attempt 的产物"。
            future = time.time() + 10
            os.utime(new, (future, future))
            self.assertEqual(
                category_pipeline._attempt_progress(directory, started_after),
                (future, 2))

    def make_segment(self, root, *, rows, name="qnh_scan_segment", state=None,
                     expected_count=None, stop_reason=""):
        directory = os.path.join(root, "044")
        os.makedirs(directory, exist_ok=True)
        prefix = os.path.join(directory, name)
        csv_path = prefix + ".csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            if rows:
                for index, row in enumerate(rows, 1):
                    row["序号"] = str(index)
                    row["证据前缀"] = prefix
                writer.writerows(rows)
        profile = C.load_profile("xiniu-changan")
        segment = self.SEGMENT
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()), state=state or ("success" if rows else "failed"),
            profile=profile, device_id="R28M31LJX0E",
            scope={"list_title": segment["leaf"],
                   "category": f"{segment['top']}/{segment['leaf']}",
                   "expected_product_count": (expected_count if expected_count is not None
                                              else len(rows or [])),
                   "requested_count": 0},
            capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                     "run_started_at": "2026-10-09T10:00:00",
                     "finished_at": "2026-10-09T10:01:00",
                     "collector_version": "test", "pace": "ultra",
                     "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False,
                     "dismiss_order_dialogs": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix,
                       "video_path": None, "csv_sha256": C.sha256_file(csv_path)},
            rows=rows or [], stop_reason=stop_reason)
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest)
        return manifest_path

    def test_newest_resumable_segment_backtracks_past_failures(self):
        # 复现 shard44 断点丢失：最新 5 份全是无效工件，唯一有效断点排在
        # 第 6 旧。全量回溯必须仍能找到它；cap=5 的旧实现返回 None。
        with tempfile.TemporaryDirectory() as root:
            good = self.make_segment(
                root, rows=[complete_row(1, "商品A", "6900000000001")],
                name="qnh_scan_20261012-173614_f8b862e3")
            os.utime(good, (time.time() - 1000, time.time() - 1000))
            for index in range(6):
                self.make_segment(
                    root, rows=None,
                    name=f"qnh_scan_20261012-18{index:02d}00_{uuid.uuid4().hex[:8]}")
            directory = os.path.join(root, "044")
            found = category_pipeline.newest_resumable_segment(
                directory, self.SEGMENT)
            self.assertEqual(found, good)

    def test_promotes_two_complete_bottom_passes_with_small_stable_shortfall(self):
        with tempfile.TemporaryDirectory() as root:
            rows = [complete_row(i, f"商品{i}", f"6900000000{i:03d}")
                    for i in range(1, 181)]
            first = self.make_segment(
                root, rows=[dict(row) for row in rows], name="qnh_scan_first",
                state="partial", expected_count=182,
                stop_reason=category_pipeline.BOTTOM_STOP_REASON)
            second = self.make_segment(
                root, rows=[dict(row) for row in rows], name="qnh_scan_second",
                state="interrupted", expected_count=182,
                stop_reason=category_pipeline.BOTTOM_STOP_REASON)
            promoted = category_pipeline.promote_stable_segment(
                os.path.join(root, "044"), self.SEGMENT)
            self.assertIsNotNone(promoted)
            path, report = promoted
            self.assertEqual(report.row_count, 180)
            self.assertEqual(report.manifest["state"], "success")
            self.assertEqual(report.manifest["scope"]["expected_product_count"], 180)
            self.assertEqual(C.read_json(first)["state"], "partial")
            self.assertEqual(C.read_json(second)["state"], "interrupted")
            self.assertTrue(category_pipeline.validate_segment(
                path, {**self.SEGMENT, "expected_count": 180}).ok)

    def test_stable_ceiling_rejects_single_bottom_pass(self):
        with tempfile.TemporaryDirectory() as root:
            rows = [complete_row(i, f"商品{i}", f"6900000000{i:03d}")
                    for i in range(1, 181)]
            self.make_segment(
                root, rows=rows, state="partial", expected_count=182,
                stop_reason=category_pipeline.BOTTOM_STOP_REASON)
            self.assertIsNone(category_pipeline.stable_list_ceiling(
                os.path.join(root, "044"), self.SEGMENT))

    def test_stable_ceiling_rejects_shortfall_over_two(self):
        with tempfile.TemporaryDirectory() as root:
            rows = [complete_row(i, f"商品{i}", f"6900000000{i:03d}")
                    for i in range(1, 180)]
            for name in ("qnh_scan_first", "qnh_scan_second"):
                self.make_segment(
                    root, rows=[dict(row) for row in rows], name=name,
                    state="partial", expected_count=182,
                    stop_reason=category_pipeline.BOTTOM_STOP_REASON)
            self.assertIsNone(category_pipeline.stable_list_ceiling(
                os.path.join(root, "044"), self.SEGMENT))


if __name__ == "__main__":
    unittest.main()
