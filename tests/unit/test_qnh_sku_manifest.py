"""多 SKU capture-only 本地契约：卡级 40 列 + .skus.json sidecar 门禁。

契约（纯本地，不连库、不采集）：
- CSV 仍是一张可见卡一行、40 列。多 SKU 卡状态=「多SKU」，SKU 级列（SPU/SKU
  和全部详情列）全空——不塞代表 SKU，也不和列表卡字段混。
- 每个 SKU 的完整 40 列写进 sidecar <prefix>.skus.json：
  {'schema_version': 1, 'listings': {str(卡片序号): [SKU行, ...]}}
  SKU 行的序号仍是父卡序号，标题/采集时间/证据前缀与父卡一致，规格是该 SKU
  自己的具体规格，SKU/SPU/条码/重量/采购价/三渠道各自独立；店内码/货号允许
  为空，但不能复制第一个 SKU 的值。
- manifest 声明 scope.sku_mode='sidecar_v1'、artifacts.sku_path/sku_sha256、
  progress.listing_count（=卡片行数）、progress.sku_row_count（多SKU卡按子行
  数计、单卡按 1 计）。
- 门禁自己算，不信 CSV 状态：sidecar 在不在、hash 对不对、下标不漏不多、每组
  ≥2 个 SKU、组内 SKU 唯一且同 SPU、标题/时间/证据与父卡一致、店内码/货号不
  复制第一个、子行详情完整、子行状态合法。partial 也要过这一关。
- loader：manifest 声明了 scope.sku_mode 就 preconnect 拒，绝不默读入库。
"""

import csv
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

import qnh_capture_common as C
import qnh_load_db as loader
import qnh_product_scan as scan
import qnh_sku_capture as sku_capture


CAPTURE_TIME = "2026-10-09T10:00:00"
# 真机 2026-10-21 高屏 dump：同一 SPU 两个 SKU（粉色/香槟色牡丹）。
MULTISPEC_FIXTURE = Path(__file__).parents[1] / "fixtures/qnh_multispec_detail.xml"


def card_row(index=1, name="商品A", spec="100g", status="成功"):
    """普通单 SKU 卡片行（40 列齐全）。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": f"69000000000{index:02d}",
        "店内码": f"A-{index}", "货号": f"SKU-{index}", "状态": status,
        "视频时间点": "",
        "时刻": "10:00:00", "采集时间": CAPTURE_TIME, "证据前缀": "",
        "规格": spec, "月售": "10", "库存": "2", "售罄": "否", "活动品": "否",
        "美团闪购价": "10", "饿了么价": "10", "京东到家价": "10",
        "采购价": "5/袋", "价格原文": "10 | 10 | 10 | 5/袋",
        "开放平台标识": f"OP-{index}", "SPU": f"SPU-{index}",
        "SKU": f"SKU-{index}", "重量": "100克(g)",
        "采购价_详情": "5", "采购单位_详情": "袋",
        "美团闪购状态": "已上架", "美团闪购零售价": "10", "美团闪购库存": "2",
        "饿了么状态": "已下架", "京东到家状态": "未上线",
        "可售日期": "不限", "礼袋": "0个礼袋", "是否力荐": "否", "是否标品": "标品",
    })
    return row


def multi_card_row(index=1, name="多规格牡丹", spec="粉色牡丹一束（5朵花），香槟色牡丹一束（一朵花）"):
    """多 SKU 卡片行：只有列表卡级字段，SKU 级列全空。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": "", "店内码": "", "货号": "",
        "状态": C.MULTISKU_STATUS, "视频时间点": "", "时刻": "10:00:00",
        "采集时间": CAPTURE_TIME, "证据前缀": "", "规格": spec,
        "月售": "10", "库存": "2", "售罄": "否", "活动品": "否",
        "美团闪购价": "14.5", "饿了么价": "14.5", "京东到家价": "14.5",
        "采购价": "5/袋", "价格原文": "14.5 | 14.5 | 14.5 | 5/袋",
    })
    return row


def sku_row(index=1, name="多规格牡丹", spec="粉色牡丹一束（5朵花）", sku="SKU-A",
            spu="SPU-9", barcode="6900000000009", store_code="", article_no=""):
    """sidecar 里的单个 SKU 行：自己的 spec / SKU / SPU / 条码 / 三渠道。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": barcode,
        "店内码": store_code, "货号": article_no, "状态": "成功", "视频时间点": "",
        "时刻": "10:00:00", "采集时间": CAPTURE_TIME, "证据前缀": "",
        "规格": spec, "月售": "10", "库存": "2", "售罄": "否", "活动品": "否",
        "美团闪购价": "14.5", "饿了么价": "14.5", "京东到家价": "14.5",
        "采购价": "5/袋", "价格原文": "14.5 | 14.5 | 14.5 | 5/袋",
        "开放平台标识": "OP-" + spu, "SPU": spu, "SKU": sku, "重量": "20克(g)",
        "采购价_详情": "5", "采购单位_详情": "单品",
        "美团闪购状态": "已上架", "美团闪购零售价": "14.5", "美团闪购库存": "1",
        "饿了么状态": "已下架", "京东到家状态": "未上线",
        "可售日期": "不限", "礼袋": "0个礼袋", "是否力荐": "否", "是否标品": "标品",
    })
    return row


class SkuSidecarCase(unittest.TestCase):
    """搭一套 mock 采集产物（CSV + sidecar + manifest），全部落在临时目录。"""

    def build(self, directory, rows, listings=None, *, sku_mode=C.SKU_SIDECAR_MODE,
              listing_count=None, sku_row_count=None, sidecar=None,
              sku_sha256=None, write_sidecar=True, scope_extra=None):
        prefix = os.path.join(directory, "qnh_scan_20261009-100000")
        csv_path = prefix + ".csv"
        for row in rows:
            row["证据前缀"] = prefix
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        scope = {
            "list_title": "全部商品", "category": None,
            "expected_product_count": len(rows), "requested_count": 0,
        }
        if sku_mode is not None:
            scope["sku_mode"] = sku_mode
        scope.update(scope_extra or {})
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()), state="success",
            profile={"name": "test", "store_name": "测试店", "warehouse_id": "1",
                     "tenant_id": "2", "store_code": "qnh_test", "role": "competitor"},
            device_id="device", scope=scope,
            capture={"method": "app_ui", "started_at": CAPTURE_TIME,
                     "run_started_at": CAPTURE_TIME,
                     "finished_at": "2026-10-09T10:01:00", "collector_version": "test",
                     "pace": "ultra", "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 50, "record": False},
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix,
                       "video_path": None, "csv_sha256": C.sha256_file(csv_path)},
            rows=rows)
        sidecar_path = prefix + ".skus.json"
        if write_sidecar:
            payload = {"schema_version": C.SKU_SIDECAR_SCHEMA_VERSION,
                       "listings": listings or {}}
            with open(sidecar_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False))
            manifest["artifacts"]["sku_path"] = os.path.basename(sidecar_path)
            manifest["artifacts"]["sku_sha256"] = (
                sku_sha256 if sku_sha256 is not None else C.sha256_file(sidecar_path))
        if listing_count is not None:
            manifest["progress"]["listing_count"] = listing_count
        else:
            manifest["progress"]["listing_count"] = len(rows)
        if sku_row_count is not None:
            manifest["progress"]["sku_row_count"] = sku_row_count
        else:
            manifest["progress"]["sku_row_count"] = self.expected_sku_rows(
                rows, listings or {})
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest, overwrite=True)
        return csv_path, sidecar_path, manifest_path

    @staticmethod
    def expected_sku_rows(rows, listings):
        total = 0
        for row in rows:
            if row["状态"] == C.MULTISKU_STATUS:
                total += len(listings.get(str(int(row["序号"])), []))
            else:
                total += 1
        return total

    def two_sku_listing(self, index=1, name="多规格牡丹"):
        return [
            sku_row(index=index, name=name, spec="粉色牡丹一束（5朵花）",
                    sku="SKU-A", spu="SPU-9", barcode="6900000000009",
                    store_code="A-1", article_no="H-1"),
            sku_row(index=index, name=name, spec="香槟色牡丹一束（一朵花）",
                    sku="SKU-B", spu="SPU-9", barcode="6900000000010",
                    store_code="B-2", article_no="H-2"),
        ]


class MultiSkuCardRowTests(SkuSidecarCase):
    def test_multi_row_has_no_detail_errors(self):
        row = multi_card_row()
        self.assertEqual(C.row_detail_errors(row), [])
        self.assertIn(C.MULTISKU_STATUS, C.COMPLETE_ROW_STATUSES)

    def test_card_row_must_not_carry_representative_sku(self):
        """卡片行塞代表 SKU 的 SPU/SKU 就是造假行：门禁必须拒。"""
        rows = [multi_card_row()]
        rows[0]["SPU"] = "SPU-9"
        rows[0]["SKU"] = "SKU-A"
        rows[0]["美团闪购零售价"] = "14.5"
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": self.two_sku_listing()})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("不得携带 SKU 级列" in error for error in report.errors),
                            report.errors)

    def test_zero_multi_manifest_keeps_old_contract(self):
        """零多 SKU：没有 sku_mode、没有 sidecar，旧契约一个字都不改。"""
        rows = [card_row(1, "商品A"), card_row(2, "商品B")]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, sku_mode=None, write_sidecar=False)
            report = C.validate_manifest(path, require_complete=True)
            self.assertTrue(report.ok, report.errors)
            self.assertNotIn("sku_path", report.manifest["artifacts"])


class MultiSkuSidecarGateTests(SkuSidecarCase):
    def test_valid_multi_sku_capture_passes(self):
        rows = [multi_card_row(1), card_row(2, "商品B")]
        listings = {"1": self.two_sku_listing()}
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, listings)
            report = C.validate_manifest(path, require_complete=True)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.row_count, 2)
            self.assertEqual(report.manifest["progress"]["listing_count"], 2)
            self.assertEqual(report.manifest["progress"]["sku_row_count"], 3)

    def test_same_spu_multiple_skus_is_not_identity_conflict(self):
        rows = [multi_card_row(1)]
        listings = {"1": self.two_sku_listing()}
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, listings)
            report = C.validate_manifest(path, require_complete=True)
            self.assertTrue(report.ok, report.errors)
            self.assertFalse([error for error in report.errors if "身份冲突" in error])

    def test_sidecar_hash_mismatch_fails(self):
        rows = [multi_card_row(1)]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": self.two_sku_listing()},
                                    sku_sha256="0" * 64)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("SHA-256" in error for error in report.errors),
                            report.errors)

    def test_missing_listing_fails(self):
        rows = [multi_card_row(1)]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("没有 listing" in error for error in report.errors),
                            report.errors)

    def test_extra_listing_fails(self):
        rows = [multi_card_row(1)]
        listings = {"1": self.two_sku_listing(), "7": self.two_sku_listing(index=7)}
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, listings)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("多余下标" in error for error in report.errors),
                            report.errors)

    def test_single_child_listing_fails(self):
        rows = [multi_card_row(1)]
        listings = {"1": self.two_sku_listing()[:1]}
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, listings)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("至少 2 个" in error for error in report.errors),
                            report.errors)

    def test_duplicate_sku_in_listing_fails(self):
        rows = [multi_card_row(1)]
        children = self.two_sku_listing()
        children[1]["SKU"] = children[0]["SKU"]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("SKU 重复" in error for error in report.errors),
                            report.errors)

    def test_listing_spanning_two_spus_fails(self):
        rows = [multi_card_row(1)]
        children = self.two_sku_listing()
        children[1]["SPU"] = "SPU-OTHER"
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("多个 SPU" in error for error in report.errors),
                            report.errors)

    def test_child_title_mismatch_fails(self):
        rows = [multi_card_row(1)]
        children = self.two_sku_listing()
        children[1]["商品名称"] = "另一个商品名"
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("商品名称与父卡不一致" in error for error in report.errors),
                            report.errors)

    def test_child_time_mismatch_fails(self):
        rows = [multi_card_row(1)]
        children = self.two_sku_listing()
        children[1]["采集时间"] = "2026-10-09T10:00:30"
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("采集时间与父卡不一致" in error for error in report.errors),
                            report.errors)

    def test_child_evidence_mismatch_fails(self):
        rows = [multi_card_row(1)]
        children = self.two_sku_listing()
        children[1]["证据前缀"] = "/tmp/别人的前缀"
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("证据前缀与父卡不一致" in error for error in report.errors),
                            report.errors)

    def test_child_detail_incomplete_fails(self):
        rows = [multi_card_row(1)]
        children = self.two_sku_listing()
        children[1]["采购单位_详情"] = ""
        children[1]["美团闪购状态"] = "已上架"
        children[1]["美团闪购零售价"] = ""
        children[1]["美团闪购库存"] = ""
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("详情字段不完整" in error for error in report.errors),
                            report.errors)

    def test_child_status_invalid_fails(self):
        rows = [multi_card_row(1)]
        children = self.two_sku_listing()
        children[1]["状态"] = "携带"
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("状态无效" in error for error in report.errors),
                            report.errors)

    def test_store_code_copied_from_first_child_fails(self):
        rows = [multi_card_row(1)]
        children = self.two_sku_listing()
        children[1]["店内码"] = children[0]["店内码"]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("复制了第一个 SKU" in error for error in report.errors),
                            report.errors)

    def test_blank_store_code_and_article_no_are_allowed(self):
        rows = [multi_card_row(1)]
        children = self.two_sku_listing()
        for child in children:
            child["店内码"] = ""
            child["货号"] = ""
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertTrue(report.ok, report.errors)

    def test_multi_row_without_sku_mode_fails(self):
        rows = [multi_card_row(1)]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": self.two_sku_listing()},
                                    sku_mode=None)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("scope.sku_mode 未声明" in error for error in report.errors),
                            report.errors)

    def test_sidecar_gate_applies_to_partial_manifest(self):
        """partial 也必须自己验 sidecar：状态写着多SKU 不能替代 sidecar。"""
        rows = [multi_card_row(1)]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {})
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("没有 listing" in error for error in report.errors),
                            report.errors)

    def test_listing_count_mismatch_fails(self):
        rows = [multi_card_row(1)]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": self.two_sku_listing()},
                                    listing_count=99)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("listing_count" in error for error in report.errors),
                            report.errors)

    def test_sku_row_count_mismatch_fails(self):
        rows = [multi_card_row(1), card_row(2, "商品B")]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": self.two_sku_listing()},
                                    sku_row_count=1)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("sku_row_count" in error for error in report.errors),
                            report.errors)

    def test_sku_row_count_counts_incomplete_cards_as_one(self):
        """不完整单卡也算一行（主线程口径）：sku_row_count = 子行数 + 单卡数。"""
        rows = [multi_card_row(1), card_row(2, "商品B", status="详情不全")]
        listings = {"1": self.two_sku_listing()}
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, listings)
            report = C.validate_manifest(path, require_complete=False)
            self.assertNotIn("sku_row_count", "".join(report.errors))
            self.assertEqual(report.manifest["progress"]["sku_row_count"], 3)


class RealMultispecFixtureTests(SkuSidecarCase):
    """真机 fixture 解析出的两个 SKU 必须能过门禁（只读本地 XML，不连设备）。"""

    def test_real_parsed_children_pass_the_gate(self):
        nodes = scan.parse_nodes(MULTISPEC_FIXTURE.read_text(encoding="utf-8"))
        details = sku_capture.parse_sku_details(nodes)
        self.assertEqual(len(details), 2)
        card = multi_card_row(
            1, "含苞牡丹仿真花芍药室内家居摆件 1束 婚庆手捧花野餐冰箱客厅桌面装饰")
        children = []
        for detail in details:
            child = dict(card)
            child.update(detail)
            child["状态"] = "成功"
            child["店内码"] = ""
            child["货号"] = ""
            children.append(child)
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, [card], {"1": children})
            report = C.validate_manifest(path, require_complete=True)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.manifest["progress"]["sku_row_count"], 2)


class MultiSkuLoaderIngestTests(SkuSidecarCase):
    """多 SKU 产物现在能入库：sidecar 的子行逐 SKU 成行（详见
    tests/unit/test_qnh_multisku_ingest.py）。这里只钉住两件事：不连库时的
    拒绝与放行。"""

    def test_loader_no_longer_refuses_a_multi_sku_manifest(self):
        rows = [multi_card_row(1)]
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.build(directory, rows, {"1": self.two_sku_listing()})
            try:
                loader.load_capture(manifest_path=path)
            except Exception as exc:
                message = str(exc)
            else:
                message = ""
            # 不再因为「多 SKU 产物」被拒（这条拒绝已经取消）。
            self.assertNotIn("多SKU采集产物暂不支持入库", message)

    def test_loader_rejects_legacy_csv_with_multisku_row(self):
        opened = []

        def boom():
            opened.append(True)
            raise AssertionError("loader 不该连接数据库")

        original = loader._open_connection
        loader._open_connection = boom
        try:
            with tempfile.TemporaryDirectory() as directory:
                csv_path, _, _ = self.build(
                    directory, [multi_card_row(1)], {"1": self.two_sku_listing()})
                with self.assertRaises(C.CaptureContractError) as ctx:
                    loader.load_capture(csv_path=csv_path, store_name="测试店",
                                        warehouse_id="1")
                # 直接 CSV 模式没有 sidecar 就没法展开多 SKU：必须拒绝。
                self.assertIn("sidecar", str(ctx.exception))
        finally:
            loader._open_connection = original
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()
