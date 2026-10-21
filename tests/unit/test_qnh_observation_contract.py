"""观测契约的两条边界：契约标记只给真有观测内容的产物；升级到观测终态要
身份对得上，不是张数对得上。

- F2：`capture.observation_contract='v1'` 只写在真的带观测内容（逐条例外）
  的 manifest 上。观测模式跑出的普通成功产物不挂契约——挂了就是「声称在
  观测契约下」却拿不出观测内容，入库入口也会被它无谓地拦下。
- F4：扫描器决定「是否升级到观测终态」时，比的是卡片身份多重集，不是张数。
  漏一张又错收一张，张数照样相等，但采到的不是这一页的东西——那种情况必须
  落 partial，不许声称观测完成。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import qnh_capture_common as C
import qnh_product_scan as S

CAPTURE_TIME = "2026-10-21T10:00:00"


def single_row(index=1, name="商品A", spec="100g", barcode="6900000000001"):
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": barcode,
        "店内码": "A-1", "货号": "H-1", "状态": "成功", "视频时间点": "",
        "时刻": "10:00:00", "采集时间": CAPTURE_TIME, "证据前缀": "",
        "规格": spec, "月售": "10", "库存": "2", "售罄": "否", "活动品": "否",
        "美团闪购价": "10", "饿了么价": "10", "京东到家价": "10",
        "采购价": "5/袋", "价格原文": "10 | 10 | 10 | 5/袋",
        "开放平台标识": "OP-1", "SPU": "SPU-1", "SKU": "SKU-1", "重量": "100克(g)",
        "采购价_详情": "5", "采购单位_详情": "袋",
        "美团闪购状态": "已上架", "美团闪购零售价": "10", "美团闪购库存": "2",
        "饿了么状态": "已下架", "饿了么零售价": "10", "饿了么库存": "1",
        "京东到家状态": "未上线", "京东到家零售价": "10", "京东到家库存": "0",
        "卖点": "", "可售日期": "不限", "礼袋": "0个礼袋",
        "是否力荐": "否", "是否标品": "标品",
    })
    return row


def scanner_row(row):
    """CTX.rows 里的原始行（10 基础列 + 卡片列 + 详情列）。"""
    values = [int(row["序号"]), row["商品名称"], row["条码"], row["店内码"],
              row["货号"], row["状态"], 0.0, "10:00:00", CAPTURE_TIME, ""]
    return values


class ScannerContextCase(unittest.TestCase):
    """摆一套扫描器上下文（不碰设备：所有设备动作都打桩）。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.saved = {name: getattr(S.CTX, name) for name in (
            "rows", "sku_rows", "profile", "csv_path", "out_dir", "run_stamp",
            "details", "cards", "resumed_cards", "resumed_details",
            "observe_exceptions", "observation_exceptions", "list_evidence",
            "target_count", "final_state", "interrupted", "stop_reason",
            "started_at", "run_started_at", "finished_at", "capture_id",
            "batch_id", "run_id", "serial", "list_title", "category",
            "capture_options", "parsed_cards", "orphan_specs", "list_end_marker",
            "detail_attempts", "carried_this_run", "resumed", "metrics_started",
            "timings", "traversal_counts", "manifest_output", "progress_output",
            "record_out", "record_raw", "scrcpy_proc", "a11y_disabled")}
        S.CTX.rows = []
        S.CTX.sku_rows = {}
        S.CTX.profile = {"name": "xiniu-changan"}
        S.CTX.csv_path = None
        S.CTX.out_dir = self.directory.name
        S.CTX.run_stamp = "20261021-000000"
        S.CTX.details = {}
        S.CTX.cards = {}
        S.CTX.resumed_cards = {}
        S.CTX.resumed_details = {}
        S.CTX.observe_exceptions = True
        S.CTX.observation_exceptions = []
        S.CTX.list_evidence = None
        S.CTX.target_count = 1
        S.CTX.final_state = None
        S.CTX.interrupted = False
        S.CTX.stop_reason = ""
        S.CTX.started_at = None
        S.CTX.run_started_at = None
        S.CTX.finished_at = None
        S.CTX.capture_id = "11111111-1111-4111-8111-111111111111"
        S.CTX.batch_id = "22222222-2222-4222-8222-222222222222"
        S.CTX.run_id = "33333333-3333-4333-8333-333333333333"
        S.CTX.serial = "device"
        S.CTX.list_title = "猫粮"
        S.CTX.category = "萌宠专区/猫粮"
        S.CTX.capture_options = {"requested_count": 0}
        S.CTX.parsed_cards = []
        S.CTX.orphan_specs = []
        S.CTX.list_end_marker = False
        S.CTX.detail_attempts = 0
        S.CTX.carried_this_run = 0
        S.CTX.resumed = 0
        S.CTX.metrics_started = None
        S.CTX.timings = {}
        S.CTX.traversal_counts = {}
        S.CTX.manifest_output = None
        S.CTX.progress_output = None
        S.CTX.record_out = None
        S.CTX.record_raw = None
        S.CTX.scrcpy_proc = None
        S.CTX.a11y_disabled = False
        S.CTX.cleaned = False

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(S.CTX, name, value)
        S.CTX.cleaned = False

    def write_evidence(self, cards, platform_count=1):
        path = os.path.join(self.directory.name, "inventory.json")
        C.atomic_write_json(path, {
            "schema_version": C.LIST_EVIDENCE_SCHEMA_VERSION,
            "category": S.CTX.category, "platform_count": platform_count,
            "cards": cards, "list_exhausted": True, "pages": []})
        S.CTX.list_evidence = path
        return path

    def add_detail(self, index, row):
        """把详情字段放进扫描器的详情缓存（采集行的详情列来自这里）。"""
        key = C.row_identity(index, row["商品名称"], row["条码"])
        S.CTX.details[key] = {column: row[column] for column in C.DETAIL_COLUMNS}

    def add_card(self, index, name, spec, barcode):
        """把列表卡字段放进扫描器的卡片缓存（采集行的规格/价格来自这里）。"""
        key = ("test-card", index)
        S.CTX.row_product_ids[index] = key
        S.CTX.cards[key] = {
            "spec": spec, "month_sales": "10", "stock": "2",
            "sold_out": False, "promo_item": False,
            "prices": ["10", "10", "10", "5/袋"]}
        return key

    def run_cleanup(self):
        """跑收尾（设备动作全部打桩），返回写出的 manifest 路径。"""
        with mock.patch.object(S, "stop_recording"), \
             mock.patch.object(S, "helper_teardown"), \
             mock.patch.object(S, "restore_display"), \
             mock.patch.object(S, "restore_a11y"), \
             mock.patch.object(S, "_set_animations"):
            S.cleanup()
        prefix = os.path.join(self.directory.name, f"qnh_scan_{S.CTX.run_stamp}")
        return prefix + ".manifest.json"


class ObservationContractMarkingTests(ScannerContextCase):
    """F2：契约标记只给真有观测内容的产物。"""

    def test_plain_success_in_observation_mode_has_no_contract(self):
        row = single_row()
        self.add_card(1, row["商品名称"], row["规格"], row["条码"])
        self.add_detail(1, row)
        S.CTX.rows = [scanner_row(row)]
        self.write_evidence([{"name": row["商品名称"], "spec": row["规格"],
                              "barcode": row["条码"]}])
        manifest_path = self.run_cleanup()
        manifest = C.read_json(manifest_path)
        self.assertEqual(manifest["state"], "success")
        # 没有例外 = 没有观测内容：不该挂 v1 契约。
        self.assertNotIn("observation_contract", manifest["capture"])
        self.assertEqual(manifest["progress"]["exceptions"], [])

    def test_manifest_with_exceptions_carries_the_contract(self):
        row = single_row()
        self.add_card(1, row["商品名称"], row["规格"], row["条码"])
        self.add_detail(1, row)
        S.CTX.rows = [scanner_row(row)]
        S.CTX.observation_exceptions = [{"type": "platform_count_mismatch",
                                         "platform_count": 2,
                                         "visible_card_count": 1}]
        self.write_evidence([{"name": row["商品名称"], "spec": row["规格"],
                              "barcode": row["条码"]}], platform_count=2)
        manifest_path = self.run_cleanup()
        manifest = C.read_json(manifest_path)
        self.assertEqual(manifest["state"], "observed_with_exceptions")
        self.assertEqual(manifest["capture"]["observation_contract"], "v1")
        self.assertFalse(manifest["progress"]["business_complete"])


class ObservedUpgradeIdentityTests(ScannerContextCase):
    """F4：升级到观测终态要身份对得上，不是张数对得上。"""

    def test_same_count_different_identity_never_upgrades(self):
        row = single_row()
        self.add_card(1, row["商品名称"], row["规格"], row["条码"])
        self.add_detail(1, row)
        S.CTX.rows = [scanner_row(row)]
        S.CTX.observation_exceptions = [{"type": "platform_count_mismatch",
                                         "platform_count": 2,
                                         "visible_card_count": 1}]
        # 张数一样（1 张），身份不同：采到的不是这一页的东西。
        self.write_evidence([{"name": "别的商品", "spec": "100g",
                              "barcode": "6900000009999"}], platform_count=2)
        manifest_path = self.run_cleanup()
        manifest = C.read_json(manifest_path)
        self.assertNotEqual(manifest["state"], "observed_with_exceptions")
        self.assertNotEqual(manifest["state"], "success")
        self.assertEqual(manifest["state"], "partial")
        self.assertIn("身份不一致", manifest["progress"]["stop_reason"])

    def test_matching_identity_upgrades_to_observed_state(self):
        row = single_row()
        self.add_card(1, row["商品名称"], row["规格"], row["条码"])
        self.add_detail(1, row)
        S.CTX.rows = [scanner_row(row)]
        S.CTX.observation_exceptions = [{"type": "platform_count_mismatch",
                                         "platform_count": 2,
                                         "visible_card_count": 1}]
        self.write_evidence([{"name": row["商品名称"], "spec": row["规格"],
                              "barcode": row["条码"]}], platform_count=2)
        manifest_path = self.run_cleanup()
        manifest = C.read_json(manifest_path)
        self.assertEqual(manifest["state"], "observed_with_exceptions")
        self.assertEqual(manifest["capture"]["observation_contract"], "v1")

    def test_matching_identity_without_exceptions_is_a_plain_success(self):
        row = single_row()
        self.add_card(1, row["商品名称"], row["规格"], row["条码"])
        self.add_detail(1, row)
        S.CTX.rows = [scanner_row(row)]
        self.write_evidence([{"name": row["商品名称"], "spec": row["规格"],
                              "barcode": row["条码"]}])
        manifest_path = self.run_cleanup()
        manifest = C.read_json(manifest_path)
        self.assertEqual(manifest["state"], "success")
        self.assertNotIn("observation_contract", manifest["capture"])


if __name__ == "__main__":
    unittest.main()
