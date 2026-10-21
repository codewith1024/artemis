"""跨分片观测终态聚合：异常分片聚合后仍是观测终态，绝不变成成功。

离线：不连设备、不连库、不入库（入库入口在连库之前就拒观测契约产物）。

要钉死的几条：
- 分片是观测终态（observed_with_exceptions）时，聚合也是观测终态：
  state=observed_with_exceptions、observation_complete=true、business_complete=false。
- 聚合的严格门禁必须拒绝它（它不是业务完整的快照）。
- 分片级计数差按类目登记成 segment_platform_count_mismatch，平台数不 lowered。
- 渠道行缺失凭据跟着卡重编号搬到聚合 sidecar（下标=聚合后序号）。
- 入库入口在打开数据库连接之前拒绝它。
"""

import csv
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import qnh_capture_common as C
import qnh_capture_pipeline as pipeline
import qnh_category_pipeline as category_pipeline
import qnh_load_db as loader
import qnh_sku_capture as K
from qnh_observation import parse_nodes

FIXTURES = Path(__file__).parents[1] / "fixtures"
CAPTURE_TIME = "2026-10-21T10:00:00"
PROFILE = "xiniu-changan"

# 逐屏列表证据（离线合成）：商品列表 ScrollView 的直接子 ViewGroup 就是卡容器。
LIST_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="{leaf}（{count}）" bounds="[0,100][300,160]" class="android.widget.TextView"/>
  <node class="android.widget.ScrollView" bounds="[380,200][1000,1800]">
{cards}
    <node text="已加载全部数据" bounds="[0,0][0,0]" class="android.widget.TextView"/>
  </node>
</hierarchy>
"""

LIST_CARD = """    <node bounds="[380,{top}][1000,{bottom}]" class="android.view.ViewGroup">
      <node text="{name}" bounds="[380,{top}][900,{bottom}]" class="android.widget.TextView"/>
      <node text="规格：{spec}" bounds="[380,{top}][900,{bottom}]" class="android.widget.TextView"/>
      <node text="条码:{barcode}" bounds="[380,{top}][900,{bottom}]" class="android.widget.TextView"/>
    </node>"""


def list_evidence_for(directory, cards, *, leaf, category, platform_count):
    """合成逐屏列表证据：首屏 [0:m]、中屏 [m-1:]、末屏与中屏一致（证明读尽）。

    末两屏一致 + 末屏带「已加载全部数据」才证明列表读尽；相邻屏必须有边界
    重叠，否则门禁复解析时判「缺页或中间有漏采」。
    """
    total = len(cards)
    middle = max(1, total // 2)
    pages = [cards[:middle], cards[middle - 1:], cards[middle - 1:]]
    page_files = []
    for index, page in enumerate(pages, 1):
        body = []
        for position, card in enumerate(page):
            top = 200 + position * 28
            body.append(LIST_CARD.format(top=top, bottom=top + 20, **card))
        path = os.path.join(directory, f"page_{index:03d}.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(LIST_PAGE.format(leaf=leaf, count=platform_count,
                                      cards="\n".join(body)))
        page_files.append({"path": path, "sha256": C.sha256_file(path)})
    evidence = {"schema_version": C.LIST_EVIDENCE_SCHEMA_VERSION,
                "category": category, "platform_count": platform_count,
                "cards": [dict(card) for card in cards],
                "list_exhausted": True, "pages": page_files}
    path = os.path.join(directory, "inventory.json")
    C.atomic_write_json(path, evidence)
    return path


def complete_row(index=1, name="商品A", barcode=None, spec="100g"):
    """单 SKU 卡片行：40 列齐全（每个商品给不同 SKU/条码，避免造出假的身份冲突）。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name,
        "条码": barcode if barcode is not None else f"6900000000{index:03d}",
        "店内码": "A-1", "货号": "H-1", "状态": "成功", "视频时间点": "",
        "时刻": "10:00:00", "采集时间": CAPTURE_TIME, "证据前缀": "",
        "规格": spec, "月售": "10", "库存": "2", "售罄": "否", "活动品": "否",
        "美团闪购价": "10", "饿了么价": "10", "京东到家价": "10",
        "采购价": "5/袋", "价格原文": "10 | 10 | 10 | 5/袋",
        "开放平台标识": "OP-1", "SPU": f"SPU-{index}", "SKU": f"SKU-{index}",
        "重量": "100克(g)",
        "采购价_详情": "5", "采购单位_详情": "袋",
        "美团闪购状态": "已上架", "美团闪购零售价": "10", "美团闪购库存": "2",
        "饿了么状态": "已下架", "饿了么零售价": "10", "饿了么库存": "1",
        "京东到家状态": "未上线", "京东到家零售价": "10", "京东到家库存": "0",
        "卖点": "", "可售日期": "不限", "礼袋": "0个礼袋",
        "是否力荐": "否", "是否标品": "标品",
    })
    return row


def multi_card_row(index=1, name="多规格商品"):
    """多 SKU 卡片行：只有列表卡级字段，SKU 级列全空。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": "", "店内码": "", "货号": "",
        "状态": C.MULTISKU_STATUS, "视频时间点": "", "时刻": "10:00:00",
        "采集时间": CAPTURE_TIME, "证据前缀": "",
        "规格": "鳕鱼虾味1kg/鳕鱼虾味40g", "月售": "10", "库存": "2",
        "售罄": "否", "活动品": "否", "美团闪购价": "59", "饿了么价": "59",
        "京东到家价": "59", "采购价": "28.2/单品", "价格原文": "59 | 59 | 59 | 28.2/单品",
    })
    return row


def fixture_listing(card):
    """用真机详情夹具合成 sidecar 子行（含真实的渠道行缺失凭据）。

    夹具里 SKU 2098339639743791201（鳕鱼虾味40g）在「京东到家」渠道表里确实
    没有行——凭据挂的就是这份原始 XML，门禁会自己复解析一遍。
    """
    path = FIXTURES / "qnh_wowo_channel_absent.xml"
    details = K.parse_sku_details(parse_nodes(path.read_text()))
    children = []
    for detail in details:
        child = dict(card)
        child.update({key: value for key, value in detail.items()
                      if key in C.DETAIL_COLUMNS or key in ("规格", "条码")})
        child["状态"] = "成功"
        child["店内码"] = ""
        child["货号"] = ""
        children.append(child)
    observations = {}
    for detail in details:
        for channel, entry in (detail.get("_channel_observations") or {}).items():
            observations.setdefault(str(detail["SKU"]), {})[channel] = {
                **entry,
                "evidence_pages": [{"path": str(path),
                                    "sha256": C.sha256_file(path)}],
            }
    return children, observations


class ObservedAggregationCase(unittest.TestCase):
    """搭一套分片产物（CSV + sidecar + manifest），全部落在临时目录。"""

    def make_segment(self, root, order, top, leaf, rows, *, state="success",
                     platform_count=None, exceptions=None, listings=None,
                     observations=None, evidence=None):
        directory = os.path.join(root, f"{order:03d}")
        os.makedirs(directory, exist_ok=True)
        prefix = os.path.join(directory, "qnh_scan_segment")
        csv_path = prefix + ".csv"
        for index, row in enumerate(rows, 1):
            row["序号"] = str(index)
            row["证据前缀"] = prefix
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        scope = {"list_title": leaf, "category": f"{top}/{leaf}",
                 "expected_product_count": (platform_count if platform_count is not None
                                            else len(rows)),
                 "requested_count": 0}
        artifacts = {"csv_path": csv_path, "evidence_prefix": prefix,
                     "video_path": None, "csv_sha256": C.sha256_file(csv_path)}
        if state == category_pipeline.OBSERVED_STATE and not evidence:
            # 例外态一律要逐屏列表证据：不短收（只有渠道行缺失）同样要证
            # 「页面已采全」。
            evidence = list_evidence_for(
                directory,
                [{"name": row["商品名称"], "spec": row["规格"],
                  "barcode": row.get("条码", "")} for row in rows],
                leaf=leaf, category=f"{top}/{leaf}",
                platform_count=scope["expected_product_count"])
        if evidence:
            artifacts["list_evidence_path"] = evidence
            artifacts["list_evidence_sha256"] = C.sha256_file(evidence)
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()), state=state,
            profile=C.load_profile(PROFILE), device_id="R28M31LJX0E",
            scope=scope,
            capture={"method": "app_ui", "started_at": CAPTURE_TIME,
                     "run_started_at": CAPTURE_TIME, "finished_at": CAPTURE_TIME,
                     "collector_version": "test", "pace": "ultra",
                     "tall_screen": True, "verify_every": 1,
                     "checkpoint_every": 25, "record": False,
                     "dismiss_order_dialogs": False},
            artifacts=artifacts, rows=rows,
            detail_complete_count=category_pipeline._merged_detail_complete_count(
                rows, listings or {}))
        if listings:
            # 子行跟父卡严格同步（序号/证据前缀/采集时间）：扫描器也是这么写的。
            listings = {
                key: [dict(child, 序号=key, 证据前缀=prefix,
                           采集时间=rows[int(key) - 1]["采集时间"])
                      for child in children]
                for key, children in listings.items()
            }
            payload = {"schema_version": C.SKU_SIDECAR_SCHEMA_VERSION,
                       "listings": listings}
            if observations:
                payload["observations"] = observations
            sidecar_path = prefix + ".skus.json"
            C.atomic_write_json(sidecar_path, payload)
            manifest["artifacts"]["sku_path"] = sidecar_path
            manifest["artifacts"]["sku_sha256"] = C.sha256_file(sidecar_path)
            manifest["scope"]["sku_mode"] = C.SKU_SIDECAR_MODE
            manifest["progress"]["listing_count"] = len(rows)
            manifest["progress"]["sku_row_count"] = sum(
                len((listings or {}).get(row["序号"], []))
                if C.is_multisku_row(row) else 1 for row in rows)
        if state == category_pipeline.OBSERVED_STATE:
            manifest["capture"]["observation_contract"] = C.OBSERVATION_CONTRACT_VERSION
            manifest["progress"].update({
                "observation_complete": True,
                "business_complete": False,
                "exceptions": exceptions or [],
            })
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest)
        return manifest_path

    def make_state(self, root, completed):
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

    def merge(self, root, segments, state_path, *, full_count, allow_observed=True):
        return category_pipeline.merge_segments(
            root, {"full_store_count": full_count}, segments, state_path,
            full_count=full_count, allow_observed=allow_observed)

    def observed_count_segment(self, root, order=1, top="萌宠专区", leaf="猫粮",
                               platform_count=52, visible=51, start=1):
        """带平台计数差凭据的观测终态分片。"""
        rows = [complete_row(index, f"商品{index}")
                for index in range(start, start + visible)]
        directory = os.path.join(root, f"{order:03d}")
        os.makedirs(directory, exist_ok=True)
        evidence = list_evidence_for(
            directory,
            [{"name": row["商品名称"], "spec": row["规格"], "barcode": row["条码"]}
             for row in rows],
            leaf=leaf, category=f"{top}/{leaf}", platform_count=platform_count)
        exceptions = [{"type": "platform_count_mismatch",
                       "platform_count": platform_count,
                       "visible_card_count": visible}]
        path = self.make_segment(root, order, top, leaf, rows,
                                  state=category_pipeline.OBSERVED_STATE,
                                  platform_count=platform_count,
                                  exceptions=exceptions, evidence=evidence)
        return path, rows


class ObservedAggregateTests(ObservedAggregationCase):
    """观测终态分片 → 观测终态聚合。"""

    def test_observed_segment_aggregates_into_observed_aggregate(self):
        with tempfile.TemporaryDirectory() as root:
            path, rows = self.observed_count_segment(root)
            state_path = self.make_state(root, {"1": (path, 52)})
            segments = [{"order": 1, "top": "萌宠专区", "leaf": "猫粮",
                         "expected_count": 52}]
            manifest_path = self.merge(root, segments, state_path, full_count=51)
            manifest = C.read_json(manifest_path)
            # 观测终态不许变成成功。
            self.assertEqual(manifest["state"], category_pipeline.OBSERVED_STATE)
            self.assertTrue(manifest["progress"]["observation_complete"])
            self.assertFalse(manifest["progress"]["business_complete"])
            self.assertEqual(manifest["capture"]["observation_contract"], "v1")
            # 宽松门禁通过。
            relaxed = C.validate_manifest(manifest_path, require_complete=False,
                                          expected_profile=C.load_profile(PROFILE))
            self.assertTrue(relaxed.ok, relaxed.errors)
            # 严格门禁必须拒绝。
            strict = C.validate_manifest(manifest_path, require_complete=True,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertFalse(strict.ok)
            self.assertTrue(any("不是 success" in error for error in strict.errors),
                            strict.errors)

    def test_segment_count_mismatch_is_recorded_with_its_category(self):
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.observed_count_segment(root, platform_count=52, visible=51)
            state_path = self.make_state(root, {"1": (path, 52)})
            segments = [{"order": 1, "top": "萌宠专区", "leaf": "猫粮",
                         "expected_count": 52}]
            manifest_path = self.merge(root, segments, state_path, full_count=51)
            manifest = C.read_json(manifest_path)
            exceptions = manifest["progress"]["exceptions"]
            counted = [item for item in exceptions
                       if item["type"] == "segment_platform_count_mismatch"]
            self.assertEqual(len(counted), 1)
            self.assertEqual(counted[0]["segment"], "萌宠专区/猫粮")
            # 平台数原样保留：52 不许改成 51。
            self.assertEqual(counted[0]["platform_count"], 52)
            self.assertEqual(counted[0]["visible_card_count"], 51)
            # 分片记录里平台目标数也是 52，采集数 51，差看得见。
            segment = manifest["scope"]["segments"][0]
            self.assertEqual(segment["expected_count"], 52)
            self.assertEqual(segment["captured_count"], 51)
            self.assertEqual(segment["state"], category_pipeline.OBSERVED_STATE)

    def test_channel_row_absent_credential_follows_the_renumbered_card(self):
        with tempfile.TemporaryDirectory() as root:
            # 第一片：一张普通卡（多 SKU 卡排在它后面，序号从 1 挪到 2）。
            first = self.make_segment(root, 1, "休闲零食", "薯片薯条",
                                      [complete_row(1, "普通商品")])
            card = multi_card_row()
            children, observations = fixture_listing(card)
            listings = {"1": [{**child, "序号": "1"} for child in children]}
            card_observations = {"1": observations}
            second = self.make_segment(
                root, 2, "萌宠专区", "猫粮", [card],
                state=category_pipeline.OBSERVED_STATE,
                platform_count=1, exceptions=[{
                    "type": "channel_row_absent",
                    "sku": "2098339639743791201", "channel": "京东到家",
                    "spec": "鳕鱼虾味40g"}],
                listings=listings, observations=card_observations)
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 1)})
            segments = [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                         "expected_count": 1},
                        {"order": 2, "top": "萌宠专区", "leaf": "猫粮",
                         "expected_count": 1}]
            manifest_path = self.merge(root, segments, state_path, full_count=2)
            manifest = C.read_json(manifest_path)
            self.assertEqual(manifest["state"], category_pipeline.OBSERVED_STATE)
            sidecar = C.read_json(manifest["artifacts"]["sku_path"])
            # 凭据跟着卡重编号：分片里是 1，聚合后是 2。
            self.assertEqual(list(sidecar["listings"]), ["2"])
            self.assertIn("2", sidecar["observations"])
            self.assertNotIn("1", sidecar["observations"])
            entry = sidecar["observations"]["2"]["2098339639743791201"]["京东到家"]
            self.assertEqual(entry["observation"], "row_absent")
            self.assertTrue(entry["evidence_pages"])
            # 聚合的例外清单里有这条缺行。
            absent = [item for item in manifest["progress"]["exceptions"]
                      if item["type"] == "channel_row_absent"]
            self.assertEqual(len(absent), 1)
            self.assertEqual(absent[0]["sku"], "2098339639743791201")
            self.assertEqual(absent[0]["channel"], "京东到家")
            self.assertEqual(absent[0]["spec"], "鳕鱼虾味40g")
            # 聚合的例外也要能单读自证：证据指针跟着凭据一起搬过来。
            self.assertEqual(absent[0]["evidence_pages"],
                             entry["evidence_pages"])
            self.assertEqual(absent[0]["section_status"], "已上架")
            relaxed = C.validate_manifest(manifest_path, require_complete=False,
                                          expected_profile=C.load_profile(PROFILE))
            self.assertTrue(relaxed.ok, relaxed.errors)
            self.assertFalse([warning for warning in relaxed.warnings
                              if "evidence_pages" in warning], relaxed.warnings)

    def test_mixed_success_and_observed_segments_keep_every_row(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.make_segment(root, 1, "休闲零食", "薯片薯条",
                                      [complete_row(1, "普通商品")])
            second, _ = self.observed_count_segment(root, order=2, leaf="猫粮",
                                                    platform_count=3, visible=2,
                                                    start=2)
            state_path = self.make_state(root, {"1": (first, 1), "2": (second, 3)})
            segments = [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                         "expected_count": 1},
                        {"order": 2, "top": "萌宠专区", "leaf": "猫粮",
                         "expected_count": 3}]
            manifest_path = self.merge(root, segments, state_path, full_count=3)
            manifest = C.read_json(manifest_path)
            # 有一片是观测终态，聚合整体就是观测终态。
            self.assertEqual(manifest["state"], category_pipeline.OBSERVED_STATE)
            self.assertEqual(manifest["progress"]["row_count"], 3)
            self.assertEqual(manifest["scope"]["raw_segment_product_count"], 3)
            states = {item["leaf"]: item["state"] for item in manifest["scope"]["segments"]}
            self.assertEqual(states, {"薯片薯条": "success", "猫粮": "observed_with_exceptions"})
            relaxed = C.validate_manifest(manifest_path, require_complete=False,
                                          expected_profile=C.load_profile(PROFILE))
            self.assertTrue(relaxed.ok, relaxed.errors)

    def test_success_only_aggregate_is_still_success(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.make_segment(root, 1, "休闲零食", "薯片薯条",
                                     [complete_row(1, "普通商品")])
            state_path = self.make_state(root, {"1": (path, 1)})
            segments = [{"order": 1, "top": "休闲零食", "leaf": "薯片薯条",
                         "expected_count": 1}]
            manifest_path = self.merge(root, segments, state_path, full_count=1,
                                       allow_observed=True)
            manifest = C.read_json(manifest_path)
            self.assertEqual(manifest["state"], "success")
            strict = C.validate_manifest(manifest_path, require_complete=True,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertTrue(strict.ok, strict.errors)

    def test_observed_segment_is_still_rejected_without_the_observation_flag(self):
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.observed_count_segment(root)
            state_path = self.make_state(root, {"1": (path, 52)})
            segments = [{"order": 1, "top": "萌宠专区", "leaf": "猫粮",
                         "expected_count": 52}]
            with self.assertRaises(C.CaptureContractError) as ctx:
                self.merge(root, segments, state_path, full_count=51,
                           allow_observed=False)
            self.assertIn("observed_with_exceptions", str(ctx.exception))
            self.assertFalse([name for name in os.listdir(root)
                              if name.startswith("qnh_category_full")])

    def test_fabricated_segment_count_mismatch_is_rejected(self):
        # 聚合里的分片计数差必须对得上聚合自己的分片记录：改数就废。
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.observed_count_segment(root)
            state_path = self.make_state(root, {"1": (path, 52)})
            segments = [{"order": 1, "top": "萌宠专区", "leaf": "猫粮",
                         "expected_count": 52}]
            manifest_path = self.merge(root, segments, state_path, full_count=51)
            manifest = C.read_json(manifest_path)
            for exception in manifest["progress"]["exceptions"]:
                if exception["type"] == "segment_platform_count_mismatch":
                    exception["platform_count"] = 99
            C.atomic_write_json(manifest_path, manifest, overwrite=True)
            report = C.validate_manifest(manifest_path, require_complete=False,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertFalse(report.ok)
            self.assertTrue(any("platform_count" in error for error in report.errors),
                            report.errors)

    def test_segment_count_mismatch_for_an_unknown_segment_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.observed_count_segment(root)
            state_path = self.make_state(root, {"1": (path, 52)})
            segments = [{"order": 1, "top": "萌宠专区", "leaf": "猫粮",
                         "expected_count": 52}]
            manifest_path = self.merge(root, segments, state_path, full_count=51)
            manifest = C.read_json(manifest_path)
            for exception in manifest["progress"]["exceptions"]:
                if exception["type"] == "segment_platform_count_mismatch":
                    exception["segment"] = "萌宠专区/不存在的类目"
            C.atomic_write_json(manifest_path, manifest, overwrite=True)
            report = C.validate_manifest(manifest_path, require_complete=False,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertFalse(report.ok)
            self.assertTrue(any("不在聚合的分片清单里" in error
                                for error in report.errors), report.errors)


class ManifestEvidenceCrossCheckTests(ObservedAggregationCase):
    """F6 的门禁侧：manifest 的例外证据必须与 sidecar 凭据一致。"""

    def build(self, root):
        """一片带渠道行缺失凭据的观测分片（凭据挂真机详情夹具）。"""
        card = multi_card_row()
        children, observations = fixture_listing(card)
        listings = {"1": [{**child, "序号": "1"} for child in children]}
        pages = observations["2098339639743791201"]["京东到家"]["evidence_pages"]
        exceptions = [{"type": "channel_row_absent", "sku": "2098339639743791201",
                       "channel": "京东到家", "spec": "鳕鱼虾味40g",
                       "evidence_pages": pages}]
        path = self.make_segment(
            root, 1, "萌宠专区", "猫粮", [card],
            state=category_pipeline.OBSERVED_STATE, platform_count=1,
            exceptions=exceptions, listings=listings,
            observations={"1": observations})
        return path, pages

    def test_matching_evidence_passes(self):
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.build(root)
            report = C.validate_manifest(path, require_complete=False,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertTrue(report.ok, report.errors)

    def test_evidence_pointing_elsewhere_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path, pages = self.build(root)
            manifest = C.read_json(path)
            for exception in manifest["progress"]["exceptions"]:
                if exception["type"] == "channel_row_absent":
                    exception["evidence_pages"] = [
                        {"path": pages[0]["path"], "sha256": "b" * 64}]
            C.atomic_write_json(path, manifest, overwrite=True)
            report = C.validate_manifest(path, require_complete=False,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertFalse(report.ok)
            self.assertTrue(any("evidence_pages 与 sidecar 凭据不一致" in error
                                for error in report.errors), report.errors)

    def test_missing_evidence_pointer_is_only_a_warning(self):
        # 旧产物（manifest 的例外没带证据指针）仍放行，但必须看得见。
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.build(root)
            manifest = C.read_json(path)
            for exception in manifest["progress"]["exceptions"]:
                if exception["type"] == "channel_row_absent":
                    exception.pop("evidence_pages", None)
            C.atomic_write_json(path, manifest, overwrite=True)
            report = C.validate_manifest(path, require_complete=False,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertTrue(report.ok, report.errors)
            self.assertTrue(any("没有 evidence_pages" in warning
                                for warning in report.warnings), report.warnings)


class ObservedListEvidenceTests(ObservedAggregationCase):
    """F4 的门禁侧：例外态一律要逐屏列表证据，且身份要对得上。"""

    def build(self, root):
        card = multi_card_row()
        children, observations = fixture_listing(card)
        listings = {"1": [{**child, "序号": "1"} for child in children]}
        exceptions = [{"type": "channel_row_absent", "sku": "2098339639743791201",
                       "channel": "京东到家", "spec": "鳕鱼虾味40g"}]
        path = self.make_segment(
            root, 1, "萌宠专区", "猫粮", [card],
            state=category_pipeline.OBSERVED_STATE, platform_count=1,
            exceptions=exceptions, listings=listings,
            observations={"1": observations})
        return path

    def test_observed_manifest_without_list_evidence_is_rejected(self):
        # 不短收（只有渠道行缺失）同样要证「页面已采全」。
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root)
            manifest = C.read_json(path)
            manifest["artifacts"].pop("list_evidence_path", None)
            manifest["artifacts"].pop("list_evidence_sha256", None)
            C.atomic_write_json(path, manifest, overwrite=True)
            report = C.validate_manifest(path, require_complete=False,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertFalse(report.ok)
            self.assertTrue(any("list_evidence_path" in error for error in report.errors),
                            report.errors)

    def test_evidence_with_same_count_but_other_identity_is_rejected(self):
        # 张数一样、身份不同：采到的不是这一页的东西，证据不能放行。
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root)
            manifest = C.read_json(path)
            evidence_path = manifest["artifacts"]["list_evidence_path"]
            evidence = C.read_json(evidence_path)
            # 把逐屏原始 XML 换成另一张卡（张数不变），并同步所有哈希：
            # 复解析结果与证据 JSON 彼此一致，骗不过门禁——只有「证据的卡 ≠
            # 采集行的卡」这一层能拦住。
            for page in evidence["pages"]:
                with open(page["path"], encoding="utf-8") as fh:
                    xml = fh.read()
                xml = xml.replace("多规格商品", "另一个商品")
                with open(page["path"], "w", encoding="utf-8") as fh:
                    fh.write(xml)
                page["sha256"] = C.sha256_file(page["path"])
            evidence["cards"] = [{"name": "另一个商品",
                                  "spec": multi_card_row()["规格"],
                                  "barcode": ""}]
            C.atomic_write_json(evidence_path, evidence, overwrite=True)
            manifest["artifacts"]["list_evidence_sha256"] = C.sha256_file(evidence_path)
            C.atomic_write_json(path, manifest, overwrite=True)
            report = C.validate_manifest(path, require_complete=False,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertFalse(report.ok)
            self.assertTrue(any("列表证据卡片与采集行不匹配" in error
                                for error in report.errors), report.errors)


class ObservedAggregateLoaderTests(ObservedAggregationCase):
    """观测终态聚合不许入库：连库之前就拒。"""

    def test_loader_rejects_observed_aggregate_before_connecting(self):
        opened = []

        def boom():
            opened.append(True)
            raise AssertionError("loader 不该连接数据库")

        original = loader._open_connection
        loader._open_connection = boom
        try:
            with tempfile.TemporaryDirectory() as root:
                path, _ = self.observed_count_segment(root)
                state_path = self.make_state(root, {"1": (path, 52)})
                segments = [{"order": 1, "top": "萌宠专区", "leaf": "猫粮",
                             "expected_count": 52}]
                manifest_path = self.merge(root, segments, state_path,
                                           full_count=51)
                with self.assertRaises(C.CaptureContractError) as ctx:
                    loader.load_capture(manifest_path=manifest_path)
                self.assertIn("观测契约产物不入库", str(ctx.exception))
        finally:
            loader._open_connection = original
        self.assertEqual(opened, [])

    def test_capture_pipeline_refuses_aggregate_and_points_at_the_sharder(self):
        # 聚合产物不是一次采集：既不能续跑也不该被建议「重新采集一次」，
        # 得指向分片器的观测模式。
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.observed_count_segment(root)
            state_path = self.make_state(root, {"1": (path, 52)})
            segments = [{"order": 1, "top": "萌宠专区", "leaf": "猫粮",
                         "expected_count": 52}]
            manifest_path = self.merge(root, segments, state_path, full_count=51)
            argv = ["qnh_capture_pipeline.py", "--profile", PROFILE,
                    "--from-manifest", manifest_path]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(pipeline.loader, "load_capture") as load_capture:
                with self.assertRaises(SystemExit) as raised:
                    pipeline.main()
            self.assertEqual(raised.exception.code, 2)
            load_capture.assert_not_called()
            with open(pipeline._result_path(manifest_path), encoding="utf-8") as fh:
                result = json.load(fh)
            self.assertNotIn("resume_command", result)
            self.assertIn("qnh_category_pipeline.py", result["restart_command"])
            self.assertIn("--observe-exceptions", result["restart_command"])


REAL_ROOT = Path(__file__).parents[2] / "qnh_acceptance" / "20261021" / "observations"
REAL_SEGMENTS = [
    (1, "萌宠专区", "猫粮",
     REAL_ROOT / "cat_food" / "qnh_scan_20261021-165935_ae749bb2.manifest.json", 52),
    (2, "萌宠专区", "猫零食",
     REAL_ROOT / "cat_snacks" / "qnh_scan_20261021-171203_02009d89.manifest.json", 44),
]


@unittest.skipUnless(all(path.is_file() for *_, path, _ in REAL_SEGMENTS),
                     "真机观测产物不在本次工作目录里")
class RealObservedSegmentsAggregationTests(ObservedAggregationCase):
    """真机两片观测终态分片（猫粮 51 卡/65 SKU、猫零食 43 卡/47 SKU）离线聚合。"""

    def test_real_observed_segments_aggregate_into_observed_aggregate(self):
        with tempfile.TemporaryDirectory() as root:
            completed = {}
            segments = []
            for order, top, leaf, path, count in REAL_SEGMENTS:
                completed[str(order)] = (str(path), count)
                segments.append({"order": order, "top": top, "leaf": leaf,
                                 "expected_count": count})
            state_path = self.make_state(root, completed)
            manifest_path = self.merge(root, segments, state_path, full_count=92)
            manifest = C.read_json(manifest_path)
            self.assertEqual(manifest["state"], category_pipeline.OBSERVED_STATE)
            self.assertFalse(manifest["progress"]["business_complete"])
            relaxed = C.validate_manifest(manifest_path, require_complete=False,
                                          expected_profile=C.load_profile(PROFILE))
            self.assertTrue(relaxed.ok, relaxed.errors)
            strict = C.validate_manifest(manifest_path, require_complete=True,
                                         expected_profile=C.load_profile(PROFILE))
            self.assertFalse(strict.ok)
            # 猫粮 51 + 猫零食 43 = 94 张卡，其中 2 张跨类目重复（同一 SKU 同一
            # 规格在两个叶子类目都出现）→ 并集 92 张。SKU 行 65 + 47 - 2 = 110。
            self.assertEqual(manifest["progress"]["listing_count"], 92)
            self.assertEqual(manifest["progress"]["sku_row_count"], 110)
            self.assertEqual(manifest["scope"]["raw_segment_product_count"], 94)
            self.assertEqual(manifest["scope"]["cross_category_duplicate_count"], 2)
            # 含未知子行的两张卡不算详情完整：92 - 2 = 90。
            self.assertEqual(manifest["progress"]["detail_complete_count"], 90)
            # 两片的平台计数差都登记了，平台数不 lowered。
            counted = {item["segment"]: item
                       for item in manifest["progress"]["exceptions"]
                       if item["type"] == "segment_platform_count_mismatch"}
            self.assertEqual(counted["萌宠专区/猫粮"]["platform_count"], 52)
            self.assertEqual(counted["萌宠专区/猫粮"]["visible_card_count"], 51)
            self.assertEqual(counted["萌宠专区/猫零食"]["platform_count"], 44)
            self.assertEqual(counted["萌宠专区/猫零食"]["visible_card_count"], 43)
            # 猫粮的 3 条渠道行缺失凭据都搬过来了。
            absent = [item for item in manifest["progress"]["exceptions"]
                      if item["type"] == "channel_row_absent"]
            self.assertEqual(len(absent), 3)
            self.assertEqual({item["sku"] for item in absent},
                             {"2098339639743791201", "2112073004715569247"})
            sidecar = C.read_json(manifest["artifacts"]["sku_path"])
            self.assertTrue(sidecar["observations"])
            for seq, block in sidecar["observations"].items():
                self.assertIn(seq, sidecar["listings"])


if __name__ == "__main__":
    unittest.main()
