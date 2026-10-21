"""评审遗留三条的回归：观测失败不外抛、sidecar 后于 CSV、例外带证据。

- F3：列表观测阶段的异常（尤其 QNHListEvidenceError）不许甩出 main()——
  外抛会跳过收尾（设备还原 + failed 清单），把手机留在改过分辨率/无障碍的
  状态。失败必须走 die()：还原设备、写 failed 清单、按失败退出。
- F5：多 SKU sidecar 描述的是那份 CSV，绝不能先于 CSV 出现（先出现就是
  「凭据指向一份还不存在的 CSV」）。sidecar 落盘必须在 CSV 改名之后。
- F6：manifest 的渠道行缺失例外必须带着凭据（区块状态/原因/详情 XML 证据
  指针）。只留 sku/channel/spec 的重建条目等于把证据丢了。
"""

import csv
import json
import os
import tempfile
import unittest
from unittest import mock

import qnh_capture_common as C
import qnh_observation as OBS
import qnh_product_scan as S


class ObservationFailureHandlingTests(unittest.TestCase):
    """F3：观测失败按失败收口，不把异常甩出 main()。"""

    def call(self, error):
        with mock.patch.object(S, "collect_list_observation", side_effect=error), \
             mock.patch.object(S, "die", side_effect=SystemExit(1)) as die:
            with self.assertRaises(SystemExit):
                S.observe_list_or_die("萌宠专区/猫粮")
        return die.call_args.args[0]

    def test_list_evidence_error_does_not_escape(self):
        message = self.call(OBS.QNHListEvidenceError("第 2 屏与上一屏没有边界重叠"))
        self.assertIn("列表证据复算失败", message)
        self.assertIn("没有边界重叠", message)

    def test_contract_error_does_not_escape(self):
        message = self.call(C.CaptureContractError("列表观测未在150屏内确认读尽"))
        self.assertIn("列表观测失败", message)

    def test_unexpected_error_does_not_escape(self):
        message = self.call(RuntimeError("设备掉线"))
        self.assertIn("列表观测异常", message)
        self.assertIn("RuntimeError", message)

    def test_die_restores_the_device_before_exiting(self):
        # die() 必须先收尾再退出：不收尾就把手机留在改过分辨率/无障碍的状态。
        with mock.patch.object(S, "cleanup") as cleanup, \
             mock.patch("sys.exit", side_effect=SystemExit(1)) as exit_:
            with self.assertRaises(SystemExit):
                S.die("测试失败")
        cleanup.assert_called_once()
        exit_.assert_called_once_with(1)


class SidecarOrderTests(unittest.TestCase):
    """F5：sidecar 在 CSV 落盘之后才写。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.saved = (S.CTX.rows, S.CTX.sku_rows, S.CTX.profile, S.CTX.csv_path,
                      S.CTX.out_dir, S.CTX.run_stamp, S.CTX.details, S.CTX.cards,
                      S.CTX.resumed_cards, S.CTX.resumed_details)
        S.CTX.rows = [[1, "多规格商品", "", "", "", C.MULTISKU_STATUS, 0.0,
                       "10:00:00", "2026-10-21T10:00:00", ""]]
        S.CTX.sku_rows = {1: [{"SKU": "SKU-1", "规格": "100g", "条码": "6900000000001",
                               "SPU": "SPU-1", "_channel_observations": {}}]}
        S.CTX.profile = {"name": "xiniu-changan"}
        S.CTX.csv_path = None
        S.CTX.out_dir = self.directory.name
        S.CTX.run_stamp = "20261021-000000"
        S.CTX.details = {}
        S.CTX.cards = {}
        S.CTX.resumed_cards = {}
        S.CTX.resumed_details = {}

    def tearDown(self):
        (S.CTX.rows, S.CTX.sku_rows, S.CTX.profile, S.CTX.csv_path, S.CTX.out_dir,
         S.CTX.run_stamp, S.CTX.details, S.CTX.cards, S.CTX.resumed_cards,
         S.CTX.resumed_details) = self.saved
        self.directory.cleanup()

    def test_sidecar_is_written_after_the_csv_landed(self):
        seen = {}
        real_atomic = C.atomic_write_json

        def spy(path, value, **kwargs):
            if str(path).endswith(".skus.json"):
                seen["csv_exists"] = os.path.isfile(S.CTX.csv_path)
            return real_atomic(path, value, **kwargs)

        with mock.patch.object(C, "atomic_write_json", side_effect=spy):
            path = S.write_csv()
        self.assertTrue(os.path.isfile(path))
        # sidecar 落盘的那一刻，CSV 必须已经在盘上。
        self.assertTrue(seen.get("csv_exists"))
        self.assertTrue(os.path.isfile(os.path.splitext(path)[0] + ".skus.json"))

    def test_no_sidecar_without_multi_sku_rows(self):
        S.CTX.rows = [[1, "普通商品", "6900000000001", "", "", "成功", 0.0,
                       "10:00:00", "2026-10-21T10:00:00", ""]]
        S.CTX.sku_rows = {}
        path = S.write_csv()
        self.assertFalse(os.path.exists(os.path.splitext(path)[0] + ".skus.json"))

    def test_csv_rows_keep_the_forty_column_contract(self):
        path = S.write_csv()
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(len(rows[0]), len(C.FULL_COLUMNS))
        self.assertEqual(len(rows), 2)


class ManifestCredentialTests(unittest.TestCase):
    """F6：manifest 的渠道行缺失例外带着凭据（不只 sku/channel/spec）。"""

    def manifest_exceptions(self, evidence_pages):
        saved = (S.CTX.rows, S.CTX.sku_rows, S.CTX.profile, S.CTX.observe_exceptions,
                 S.CTX.observation_exceptions, S.CTX.details, S.CTX.cards,
                 S.CTX.resumed_cards, S.CTX.resumed_details, S.CTX.list_evidence)
        observation = {"observation": "row_absent", "section_status": "已上架",
                       "reason": "已观察完整渠道表至其他信息"}
        if evidence_pages:
            observation["evidence_pages"] = evidence_pages
        child = {"SKU": "SKU-1", "规格": "鳕鱼虾味40g", "条码": "6900000000001",
                 "SPU": "SPU-1", "_channel_observations": {"京东到家": observation}}
        S.CTX.rows = [[1, "多规格商品", "", "", "", C.MULTISKU_STATUS, 0.0,
                       "10:00:00", "2026-10-21T10:00:00", ""]]
        S.CTX.sku_rows = {1: [child]}
        S.CTX.profile = {"name": "xiniu-changan"}
        S.CTX.observe_exceptions = True
        S.CTX.observation_exceptions = []
        S.CTX.details = {}
        S.CTX.cards = {}
        S.CTX.resumed_cards = {}
        S.CTX.resumed_details = {}
        S.CTX.list_evidence = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                S.CTX.out_dir = directory
                S.CTX.run_stamp = "20261021-000000"
                S.CTX.csv_path = None
                path = S.write_csv()
                manifest = S._manifest_for(path, "observed_with_exceptions")
        finally:
            (S.CTX.rows, S.CTX.sku_rows, S.CTX.profile, S.CTX.observe_exceptions,
             S.CTX.observation_exceptions, S.CTX.details, S.CTX.cards,
             S.CTX.resumed_cards, S.CTX.resumed_details, S.CTX.list_evidence) = saved
        return manifest["progress"]["exceptions"]

    def test_manifest_exception_keeps_the_evidence_pointer(self):
        pages = [{"path": "/tmp/qnh_detail_1.xml", "sha256": "a" * 64}]
        exceptions = self.manifest_exceptions(pages)
        absent = [item for item in exceptions
                  if item["type"] == "channel_row_absent"]
        self.assertEqual(len(absent), 1)
        self.assertEqual(absent[0]["sku"], "SKU-1")
        self.assertEqual(absent[0]["channel"], "京东到家")
        self.assertEqual(absent[0]["spec"], "鳕鱼虾味40g")
        # 证据不许丢：manifest 单读就要看得出这条「行缺失」凭什么成立。
        self.assertEqual(absent[0]["evidence_pages"], pages)
        self.assertEqual(absent[0]["section_status"], "已上架")
        self.assertTrue(absent[0]["reason"])

    def test_manifest_exception_keeps_the_sidecar_credential_in_sync(self):
        pages = [{"path": "/tmp/qnh_detail_1.xml", "sha256": "a" * 64}]
        exceptions = self.manifest_exceptions(pages)
        absent = [item for item in exceptions
                  if item["type"] == "channel_row_absent"][0]
        # sidecar 里的凭据与 manifest 的例外必须是同一份。
        self.assertEqual(absent["evidence_pages"], pages)
        self.assertEqual(absent["observation"], "row_absent")


if __name__ == "__main__":
    unittest.main()
