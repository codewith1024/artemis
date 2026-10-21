"""Capture-only multi-SKU persistence without changing card-count semantics."""
import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import qnh_capture_common as C
import qnh_product_scan as S
import qnh_sku_capture as K

FIXTURE = Path(__file__).parents[1] / "fixtures/qnh_multispec_detail.xml"
TITLE = "含苞牡丹仿真花芍药室内家居摆件 1束 婚庆手捧花野餐冰箱客厅桌面装饰"
SPEC = "粉色牡丹一束（5朵花），香槟色牡丹一束（一朵花）"


class SkuCaptureIntegrationTests(unittest.TestCase):
    def test_visit_multi_sku_keeps_one_card_and_two_independent_details(self):
        nodes = S.parse_nodes(FIXTURE.read_text(encoding="utf-8"))
        prod = {"title": TITLE, "barcode": "", "spec": SPEC}
        with mock.patch.object(S.CTX, "rows", []), \
             mock.patch.object(S.CTX, "sku_rows", {}), \
             mock.patch.object(S.CTX, "row_product_ids", {}), \
             mock.patch.object(S.CTX, "last_detail_nodes", nodes), \
             mock.patch.object(S, "open_detail", return_value=True), \
             mock.patch.object(S, "wait_while_dialog"), \
             mock.patch.object(S, "tap_back"), \
             mock.patch.object(S, "recover_list_child_after_back"), \
             mock.patch.object(S, "collect_sku_detail", return_value=K.parse_sku_details(nodes)), \
             mock.patch.object(S.time, "sleep"), \
             mock.patch.object(S.CTX, "out_dir", "/tmp"), \
             mock.patch.object(S.CTX, "run_stamp", "sku-test"):
            S.visit_product(1, prod, 0, None, 1080, 4560, S.PACING["ultra"], True, 8)
            self.assertEqual(len(S.CTX.rows), 1)
            self.assertEqual(S.CTX.rows[0][5], "多SKU")
            variants = S.CTX.sku_rows[1]
            self.assertEqual(len(variants), 2)
            self.assertEqual([r["SKU"] for r in variants],
                             ["2064972073662615613", "2064972073664102474"])
            self.assertNotEqual(variants[0]["规格"], variants[1]["规格"])

    def test_capture_only_runs_local_sidecar_gate_without_database(self):
        import qnh_capture_pipeline as pipeline
        for valid in (True, False):
            gate = C.GateReport(ok=valid)
            if not valid:
                gate.fail("sidecar hash mismatch")
            with mock.patch("sys.argv", ["qnh_capture_pipeline.py", "--profile", "xiniu-changan",
                                         "--from-manifest", "/tmp/test.manifest.json", "--capture-only"]), \
                 mock.patch.object(pipeline.C, "validate_manifest", return_value=gate) as validate, \
                 mock.patch.object(pipeline.loader, "_open_connection", side_effect=AssertionError("DB called")) as connect:
                if valid:
                    pipeline.main()
                else:
                    with self.assertRaises(SystemExit):
                        pipeline.main()
                connect.assert_not_called()
                self.assertFalse(validate.call_args.kwargs["require_complete"])

    def test_sidecar_preserves_per_sku_fields_and_roundtrips_resume(self):
        nodes = S.parse_nodes(FIXTURE.read_text(encoding="utf-8"))
        variants = K.parse_sku_details(nodes)
        with tempfile.TemporaryDirectory() as directory:
            prefix = str(Path(directory) / "qnh_scan_20261021-120000")
            row = [1, TITLE, "", "", "", "多SKU", "", "12:00:00",
                   "2026-10-21T12:00:00", prefix]
            card = [SPEC, "0", "2", "否", "否", "14.50", "14.50", "14.50", "5/件", ""]
            key = C.row_identity(1, TITLE, "")
            with mock.patch.object(S.CTX, "rows", [row]), \
                 mock.patch.object(S.CTX, "sku_rows", {1: variants}), \
                 mock.patch.object(S.CTX, "details", {}), \
                 mock.patch.object(S.CTX, "resumed_details", {}), \
                 mock.patch.object(S.CTX, "carry_details", {}), \
                 mock.patch.object(S.CTX, "profile", C.load_profile("xiniu-changan")), \
                 mock.patch.object(S.CTX, "csv_path", None), \
                 mock.patch.object(S.CTX, "out_dir", directory), \
                 mock.patch.object(S.CTX, "run_stamp", "20261021-120000"), \
                 mock.patch.object(S, "card_cols", return_value=card):
                path = S.write_csv()
                payload = C.read_json(C.source_prefix(path) + ".skus.json")
                children = payload["listings"]["1"]
                self.assertEqual(len(children), 2)
                self.assertEqual(children[0]["美团闪购库存"], "1")
                self.assertEqual(children[1]["美团闪购库存"], "1")
                with open(path, encoding="utf-8-sig", newline="") as fh:
                    cards = list(csv.DictReader(fh))
                self.assertEqual(len(cards), 1)
                self.assertTrue(all(cards[0][col] == "" for col in C.DETAIL_COLUMNS))
                manifest = C.build_manifest(
                    capture_id="00000000-0000-0000-0000-000000000001",
                    batch_id="00000000-0000-0000-0000-000000000002",
                    run_id="00000000-0000-0000-0000-000000000003",
                    state="success", profile=S.CTX.profile, device_id="test",
                    scope={"sku_mode": "sidecar_v1", "list_title": "测试", "category": "测试/测试",
                           "expected_product_count": 1, "requested_count": 0},
                    capture={"method": "app_ui", "started_at": "2026-10-21T12:00:00",
                             "run_started_at": "2026-10-21T12:00:00", "finished_at": "2026-10-21T12:01:00",
                             "collector_version": "test", "pace": "ultra", "tall_screen": True,
                             "verify_every": 1, "checkpoint_every": 1, "record": False},
                    artifacts={"csv_path": path, "csv_sha256": C.sha256_file(path),
                               "evidence_prefix": prefix, "video_path": None,
                               "sku_path": prefix + ".skus.json", "sku_sha256": C.sha256_file(prefix + ".skus.json")},
                    rows=cards)
                manifest["progress"].update(listing_count=1, sku_row_count=2)
                C.atomic_write_json(prefix + ".manifest.json", manifest)
                gate = C.validate_manifest(prefix + ".manifest.json", require_complete=True)
                self.assertTrue(gate.ok, gate.errors)
                S.load_resume(path, strict_manifest=True)
                self.assertEqual(S.CTX.resumed, 1)
                self.assertEqual(len(S.CTX.sku_rows[1]), 2)
                self.assertEqual(S.CTX.sku_rows[1][1]["SKU"], "2064972073664102474")


if __name__ == "__main__":
    unittest.main()
