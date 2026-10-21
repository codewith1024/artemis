"""覆盖证明（coverage）门禁回归测试。

2026-10-21 决策：coverage 的卡片集合按「规格+标题」去重，同名同规格的两张卡
（不同条码）会塌成一张，set 相等不等于张数相等。因此「列表读尽证明」暂不被
承认：平台计数高于实抓行数时一律失败（宁可重采），coverage_allows_shortfall
固定返回 False。短收不再被任何 set 口径的证明放行。
"""

import csv
import os
import tempfile
import unittest
import uuid

import qnh_capture_common as C
import qnh_product_scan as scan
from tests.unit.test_qnh_manifest_pipeline import complete_row


def coverage(list_exhausted=True, rendered_cards=1, captured_cards=1,
             cards_match=True, unresolved=(), platform_title_count=2):
    """旧口径的覆盖证明形状（集合相等即读尽）——现在一律不被承认。"""
    return {
        "list_exhausted": list_exhausted,
        "end_marker": scan.LIST_END_MARKER,
        "rendered_cards": rendered_cards,
        "captured_cards": captured_cards,
        "cards_match": cards_match,
        "unresolved_specs": list(unresolved),
        "platform_title_count": platform_title_count,
    }


class CoverageGateTests(unittest.TestCase):
    def make_capture(self, directory, rows, *, expected_count, coverage_block=None):
        prefix = os.path.join(directory, "qnh_scan_20261020-100000")
        csv_path = prefix + ".csv"
        for row in rows:
            row["证据前缀"] = prefix
            # 不同商品给不同 SKU（同名商品共用）：合成数据全用 SKU-1 会造出假的
            # 身份冲突。
            row["SKU"] = "SKU-" + C.text_value(row.get("商品名称"))
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()),
            state="success",
            profile={"name": "test", "store_name": "测试店", "warehouse_id": "1",
                     "tenant_id": "2", "store_code": "qnh_test", "role": "competitor"},
            device_id="device",
            scope={"list_title": "猫粮", "category": "萌宠专区/猫粮",
                   "expected_product_count": expected_count, "requested_count": 0},
            capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                     "run_started_at": "2026-10-09T10:00:00",
                     "finished_at": "2026-10-09T10:01:00", "collector_version": "test",
                     "pace": "ultra", "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix, "video_path": None,
                       "csv_sha256": C.sha256_file(csv_path)},
            rows=rows, coverage=coverage_block)
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest, overwrite=False)
        return manifest_path

    def test_shortfall_fails_even_with_list_proof(self):
        # 列表确实读尽、渲染卡片全部采集——但 set 口径无法排除同名同规格塌缩，
        # 短收一律失败（宁可重采）。
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_capture(
                directory, [complete_row(1, "商品A", "6900000000001")],
                expected_count=2, coverage_block=coverage())
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("商品数不完整" in e for e in report.errors), report.errors)

    def test_shortfall_fails_without_coverage_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_capture(
                directory, [complete_row(1, "商品A", "6900000000001")],
                expected_count=2)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("商品数不完整" in e for e in report.errors), report.errors)

    def test_shortfall_fails_when_a_rendered_card_was_not_captured(self):
        # 渲染 2 张只采到 1 张 = 真漏抓，证明必须被拒。
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_capture(
                directory, [complete_row(1, "商品A", "6900000000001")],
                expected_count=2, coverage_block=coverage(rendered_cards=2,
                                                          captured_cards=1,
                                                          cards_match=False))
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("商品数不完整" in e for e in report.errors), report.errors)

    def test_shortfall_fails_when_a_card_could_never_be_parsed(self):
        # 见过规格却一直没配到标题 = 那张卡片被漏掉了。
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_capture(
                directory, [complete_row(1, "商品A", "6900000000001")],
                expected_count=2, coverage_block=coverage(unresolved=["规格:深海胶原 1.5kg"]))
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("商品数不完整" in e for e in report.errors), report.errors)

    def test_shortfall_fails_when_list_not_proved_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_capture(
                directory, [complete_row(1, "商品A", "6900000000001")],
                expected_count=2, coverage_block=coverage(list_exhausted=False))
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)

    def test_coverage_must_match_the_manifest_platform_count(self):
        # 别的分片的证明不能拿来顶数。
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_capture(
                directory, [complete_row(1, "商品A", "6900000000001")],
                expected_count=3, coverage_block=coverage(platform_title_count=2))
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)

    def test_more_rows_than_platform_count_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_capture(
                directory, [complete_row(1, "商品A", "6900000000001"),
                            complete_row(2, "商品B", "6900000000002")],
                expected_count=1, coverage_block=coverage(rendered_cards=2,
                                                          captured_cards=2,
                                                          platform_title_count=1))
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("超过平台总数" in e for e in report.errors), report.errors)

    def test_segment_aggregate_rejects_short_segment_even_with_proof(self):
        # 聚合清单里的分片缺量：set 口径的证明也不再放行。
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_capture(
                directory, [complete_row(1, "商品A", "6900000000001")],
                expected_count=2, coverage_block=coverage())
            manifest = C.read_json(path)
            manifest["scope"] = {
                "mode": C.SEGMENT_MODE,
                "list_title": "全部商品",
                "category": None,
                "expected_product_count": 1,
                "requested_count": 0,
                "raw_segment_product_count": 1,
                "cross_category_duplicate_count": 0,
                "segments": [
                    {"order": 149, "top": "萌宠专区", "leaf": "猫粮",
                     "expected_count": 2, "captured_count": 1, "unique_count": 1,
                     "duplicate_count": 0, "state": "success", "row_start": 1,
                     "row_end": 1, "coverage": coverage()},
                ],
            }
            C.atomic_write_json(path, manifest)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("数量不完整" in e for e in report.errors), report.errors)


class CoverageProofApiTests(unittest.TestCase):
    """coverage_allows_shortfall 的契约：任何形状都不放行短收。"""

    def test_snapshot_shape_never_authorizes_shortfall(self):
        snapshot = {
            "list_exhausted": True, "end_marker": "已加载全部数据",
            "rendered_cards": 1, "captured_cards": 1, "cards_match": True,
            "unresolved_specs": [], "platform_title_count": 2,
        }
        self.assertFalse(C.coverage_allows_shortfall(snapshot, 2))
        self.assertFalse(C.coverage_allows_shortfall({}, 2))
        self.assertFalse(C.coverage_allows_shortfall(None, 2))
        self.assertFalse(C.coverage_allows_shortfall(
            {"list_exhausted": True, "cards_match": True, "unresolved_specs": [],
             "platform_title_count": 2}, 2))


class RenderedCardCollectionTests(unittest.TestCase):
    def setUp(self):
        scan.CTX.parsed_cards = set()
        scan.CTX.orphan_specs = set()
        scan.CTX.list_end_marker = False
        scan.CTX.target_count = 2

    def test_end_marker_is_collected(self):
        scan.note_list_end_marker([{"text": scan.LIST_END_MARKER},
                                   {"text": "月售：4"}])
        self.assertTrue(scan.CTX.list_end_marker)
        scan.CTX.list_end_marker = False
        scan.note_list_end_marker([{"text": "月售：4"}])
        self.assertFalse(scan.CTX.list_end_marker)


class CarryRowGateTests(unittest.TestCase):
    """携带行的详情是历史合成值：完整入库必须拒绝，partial 门禁放行。"""

    def make_capture(self, directory, rows, *, expected_count):
        prefix = os.path.join(directory, "qnh_scan_20261020-100000")
        csv_path = prefix + ".csv"
        for row in rows:
            row["证据前缀"] = prefix
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()),
            state="success",
            profile={"name": "test", "store_name": "测试店", "warehouse_id": "1",
                     "tenant_id": "2", "store_code": "qnh_test", "role": "competitor"},
            device_id="device",
            scope={"list_title": "猫粮", "category": "萌宠专区/猫粮",
                   "expected_product_count": expected_count, "requested_count": 0},
            capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                     "run_started_at": "2026-10-09T10:00:00",
                     "finished_at": "2026-10-09T10:01:00", "collector_version": "test",
                     "pace": "ultra", "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix, "video_path": None,
                       "csv_sha256": C.sha256_file(csv_path)},
            rows=rows)
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest, overwrite=False)
        return manifest_path

    def test_complete_gate_rejects_carry_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            row = complete_row(1, "商品A", "6900000000001")
            row["状态"] = C.CARRY_ROW_STATUS
            path = self.make_capture(directory, [row], expected_count=1)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("携带行" in e for e in report.errors), report.errors)

    def test_partial_gate_allows_carry_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            row = complete_row(1, "商品A", "6900000000001")
            row["状态"] = C.CARRY_ROW_STATUS
            path = self.make_capture(directory, [row], expected_count=1)
            report = C.validate_manifest(path, require_complete=False)
            self.assertTrue(report.ok, report.errors)


if __name__ == "__main__":
    unittest.main()
