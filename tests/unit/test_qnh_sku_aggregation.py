"""跨分片多 SKU 聚合契约：sidecar 汇总、真实 SKU 身份去重、--capture-only 不连库。

纯本地（不连库、不采集、不碰设备）：
- 分片 sidecar（.skus.json）必须存在且哈希吻合：丢失/篡改 → 聚合失败。
  多 SKU 卡不许被当单 SKU 卡过滤掉。
- 聚合后主 CSV 仍是一张可见卡一行（40 列）；每个多 SKU 卡的 SKU 子行进聚合
  sidecar，下标=聚合后主 CSV 序号，子行的序号/标题/采集时间/证据前缀与父卡
  严格同步。
- 跨分片去重：同一张卡在两个类目出现 → 只留一次，SKU 不重复；单卡沿用历史口径
  （条码优先的 listing_identity + 规格），不改所有历史分片的合并行为；多 SKU 卡
  按 SPU 认卡，同一 SKU 指向不同 SPU → 直接失败；同一张多 SKU 卡两次观测的 SKU
  集合不同 → fail closed（不丢独有 SKU，也不合并时间快照）。
- 同 SKU 的价格/库存跨时点变动：保留最先出现那次观测，不混字段，差异逐条记
  进 manifest 的 scope.sku_time_divergences。
- --capture-only：聚合与门禁通过后只保存 manifest 并退出，不调用入库流水线。
"""

import csv
import json
import os
import sys
import tempfile
import unittest
import uuid
from unittest import mock

import qnh_capture_common as C
import qnh_category_pipeline as category_pipeline


CAPTURE_TIME = "2026-10-21T10:00:00"
MULTI_NAME = "多规格牡丹"


def card_row(index=1, name="商品A", spec="100g", sku="SKU-S1", spu="SPU-S1",
             barcode="6900000000001"):
    """单 SKU 卡片行：40 列齐全（详情列有值）。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": barcode,
        "店内码": "A-1", "货号": "H-1", "状态": "成功", "视频时间点": "",
        "时刻": "10:00:00", "采集时间": CAPTURE_TIME, "证据前缀": "",
        "规格": spec, "月售": "10", "库存": "2", "售罄": "否", "活动品": "否",
        "美团闪购价": "10", "饿了么价": "10", "京东到家价": "10",
        "采购价": "5/袋", "价格原文": "10 | 10 | 10 | 5/袋",
        "开放平台标识": "OP-1", "SPU": spu, "SKU": sku, "重量": "100克(g)",
        "采购价_详情": "5", "采购单位_详情": "袋",
        "美团闪购状态": "已上架", "美团闪购零售价": "10", "美团闪购库存": "2",
        "饿了么状态": "已下架", "饿了么零售价": "10", "饿了么库存": "1",
        "京东到家状态": "未上线", "京东到家零售价": "10", "京东到家库存": "0",
        "卖点": "", "可售日期": "不限", "礼袋": "0个礼袋",
        "是否力荐": "否", "是否标品": "标品",
    })
    return row


def multi_card_row(index=1, name=MULTI_NAME, spec="粉色牡丹一束（5朵花），香槟色牡丹一束（一朵花）"):
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


def sku_row(index=1, name=MULTI_NAME, spec="粉色牡丹一束（5朵花）", sku="SKU-A",
            spu="SPU-9", barcode="6900000000009", purchase="5", price="14.5",
            stock="1"):
    """sidecar 里的单个 SKU 行：自己的规格/SKU/SPU/条码/三渠道。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": barcode,
        "店内码": "", "货号": "", "状态": "成功", "视频时间点": "",
        "时刻": "10:00:00", "采集时间": CAPTURE_TIME, "证据前缀": "",
        "规格": spec, "月售": "10", "库存": "2", "售罄": "否", "活动品": "否",
        "美团闪购价": price, "饿了么价": price, "京东到家价": price,
        "采购价": purchase + "/单品", "价格原文": " | ".join([price] * 3),
        "开放平台标识": "OP-" + spu, "SPU": spu, "SKU": sku, "重量": "20克(g)",
        "采购价_详情": purchase, "采购单位_详情": "单品",
        "美团闪购状态": "已上架", "美团闪购零售价": price, "美团闪购库存": stock,
        "饿了么状态": "已下架", "饿了么零售价": price, "饿了么库存": stock,
        "京东到家状态": "未上线", "京东到家零售价": price, "京东到家库存": stock,
        "卖点": "", "可售日期": "不限", "礼袋": "0个礼袋",
        "是否力荐": "否", "是否标品": "标品",
    })
    return row


def two_sku_listing(spec_a="粉色牡丹一束（5朵花）", spec_b="香槟色牡丹一束（一朵花）",
                    purchase_a="5", purchase_b="5", price_b="14.5", stock_b="1"):
    return [
        sku_row(spec=spec_a, sku="SKU-A", spu="SPU-9", barcode="6900000000009",
                purchase=purchase_a),
        sku_row(spec=spec_b, sku="SKU-B", spu="SPU-9", barcode="6900000000010",
                purchase=purchase_b, price=price_b, stock=stock_b),
    ]


class SkuAggregationCase(unittest.TestCase):
    """搭一套分片产物（CSV + sidecar + manifest），全部落在临时目录。"""

    def make_segment(self, root, order, top, leaf, rows, sku_groups=None, *,
                     captured_at=CAPTURE_TIME, write_sidecar=True,
                     sidecar_sha=None, sku_mode=True):
        """sku_groups: {卡片下标(1-based): [SKU 子行, ...]}。"""
        directory = os.path.join(root, f"{order:03d}")
        os.makedirs(directory, exist_ok=True)
        prefix = os.path.join(directory, "qnh_scan_segment")
        csv_path = prefix + ".csv"
        for index, row in enumerate(rows, 1):
            row["序号"] = str(index)
            row["证据前缀"] = prefix
            # 采集时间必须落在分片的 capture 时间窗内：跟着分片的采集时点走。
            row["采集时间"] = captured_at
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        scope = {"list_title": leaf, "category": f"{top}/{leaf}",
                 "expected_product_count": len(rows), "requested_count": 0}
        artifacts = {"csv_path": csv_path, "evidence_prefix": prefix,
                     "video_path": None, "csv_sha256": C.sha256_file(csv_path)}
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()), state="success",
            profile=C.load_profile("xiniu-changan"), device_id="R28M31LJX0E",
            scope=scope,
            capture={"method": "app_ui", "started_at": captured_at,
                     "run_started_at": captured_at, "finished_at": captured_at,
                     "collector_version": "test", "pace": "ultra",
                     "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False,
                     "dismiss_order_dialogs": False},
            artifacts=artifacts, rows=rows)
        sidecar_path = prefix + ".skus.json"
        listings = {}
        for card_index, children in (sku_groups or {}).items():
            card = rows[card_index - 1]
            seq = str(card_index)
            built = []
            for child in children:
                child = dict(child)
                child["序号"] = seq
                child["证据前缀"] = prefix
                child["采集时间"] = card["采集时间"]
                built.append(child)
            listings[seq] = built
        if listings:
            payload = {"schema_version": C.SKU_SIDECAR_SCHEMA_VERSION,
                       "listings": listings}
            with open(sidecar_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False))
            manifest["artifacts"]["sku_path"] = sidecar_path
            manifest["artifacts"]["sku_sha256"] = (
                sidecar_sha if sidecar_sha is not None
                else C.sha256_file(sidecar_path))
        if sku_mode and listings:
            manifest["scope"]["sku_mode"] = C.SKU_SIDECAR_MODE
        sku_rows = 0
        for row in rows:
            if C.is_multisku_row(row):
                sku_rows += len(listings.get(row["序号"], []))
            else:
                sku_rows += 1
        manifest["progress"]["listing_count"] = len(rows)
        manifest["progress"]["sku_row_count"] = sku_rows
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest)
        return manifest_path, sidecar_path

    def make_state(self, root, completed):
        """completed: {order: (manifest_path, expected_count)}。"""
        state_path = os.path.join(root, "capture.state.json")
        C.atomic_write_json(state_path, {
            "schema_version": 1, "capture_id": str(uuid.uuid4()),
            "batch_id": str(uuid.uuid4()), "dismiss_order_dialogs": False,
            "completed": {order: {"manifest_path": path,
                                  "manifest_sha256": C.sha256_file(path),
                                  "expected_count": count}
                          for order, (path, count) in completed.items()},
        })
        return state_path

    def merge(self, root, segments, state_path, *, full_count):
        return category_pipeline.merge_segments(
            root, {"full_store_count": full_count}, segments, state_path,
            full_count=full_count)

    def aggregate_report(self, manifest_path):
        report = C.validate_manifest(
            manifest_path, require_complete=True,
            expected_profile=C.load_profile("xiniu-changan"))
        self.assertTrue(report.ok, report.errors)
        return report


class MultiSkuCrossSegmentTests(SkuAggregationCase):
    """同一张多 SKU 卡出现在两个分片：只留一次，SKU 不重复。"""

    def test_same_multi_card_in_two_segments_keeps_one_card_and_one_listing(self):
        with tempfile.TemporaryDirectory() as root:
            first, _ = self.make_segment(
                root, 1, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()}, captured_at="2026-10-21T10:00:00")
            second, _ = self.make_segment(
                root, 2, "精选业务", "精选业务", [multi_card_row()],
                {1: two_sku_listing(purchase_b="6", price_b="15.5", stock_b="2")},
                captured_at="2026-10-21T11:00:00")
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 1)})
            segments = [{"order": 1, "top": "花卉绿植", "leaf": "鲜花",
                         "expected_count": 1},
                        {"order": 2, "top": "精选业务", "leaf": "精选业务",
                         "expected_count": 1}]
            manifest_path = self.merge(root, segments, state_path, full_count=1)
            report = self.aggregate_report(manifest_path)
            # 主 CSV 仍是一卡一行：并集 1 张卡。
            self.assertEqual(report.row_count, 1)
            self.assertEqual(report.manifest["scope"]["cross_category_duplicate_count"], 1)
            # sidecar 只有一份 listing、两个 SKU：重复的卡不重复出 SKU。
            sidecar = C.read_json(report.manifest["artifacts"]["sku_path"])
            self.assertEqual(list(sidecar["listings"]), ["1"])
            self.assertEqual(len(sidecar["listings"]["1"]), 2)
            self.assertEqual({child["SKU"] for child in sidecar["listings"]["1"]},
                             {"SKU-A", "SKU-B"})
            self.assertEqual(report.manifest["progress"]["listing_count"], 1)
            self.assertEqual(report.manifest["progress"]["sku_row_count"], 2)

    def test_duplicate_multi_card_keeps_first_observation_without_mixing_fields(self):
        with tempfile.TemporaryDirectory() as root:
            first, _ = self.make_segment(
                root, 1, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()}, captured_at="2026-10-21T10:00:00")
            second, _ = self.make_segment(
                root, 2, "精选业务", "精选业务", [multi_card_row()],
                {1: two_sku_listing(purchase_b="6", price_b="15.5", stock_b="2")},
                captured_at="2026-10-21T11:00:00")
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 1)})
            segments = [{"order": 1, "top": "花卉绿植", "leaf": "鲜花",
                         "expected_count": 1},
                        {"order": 2, "top": "精选业务", "leaf": "精选业务",
                         "expected_count": 1}]
            manifest_path = self.merge(root, segments, state_path, full_count=1)
            report = self.aggregate_report(manifest_path)
            sidecar = C.read_json(report.manifest["artifacts"]["sku_path"])
            kept = {child["SKU"]: child for child in sidecar["listings"]["1"]}
            # 保留最先出现那次观测：字段原样保留，不与后来的观测混合。
            self.assertEqual(kept["SKU-B"]["采购价_详情"], "5")
            self.assertEqual(kept["SKU-B"]["美团闪购零售价"], "14.5")
            self.assertEqual(kept["SKU-B"]["美团闪购库存"], "1")
            # 差异必须看得见：记进 manifest，不静默丢掉。
            divergences = report.manifest["scope"]["sku_time_divergences"]
            self.assertEqual(len(divergences), 1)
            self.assertEqual(divergences[0]["kept"]["segment_order"], 1)
            self.assertEqual(divergences[0]["dropped"]["segment_order"], 2)
            changed = {entry["sku"]: entry["fields"]
                       for entry in divergences[0]["skus"]}
            self.assertIn("SKU-B", changed)
            self.assertIn("采购价_详情", changed["SKU-B"])
            self.assertIn("美团闪购零售价", changed["SKU-B"])
            self.assertIn("美团闪购库存", changed["SKU-B"])
            self.assertEqual(
                report.manifest["scope"]["sku_time_policy"]["policy"],
                "first_observation_wins")

    def test_single_card_and_multi_card_are_both_kept(self):
        with tempfile.TemporaryDirectory() as root:
            first, _ = self.make_segment(
                root, 1, "休闲零食", "薯片薯条", [card_row()],
                captured_at="2026-10-21T10:00:00")
            second, _ = self.make_segment(
                root, 2, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()}, captured_at="2026-10-21T11:00:00")
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 1)})
            segments = [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                         "expected_count": 1},
                        {"order": 2, "top": "花卉绿植", "leaf": "鲜花",
                         "expected_count": 1}]
            manifest_path = self.merge(root, segments, state_path, full_count=2)
            report = self.aggregate_report(manifest_path)
            self.assertEqual(report.row_count, 2)
            statuses = [row["状态"] for row in report.rows]
            self.assertEqual(statuses, ["成功", C.MULTISKU_STATUS])
            sidecar = C.read_json(report.manifest["artifacts"]["sku_path"])
            self.assertEqual(list(sidecar["listings"]), ["2"])
            self.assertEqual(report.manifest["progress"]["listing_count"], 2)
            # 卡片数 2 + 多 SKU 卡的子行 2 = SKU 行数 3：两类计数分别记。
            self.assertEqual(report.manifest["progress"]["sku_row_count"], 3)

    def test_same_sku_different_spec_never_merged(self):
        with tempfile.TemporaryDirectory() as root:
            first_row = card_row(name="同一商品", sku="SKU-X", spu="SPU-X",
                                 barcode="6900000000099")
            first_row["规格"] = "100g"
            second_row = card_row(name="同一商品", sku="SKU-X", spu="SPU-X",
                                  barcode="6900000000099")
            second_row["规格"] = "200g"
            first, _ = self.make_segment(
                root, 1, "食品", "规格A", [first_row])
            second, _ = self.make_segment(
                root, 2, "食品", "规格B", [second_row])
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 1)})
            segments = [{"order": 1, "top": "食品", "leaf": "规格A",
                         "expected_count": 1},
                        {"order": 2, "top": "食品", "leaf": "规格B",
                         "expected_count": 1}]
            manifest_path = self.merge(root, segments, state_path, full_count=2)
            report = self.aggregate_report(manifest_path)
            self.assertEqual(report.row_count, 2)
            self.assertEqual([row["规格"] for row in report.rows], ["100g", "200g"])

    def test_same_spu_different_sku_set_fails_closed(self):
        # 同 SPU 两次观测的 SKU 集合不同：丢掉独有 SKU 就是跨片 SKU 误删，
        # 合并两边就是混字段。fail closed：直接报错，不落聚合产物。
        with tempfile.TemporaryDirectory() as root:
            first, _ = self.make_segment(
                root, 1, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()}, captured_at="2026-10-21T10:00:00")
            second_listing = two_sku_listing() + [
                sku_row(spec="白色牡丹一束（三朵花）", sku="SKU-C", spu="SPU-9",
                        barcode="6900000000020")]
            second, _ = self.make_segment(
                root, 2, "精选业务", "精选业务", [multi_card_row()],
                {1: second_listing}, captured_at="2026-10-21T11:00:00")
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 1)})
            segments = [{"order": 1, "top": "花卉绿植", "leaf": "鲜花",
                         "expected_count": 1},
                        {"order": 2, "top": "精选业务", "leaf": "精选业务",
                         "expected_count": 1}]
            with self.assertRaises(C.CaptureContractError) as ctx:
                self.merge(root, segments, state_path, full_count=1)
            message = str(ctx.exception)
            self.assertIn("SKU集合跨时点变化无法合并", message)
            self.assertIn("SKU-C", message)
            self.assertIn("SKU-A", message)
            # fail closed：不能留下一个「看起来完整」的聚合产物。
            self.assertFalse([name for name in os.listdir(root)
                              if name.startswith("qnh_category_full")])

    def test_single_card_keeps_old_barcode_first_dedup(self):
        # 单卡沿用历史口径：同条码同规格仍按条码合并（换成 SKU 优先会多出一
        # 行，动到并集口径）。差异记进 divergences，不是静默。
        with tempfile.TemporaryDirectory() as root:
            first_row = card_row(name="同UPC商品", sku="SKU-1", spu="SPU-1",
                                 barcode="6900000000001")
            second_row = card_row(name="同UPC商品", sku="SKU-2", spu="SPU-2",
                                  barcode="6900000000001")
            first, _ = self.make_segment(root, 1, "食品", "类目A", [first_row])
            second, _ = self.make_segment(root, 2, "食品", "类目B", [second_row])
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 1)})
            segments = [{"order": 1, "top": "食品", "leaf": "类目A",
                         "expected_count": 1},
                        {"order": 2, "top": "食品", "leaf": "类目B",
                         "expected_count": 1}]
            manifest_path = self.merge(root, segments, state_path, full_count=1)
            report = self.aggregate_report(manifest_path)
            self.assertEqual(report.row_count, 1)
            divergences = report.manifest["scope"]["sku_time_divergences"]
            self.assertEqual(len(divergences), 1)
            self.assertIn("SKU", divergences[0]["skus"][0]["fields"])

    def test_same_sku_under_two_spus_fails(self):
        with tempfile.TemporaryDirectory() as root:
            first, _ = self.make_segment(
                root, 1, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()})
            # 第二片：另一张卡（SPU-OTHER），但里面有一个同名 SKU-A：
            # 同一 SKU 挂在两个 SPU 下 —— 跨分片身份冲突，必须失败。
            second_listing = [sku_row(spec="粉色牡丹一束（5朵花）", sku="SKU-A",
                                      spu="SPU-OTHER", barcode="6900000000020"),
                              sku_row(spec="香槟色牡丹一束（一朵花）", sku="SKU-C",
                                      spu="SPU-OTHER", barcode="6900000000021")]
            second, _ = self.make_segment(
                root, 2, "精选业务", "精选业务", [multi_card_row()],
                {1: second_listing})
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 1)})
            segments = [{"order": 1, "top": "花卉绿植", "leaf": "鲜花",
                         "expected_count": 1},
                        {"order": 2, "top": "精选业务", "leaf": "精选业务",
                         "expected_count": 1}]
            with self.assertRaises(C.CaptureContractError) as ctx:
                self.merge(root, segments, state_path, full_count=2)
            message = str(ctx.exception)
            self.assertIn("SKU 身份冲突", message)
            self.assertIn("SKU-A", message)
            self.assertIn("SPU-9", message)
            self.assertIn("SPU-OTHER", message)


class SidecarIntegrityTests(SkuAggregationCase):
    """父卡 sidecar 丢失/篡改必须失败，不许把多 SKU 卡当单卡过滤。"""

    def test_missing_sidecar_fails(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path, sidecar_path = self.make_segment(
                root, 1, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()})
            os.unlink(sidecar_path)
            state_path = self.make_state(root, {"1": (manifest_path, 1)})
            segments = [{"order": 1, "top": "花卉绿植", "leaf": "鲜花",
                         "expected_count": 1}]
            with self.assertRaises(C.CaptureContractError) as ctx:
                self.merge(root, segments, state_path, full_count=1)
            self.assertIn("sidecar", str(ctx.exception))

    def test_tampered_sidecar_fails(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path, sidecar_path = self.make_segment(
                root, 1, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()})
            sidecar = C.read_json(sidecar_path)
            sidecar["listings"]["1"][0]["采购价_详情"] = "999"
            with open(sidecar_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(sidecar, ensure_ascii=False))
            state_path = self.make_state(root, {"1": (manifest_path, 1)})
            segments = [{"order": 1, "top": "花卉绿植", "leaf": "鲜花",
                         "expected_count": 1}]
            with self.assertRaises(C.CaptureContractError) as ctx:
                self.merge(root, segments, state_path, full_count=1)
            self.assertIn("SHA-256", str(ctx.exception))

    def test_segment_without_sidecar_cannot_claim_multi_rows(self):
        # sidecar 缺失的产物必须被门禁挡住，不许把多 SKU 行当普通行聚合。
        with tempfile.TemporaryDirectory() as root:
            manifest_path, sidecar_path = self.make_segment(
                root, 1, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()})
            os.unlink(sidecar_path)
            report = category_pipeline.validate_segment(
                manifest_path, {"order": 1, "top": "花卉绿植", "leaf": "鲜花",
                                "expected_count": 1})
            self.assertFalse(report.ok)
            self.assertTrue(any("sidecar" in error for error in report.errors),
                            report.errors)


class AggregateSidecarMappingTests(SkuAggregationCase):
    """聚合 sidecar 的下标映射：跟随重编号后的主 CSV 序号。"""

    def test_listing_index_follows_renumbered_parent_row(self):
        with tempfile.TemporaryDirectory() as root:
            first, _ = self.make_segment(
                root, 1, "休闲零食", "薯片薯条", [card_row()])
            # 多 SKU 卡在分片里是第 1 张卡（序号 1），聚合后排在第 2 位。
            second, _ = self.make_segment(
                root, 2, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()})
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 1)})
            segments = [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                         "expected_count": 1},
                        {"order": 2, "top": "花卉绿植", "leaf": "鲜花",
                         "expected_count": 1}]
            manifest_path = self.merge(root, segments, state_path, full_count=2)
            report = self.aggregate_report(manifest_path)
            self.assertEqual([row["序号"] for row in report.rows], ["1", "2"])
            sidecar = C.read_json(report.manifest["artifacts"]["sku_path"])
            # 下标是聚合后的父卡序号（分片内是 1，聚合后是 2）。
            self.assertEqual(list(sidecar["listings"]), ["2"])
            parent = report.rows[1]
            for child in sidecar["listings"]["2"]:
                self.assertEqual(child["序号"], parent["序号"])
                self.assertEqual(child["商品名称"], parent["商品名称"])
                self.assertEqual(child["采集时间"], parent["采集时间"])
                self.assertEqual(C.row_evidence(
                    report.manifest["artifacts"]["csv_path"], child),
                    C.row_evidence(report.manifest["artifacts"]["csv_path"], parent))

    def test_multi_card_row_keeps_no_sku_detail_columns(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path, _ = self.make_segment(
                root, 1, "花卉绿植", "鲜花", [multi_card_row()],
                {1: two_sku_listing()})
            state_path = self.make_state(root, {"1": (manifest_path, 1)})
            segments = [{"order": 1, "top": "花卉绿植", "leaf": "鲜花",
                         "expected_count": 1}]
            output = self.merge(root, segments, state_path, full_count=1)
            report = self.aggregate_report(output)
            card = report.rows[0]
            self.assertEqual(card["状态"], C.MULTISKU_STATUS)
            for column in C.MULTISKU_CARD_FORBIDDEN_COLUMNS:
                self.assertEqual(C.text_value(card.get(column)), "",
                                 f"多 SKU 卡片行不得携带 {column}")
            self.assertEqual(report.manifest["scope"]["sku_mode"],
                             C.SKU_SIDECAR_MODE)
            self.assertEqual(
                report.manifest["artifacts"]["sku_sha256"],
                C.sha256_file(report.manifest["artifacts"]["sku_path"]))


class CaptureOnlyCliTests(SkuAggregationCase):
    """--capture-only：只保存 manifest 并退出，绝不执行入库流水线。"""

    def build_root(self, root):
        manifest_path, _ = self.make_segment(
            root, 1, "花卉绿植", "鲜花", [multi_card_row()],
            {1: two_sku_listing()})
        state_path = self.make_state(root, {"1": (manifest_path, 1)})
        inventory_path = os.path.join(root, "segments.json")
        C.atomic_write_json(inventory_path, {
            "segments": [{"order": 1, "top": "花卉绿植", "leaf": "鲜花",
                          "expected_count": 1}]})
        return inventory_path, state_path

    def test_capture_only_saves_manifest_without_calling_loader(self):
        with tempfile.TemporaryDirectory() as root:
            inventory_path, _ = self.build_root(root)
            argv = ["qnh_category_pipeline.py", "--segments-file", inventory_path,
                    "--out-dir", root, "--aggregate-only",
                    "--full-store-count", "1", "--capture-only"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(category_pipeline.subprocess, "run") as run:
                category_pipeline.main()
            # capture-only 不连库、不跑入库 dry-run。
            run.assert_not_called()
            merged = [os.path.join(root, name) for name in os.listdir(root)
                      if name.startswith("qnh_category_full")
                      and name.endswith(".manifest.json")]
            self.assertEqual(len(merged), 1)
            report = self.aggregate_report(merged[0])
            self.assertEqual(report.manifest["scope"]["sku_mode"],
                             C.SKU_SIDECAR_MODE)

    def test_default_run_still_calls_the_loader(self):
        with tempfile.TemporaryDirectory() as root:
            inventory_path, _ = self.build_root(root)
            argv = ["qnh_category_pipeline.py", "--segments-file", inventory_path,
                    "--out-dir", root, "--aggregate-only",
                    "--full-store-count", "1"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(category_pipeline.subprocess, "run") as run:
                category_pipeline.main()
            self.assertEqual(run.call_count, 1)
            command = run.call_args[0][0]
            self.assertIn("--from-manifest", command)


if __name__ == "__main__":
    unittest.main()
