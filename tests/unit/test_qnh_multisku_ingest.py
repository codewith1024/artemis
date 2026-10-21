"""多 SKU 产物与观测终态产物的入库：每个 SKU 一行，未知渠道格记 unknown。

纯本地：DB 全是假的（FakeDB），不连库、不采集、不碰设备。

契约：
- 多 SKU 卡片行没有 SKU 级字段，入库按 sidecar 的子行逐 SKU 成行；卡片行本身
  不入库（拿代表 SKU 冒充整张卡就是错的）。
- 每个 SKU 事实行带 sku_count（该 SPU 的 SKU 数）与 has_multi_spec。
- 渠道行缺失（未知）：sale_status='unknown'、listing_status 为空、价与库存为空
  ——不写 0，不写已下架/未上线。
- 观测终态产物可以入库，但快照状态记 partial，例外清单写进 error_message：
  「不完整」这件事不被掩盖，也不至于把整份快照丢掉。
- 直接 CSV 模式（没有 sidecar）遇到多 SKU 行必须拒绝：没有 sidecar 就没法展开。
"""

import csv
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import qnh_capture_common as C
import qnh_load_db as loader
import qnh_sku_capture as K
from qnh_observation import parse_nodes
from tests.unit.test_qnh_mapping_safety import FakeConn, FakeDB
from tests.unit.test_qnh_observed_aggregation import list_evidence_for
from tests.unit.test_qnh_sku_manifest import card_row, multi_card_row, sku_row

FIXTURES = Path(__file__).parents[1] / "fixtures"
CAPTURE_TIME = "2026-10-21T10:00:00"
PROFILE = "xiniu-changan"


def fixture_children(card):
    """真机详情夹具：两个 SKU，其中一个在「京东到家」渠道表里没有行。"""
    path = FIXTURES / "qnh_wowo_channel_absent.xml"
    details = K.parse_sku_details(parse_nodes(path.read_text()))
    children = []
    observations = {}
    for detail in details:
        child = dict(card)
        child.update({key: value for key, value in detail.items()
                      if key in C.DETAIL_COLUMNS or key in ("规格", "条码")})
        child["状态"] = "成功"
        child["店内码"] = ""
        child["货号"] = ""
        children.append(child)
        for channel, entry in (detail.get("_channel_observations") or {}).items():
            observations.setdefault(str(detail["SKU"]), {})[channel] = {
                **entry,
                "evidence_pages": [{"path": str(path),
                                    "sha256": C.sha256_file(path)}],
            }
    return children, observations


class IngestCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.conn = FakeConn(self.db)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def build_manifest(self, rows, *, listings=None, observations=None,
                       state="success", exceptions=None, contract=None,
                       expected_count=None, sku_mode=C.SKU_SIDECAR_MODE,
                       list_evidence=False, detail_complete_count=None):
        directory = self.directory.name
        prefix = os.path.join(directory, "qnh_scan_20261021-100000")
        csv_path = prefix + ".csv"
        for index, row in enumerate(rows, 1):
            row["序号"] = str(index)
            row["证据前缀"] = prefix
            row["采集时间"] = CAPTURE_TIME
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        scope = {"list_title": "猫粮", "category": "萌宠专区/猫粮",
                 "expected_product_count": (expected_count if expected_count is not None
                                            else len(rows)),
                 "requested_count": 0}
        if sku_mode:
            scope["sku_mode"] = sku_mode
        artifacts = {"csv_path": csv_path, "evidence_prefix": prefix,
                     "video_path": None, "csv_sha256": C.sha256_file(csv_path)}
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()), state=state,
            profile=C.load_profile(PROFILE), device_id="device", scope=scope,
            capture={"method": "app_ui", "started_at": CAPTURE_TIME,
                     "run_started_at": CAPTURE_TIME, "finished_at": CAPTURE_TIME,
                     "collector_version": "test", "pace": "ultra",
                     "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False,
                     "dismiss_order_dialogs": False},
            artifacts=artifacts, rows=rows)
        if listings:
            fixed = {}
            for key, children in listings.items():
                parent = rows[int(key) - 1]
                fixed[key] = [dict(child, 序号=key, 证据前缀=prefix,
                                   采集时间=parent["采集时间"])
                              for child in children]
            payload = {"schema_version": C.SKU_SIDECAR_SCHEMA_VERSION,
                       "listings": fixed}
            if observations:
                payload["observations"] = observations
            sidecar_path = prefix + ".skus.json"
            C.atomic_write_json(sidecar_path, payload)
            manifest["artifacts"]["sku_path"] = sidecar_path
            manifest["artifacts"]["sku_sha256"] = C.sha256_file(sidecar_path)
            manifest["progress"]["listing_count"] = len(rows)
            manifest["progress"]["sku_row_count"] = sum(
                len(fixed.get(row["序号"], [])) if C.is_multisku_row(row) else 1
                for row in rows)
        if contract:
            manifest["capture"]["observation_contract"] = contract
        if list_evidence:
            # 例外态必须给出逐屏列表证据：观测完成（页面已采全）要拿得出证据。
            evidence_path = list_evidence_for(
                directory,
                [{"name": row["商品名称"], "spec": row["规格"],
                  "barcode": row.get("条码", "")} for row in rows],
                leaf="猫粮", category="萌宠专区/猫粮",
                platform_count=len(rows))
            manifest["artifacts"]["list_evidence_path"] = evidence_path
            manifest["artifacts"]["list_evidence_sha256"] = C.sha256_file(evidence_path)
        if detail_complete_count is not None:
            manifest["progress"]["detail_complete_count"] = detail_complete_count
        if exceptions is not None:
            manifest["progress"].update({"observation_complete": True,
                                         "business_complete": False,
                                         "exceptions": exceptions})
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest)
        return csv_path, manifest_path

    def load(self, manifest_path, **kwargs):
        kwargs.setdefault("commit", True)
        with mock.patch.object(loader, "_open_connection", return_value=self.conn):
            return loader.load_capture(manifest_path=manifest_path, **kwargs)

    def facts(self):
        return self.db.rows("fact_store_product_snapshot")

    def snapshot(self):
        return self.db.rows("store_snapshot")[0]


class MultiSkuIngestTests(IngestCase):
    """多 SKU 卡片：每个 SKU 一行，事实行带 SKU 数与多规格标记。"""

    def build(self):
        card = multi_card_row(1)
        children = [sku_row(1, spec="粉色牡丹一束（5朵花）", sku="SKU-A", spu="SPU-9",
                            barcode="6900000000009"),
                    sku_row(1, spec="香槟色牡丹一束（一朵花）", sku="SKU-B", spu="SPU-9",
                            barcode="6900000000010")]
        listings = {"1": [{**child, "序号": "1"} for child in children]}
        return self.build_manifest([card], listings=listings)

    def test_card_row_is_not_written_but_every_sku_is(self):
        csv_path, manifest_path = self.build()
        result = self.load(manifest_path)
        self.assertTrue(result["ok"], result)
        facts = self.facts()
        # 2 个 SKU × 3 个渠道 = 6 行事实；卡片行本身不入库。
        self.assertEqual(len(facts), 6)
        skus = {row["source_sku_id"] for row in facts}
        self.assertEqual(skus, {"SKU-A", "SKU-B"})
        self.assertEqual(result["rows"], 1)          # 卡片（SPU）数
        self.assertEqual(result["fact_rows"], 6)      # 入库行数

    def test_each_sku_row_records_its_spu_sku_count(self):
        csv_path, manifest_path = self.build()
        self.load(manifest_path)
        for row in self.facts():
            self.assertEqual(row["sku_count"], 2)
            self.assertEqual(row["has_multi_spec"], 1)
            self.assertEqual(row["source_spu_id"], "SPU-9")

    def test_snapshot_counts_cards_and_skus_separately(self):
        csv_path, manifest_path = self.build()
        self.load(manifest_path)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["captured_spu_count"], 1)   # 卡片
        self.assertEqual(snapshot["captured_sku_count"], 2)   # SKU

    def test_post_validation_agrees_with_the_expanded_facts(self):
        csv_path, manifest_path = self.build()
        result = self.load(manifest_path)
        validation = result["validation"]
        self.assertTrue(validation["ok"], validation)


class UnknownChannelIngestTests(IngestCase):
    """渠道行缺失：sale_status=unknown、listing_status 空、价与库存空。"""

    def build(self, *, state="success", exceptions=None):
        card = multi_card_row(1)
        children, observations = fixture_children(card)
        listings = {"1": [{**child, "序号": "1"} for child in children]}
        return self.build_manifest(
            [card], listings=listings, observations={"1": observations},
            state=state, exceptions=exceptions,
            contract=C.OBSERVATION_CONTRACT_VERSION if state != "success" else None,
            list_evidence=state != "success",
            # 多 SKU 父卡含未知子行 → 整张卡不算 detail_complete。
            detail_complete_count=0)

    def test_unknown_channel_cell_is_stored_as_unknown(self):
        # 未知渠道行只能出现在例外态：普通 success 里的「未知」依旧失败。
        exceptions = [{"type": "channel_row_absent", "sku": "2098339639743791201",
                       "channel": "京东到家", "spec": "鳕鱼虾味40g"}]
        csv_path, manifest_path = self.build(state=C.OBSERVED_MANIFEST_STATE,
                                             exceptions=exceptions)
        self.load(manifest_path)
        unknown = [row for row in self.facts()
                   if row["source_sku_id"] == "2098339639743791201"
                   and row["channel_code"] == "jddj"]
        self.assertEqual(len(unknown), 1)
        row = unknown[0]
        self.assertEqual(row["sale_status"], "unknown")
        self.assertIsNone(row["listing_status"])
        self.assertIsNone(row["front_display_price_amount"])
        self.assertIsNone(row["stock_quantity"])
        # 同一 SKU 的另一个渠道照常是真实值（该渠道库存是「售罄」）。
        other = [item for item in self.facts()
                 if item["source_sku_id"] == "2098339639743791201"
                 and item["channel_code"] == "meituan"][0]
        self.assertEqual(other["sale_status"], "out_of_stock")
        self.assertEqual(str(other["front_display_price_amount"]), "2.8")

    def test_observed_manifest_is_ingested_as_a_partial_snapshot(self):
        exceptions = [{"type": "channel_row_absent", "sku": "2098339639743791201",
                       "channel": "京东到家", "spec": "鳕鱼虾味40g"}]
        csv_path, manifest_path = self.build(state=C.OBSERVED_MANIFEST_STATE,
                                             exceptions=exceptions)
        result = self.load(manifest_path)
        self.assertTrue(result["ok"], result)
        snapshot = self.snapshot()
        # 业务不完整这件事写在快照上，不靠猜。
        self.assertEqual(snapshot["status"], "partial")
        self.assertIn("观测例外", snapshot["error_message"])
        self.assertIn("channel_row_absent", snapshot["error_message"])
        # 数据照样入库：未知渠道格记 unknown。
        self.assertEqual(len(self.facts()), 6)


class LegacyCsvModeTests(IngestCase):
    """直接 CSV 模式没有 sidecar：多 SKU 行必须拒绝（没法展开）。"""

    def test_direct_csv_mode_refuses_multisku_rows(self):
        card = multi_card_row(1)
        csv_path, _ = self.build_manifest([card], listings=None, sku_mode=None)
        with self.assertRaises(C.CaptureContractError) as ctx:
            loader.load_capture(csv_path=csv_path, store_name="测试店",
                                warehouse_id="1")
        self.assertIn("sidecar", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
