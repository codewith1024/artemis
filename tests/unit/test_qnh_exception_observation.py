"""本地异常（观测）契约：渠道行缺失记「未知」+ 平台计数对账（本地，不连库）。

契约（纯本地，不连数据库、不碰设备）：
- 详情页某渠道整行缺失时，那个渠道记「未知」，价/库存留空——绝不按 0 假填。
  「未知」只在例外态（state != success）+ capture.observation_contract='v1'
  + 有精确匹配的观测凭据时才通过本地门禁；普通 success 里的未知依旧失败。
- 凭据不是一句 json：sidecar observations{str(卡片序号): {str(SKU): {中文渠道:
  {observation:'row_absent', section_status, reason, evidence_pages:[{path,
  sha256}]}}}}。门禁逐页校验 hash，并独立复解析原始 detail XML：渠道块在、
  块状态相符、且这一 SKU 真没有行，才认。编造的行缺失会被复解析打回。
- 平台标题数与列表可见卡片数不一致时：state=observed_with_exceptions、
  progress.observation_complete=true / business_complete=false / exceptions 逐条
  登记，且必须给出逐屏列表证据（artifacts.list_evidence_path/hash）：证据 JSON
  里的 platform_count/cards/list_exhausted 必须与重新复解析原始列表页 XML 的
  结果一一相等，采集行的卡片身份多重集也要等于 cards（不是 set 相等）。
- require_complete=True 仍然严格失败：例外态不是 success，不许因为它放行。
- loader：观测契约产物（state 非 success 或 business_complete 不为 true）在
  打开数据库连接之前就拒——连都不连，更不许改库。
"""

import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import types
import unittest
import uuid
import xml.etree.ElementTree as ET

import qnh_capture_common as C
import qnh_load_db as loader

CAPTURE_TIME = "2026-10-21T10:00:00"
BOUNDS_PAT = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# 详情页原始 XML 片段：美团闪购/饿了么/京东到家三个渠道块都只给「粉色牡丹」这一行，
# 「香槟色牡丹」在渠道表里没有行 —— 这就是 row_absent 的真实证据。
DETAIL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node text="已下架(美团闪购)" bounds="[0,300][1080,360]" />
  <node text="粉色牡丹一束（5朵花）" bounds="[40,380][400,430]" />
  <node text="美团闪购" bounds="[420,380][600,430]" />
  <node text="￥14.5" bounds="[620,380][700,430]" />
  <node text="1" bounds="[720,380][760,430]" />
  <node text="已下架(饿了么)" bounds="[0,450][1080,510]" />
  <node text="粉色牡丹一束（5朵花）" bounds="[40,530][400,580]" />
  <node text="饿了么" bounds="[420,530][600,580]" />
  <node text="￥14.5" bounds="[620,530][700,580]" />
  <node text="1" bounds="[720,530][760,580]" />
  <node text="已下架(京东到家)" bounds="[0,600][1080,660]" />
  <node text="粉色牡丹一束（5朵花）" bounds="[40,680][400,730]" />
  <node text="京东到家" bounds="[420,680][600,730]" />
  <node text="￥14.5" bounds="[620,680][700,730]" />
  <node text="1" bounds="[720,680][760,730]" />
  <node text="其他信息" bounds="[0,900][1080,960]" />
</hierarchy>
"""

# 同一页，但渠道表里两个 SKU 都有行：用来证明「凭据说缺行」会被复解析打回。
DETAIL_XML_WITH_BOTH = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node text="已下架(美团闪购)" bounds="[0,300][1080,360]" />
  <node text="粉色牡丹一束（5朵花）" bounds="[40,380][400,430]" />
  <node text="美团闪购" bounds="[420,380][600,430]" />
  <node text="￥14.5" bounds="[620,380][700,430]" />
  <node text="1" bounds="[720,380][760,430]" />
  <node text="香槟色牡丹一束（一朵花）" bounds="[40,440][400,490]" />
  <node text="美团闪购" bounds="[420,440][600,490]" />
  <node text="￥14.5" bounds="[620,440][700,490]" />
  <node text="1" bounds="[720,440][760,490]" />
  <node text="已下架(饿了么)" bounds="[0,520][1080,580]" />
  <node text="粉色牡丹一束（5朵花）" bounds="[40,600][400,650]" />
  <node text="饿了么" bounds="[420,600][600,650]" />
  <node text="￥14.5" bounds="[620,600][700,650]" />
  <node text="1" bounds="[720,600][760,650]" />
  <node text="已下架(京东到家)" bounds="[0,680][1080,740]" />
  <node text="粉色牡丹一束（5朵花）" bounds="[40,760][400,810]" />
  <node text="京东到家" bounds="[420,760][600,810]" />
  <node text="￥14.5" bounds="[620,760][700,810]" />
  <node text="1" bounds="[720,760][760,810]" />
  <node text="其他信息" bounds="[0,900][1080,960]" />
</hierarchy>
"""

# 列表页原始 XML 片段（列表证据的 pages 指向它）。
LIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node text="全部商品 52" bounds="[0,100][1080,160]" />
  <node text="猫粮A" bounds="[40,200][400,250]" />
  <node text="猫粮B" bounds="[40,300][400,350]" />
</hierarchy>
"""


def _parse_nodes(xml):
    """qnh_observation.parse_nodes 的契约替身：XML → 节点字典列表。

    节点形状与 scanner.parse_nodes 一致（qnh_sku_capture 只用到 text 与
    坐标），真实模块就位后本契约不变。
    """
    root = ET.fromstring(xml)
    nodes = []
    for element in root.iter("node"):
        match = BOUNDS_PAT.match(element.get("bounds", ""))
        if not match:
            continue
        x1, y1, x2, y2 = (int(value) for value in match.groups())
        nodes.append({
            "text": (element.get("text") or "").strip(),
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
        })
    return nodes


class _ObservationStub:
    """qnh_observation.audit_pages 的可替换替身（列表复解析结果）。"""

    platform_count = 3
    cards = []
    list_exhausted = True
    seen_pages = None
    raise_error = None


def _audit_pages(pages, category):
    _ObservationStub.seen_pages = list(pages)
    if _ObservationStub.raise_error:
        raise _ObservationStub.raise_error
    return {
        "category": category,
        "platform_count": _ObservationStub.platform_count,
        "cards": [dict(card) for card in _ObservationStub.cards],
        "list_exhausted": _ObservationStub.list_exhausted,
    }


def _install_observation_stub():
    """用真实的 qnh_observation（parse_nodes 照真跑），只把 audit_pages 换成替身。

    列表复解析有自己的测试；这里要的是「门禁只认复解析结果」这条契约，所以
    audit_pages 用可控替身，其余（detail XML 复解析）走真实实现。
    """
    try:
        import qnh_observation as module
    except ImportError:  # pragma: no cover - 模块未就绪时的兜底
        module = types.ModuleType("qnh_observation")
        module.parse_nodes = _parse_nodes
        module.audit_pages = _audit_pages
        sys.modules["qnh_observation"] = module
        return module, None
    original = module.audit_pages
    module.audit_pages = _audit_pages
    return module, original


def _remove_observation_stub(module, original):
    if module is None:
        sys.modules.pop("qnh_observation", None)
    elif original is None:
        sys.modules.pop("qnh_observation", None)
    else:
        module.audit_pages = original


def card_row(index=1, name="商品A", barcode="6900000000001", spec="100g",
             status="成功"):
    """普通单 SKU 卡片行（40 列齐全）。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": barcode,
        "店内码": f"A-{index}", "货号": f"H-{index}", "状态": status,
        "视频时间点": "", "时刻": "10:00:00", "采集时间": CAPTURE_TIME,
        "证据前缀": "", "规格": spec, "月售": "10", "库存": "2", "售罄": "否",
        "活动品": "否", "美团闪购价": "10", "饿了么价": "10", "京东到家价": "10",
        "采购价": "5/袋", "价格原文": "10 | 10 | 10 | 5/袋",
        "开放平台标识": f"OP-{index}", "SPU": f"SPU-{index}",
        "SKU": f"SKU-{index}", "重量": "100克(g)",
        "采购价_详情": "5", "采购单位_详情": "袋",
        "美团闪购状态": "已上架", "美团闪购零售价": "10", "美团闪购库存": "2",
        "饿了么状态": "已下架", "京东到家状态": "未上线",
        "可售日期": "不限", "礼袋": "0个礼袋", "是否力荐": "否", "是否标品": "标品",
    })
    return row


def multi_card_row(index=1, name="多规格牡丹"):
    """多 SKU 卡片行：只有列表卡级字段，SKU 级列全空。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": "", "店内码": "", "货号": "",
        "状态": C.MULTISKU_STATUS, "视频时间点": "", "时刻": "10:00:00",
        "采集时间": CAPTURE_TIME, "证据前缀": "", "规格": "粉色牡丹一束（5朵花）",
        "月售": "10", "库存": "2", "售罄": "否", "活动品": "否",
        "美团闪购价": "14.5", "饿了么价": "14.5", "京东到家价": "14.5",
        "采购价": "5/袋", "价格原文": "14.5 | 14.5 | 14.5 | 5/袋",
    })
    return row


def sku_row(index=1, name="多规格牡丹", spec="粉色牡丹一束（5朵花）", sku="SKU-A",
            spu="SPU-9", barcode="6900000000009", channel_status=None):
    """sidecar 里的单个 SKU 行。channel_status 给了就改成该渠道的状态。"""
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": str(index), "商品名称": name, "条码": barcode,
        "店内码": "", "货号": "", "状态": "成功", "视频时间点": "",
        "时刻": "10:00:00", "采集时间": CAPTURE_TIME, "证据前缀": "",
        "规格": spec, "月售": "10", "库存": "2", "售罄": "否", "活动品": "否",
        "美团闪购价": "14.5", "饿了么价": "14.5", "京东到家价": "14.5",
        "采购价": "5/袋", "价格原文": "14.5 | 14.5 | 14.5 | 5/袋",
        "开放平台标识": "OP-9", "SPU": spu, "SKU": sku, "重量": "20克(g)",
        "采购价_详情": "5", "采购单位_详情": "单品",
        "美团闪购状态": "已下架", "美团闪购零售价": "14.5", "美团闪购库存": "1",
        "饿了么状态": "已下架", "京东到家状态": "未上线",
        "可售日期": "不限", "礼袋": "0个礼袋", "是否力荐": "否", "是否标品": "标品",
    })
    if channel_status is not None:
        row["美团闪购状态"] = channel_status
        row["美团闪购零售价"] = ""
        row["美团闪购库存"] = ""
    return row


class ObservationCase(unittest.TestCase):
    """搭一套 mock 采集产物（CSV + sidecar + manifest），全部落在临时目录。"""

    def setUp(self):
        self.observation_module, self.audit_original = _install_observation_stub()
        _ObservationStub.platform_count = 3
        _ObservationStub.cards = []
        _ObservationStub.list_exhausted = True
        _ObservationStub.seen_pages = None
        _ObservationStub.raise_error = None

    def tearDown(self):
        _remove_observation_stub(self.observation_module, self.audit_original)

    def write_detail_xml(self, directory, content=DETAIL_XML):
        path = os.path.join(directory, "detail_1_0.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def build_list_evidence(self, directory, *, cards, platform_count=3,
                             list_exhausted=True, category=None):
        page_path = os.path.join(directory, "list_page_0.xml")
        with open(page_path, "w", encoding="utf-8") as fh:
            fh.write(LIST_XML)
        evidence = {
            "schema_version": C.LIST_EVIDENCE_SCHEMA_VERSION,
            "category": category,
            "platform_count": platform_count,
            "cards": cards,
            "pages": [{"path": page_path, "sha256": C.sha256_file(page_path)}],
            "list_exhausted": list_exhausted,
        }
        evidence_path = os.path.join(directory, "list_evidence.json")
        C.atomic_write_json(evidence_path, evidence, overwrite=True)
        return evidence_path

    def attach_list_evidence(self, directory, rows, *, category="萌宠专区/猫粮"):
        """给观测终态产物挂一份逐屏列表证据（例外态一律要证）。"""
        cards = [{"name": row["商品名称"], "spec": row["规格"],
                  "barcode": row.get("条码", "")} for row in rows]
        evidence_path = self.build_list_evidence(
            directory, cards=cards, platform_count=len(rows), category=category)
        _ObservationStub.platform_count = len(rows)
        _ObservationStub.cards = cards
        _ObservationStub.list_exhausted = True
        return ({"list_evidence_path": evidence_path,
                 "list_evidence_sha256": C.sha256_file(evidence_path)},
                {"category": category})

    def build(self, directory, rows, *, state="success", observation_contract=None,
              scope_extra=None, capture_extra=None, progress_extra=None,
              listings=None, observations=None, artifacts_extra=None,
              expected_count=None):
        prefix = os.path.join(directory, "qnh_scan_20261021-100000")
        csv_path = prefix + ".csv"
        for row in rows:
            row["证据前缀"] = prefix
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        scope = {
            "list_title": "全部商品", "category": None,
            "expected_product_count": (expected_count if expected_count is not None
                                       else len(rows)),
            "requested_count": 0,
        }
        scope.update(scope_extra or {})
        capture = {"method": "app_ui", "started_at": CAPTURE_TIME,
                   "run_started_at": CAPTURE_TIME,
                   "finished_at": "2026-10-21T10:01:00", "collector_version": "test",
                   "pace": "ultra", "tall_screen": True, "verify_every": 1,
                   "checkpoint_every": 50, "record": False}
        capture.update(capture_extra or {})
        manifest = C.build_manifest(
            capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()), state=state,
            profile={"name": "test", "store_name": "测试店", "warehouse_id": "1",
                     "tenant_id": "2", "store_code": "qnh_test", "role": "competitor"},
            device_id="device", scope=scope, capture=capture,
            artifacts={"csv_path": csv_path, "evidence_prefix": prefix,
                       "video_path": None, "csv_sha256": C.sha256_file(csv_path)},
            rows=rows)
        if listings is not None or observations is not None:
            sidecar_path = prefix + ".skus.json"
            payload = {"schema_version": C.SKU_SIDECAR_SCHEMA_VERSION,
                       "listings": listings or {}}
            if observations is not None:
                payload["observations"] = observations
            with open(sidecar_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False))
            manifest["scope"]["sku_mode"] = C.SKU_SIDECAR_MODE
            manifest["artifacts"]["sku_path"] = sidecar_path
            manifest["artifacts"]["sku_sha256"] = C.sha256_file(sidecar_path)
            manifest["progress"]["listing_count"] = len(rows)
            manifest["progress"]["sku_row_count"] = sum(
                len(listings.get(str(int(row["序号"])), []))
                if row["状态"] == C.MULTISKU_STATUS else 1 for row in rows)
        if observation_contract is not None:
            manifest["capture"]["observation_contract"] = observation_contract
        for key, value in (progress_extra or {}).items():
            manifest["progress"][key] = value
        manifest["artifacts"].update(artifacts_extra or {})
        manifest_path = prefix + ".manifest.json"
        C.atomic_write_json(manifest_path, manifest, overwrite=True)
        return csv_path, manifest_path

    def unknown_observation(self, detail_path, *, sku="SKU-B", channel="美团闪购",
                            section_status="已下架", reason="详情页渠道表没有这一行"):
        return {"observation": C.OBSERVATION_ROW_ABSENT,
                "section_status": section_status, "reason": reason,
                "evidence_pages": [{"path": detail_path,
                                    "sha256": C.sha256_file(detail_path)}]}

    def observed_manifest(self, directory, *, detail_path, state=None,
                          observation_contract="v1", observations=None,
                          progress_extra=None, exceptions=None,
                          list_evidence=True, category="萌宠专区/猫粮"):
        """多 SKU 卡片 + 一个渠道行缺失的 SKU（观测凭据齐全）。"""
        rows = [multi_card_row(1)]
        children = [
            sku_row(1, spec="粉色牡丹一束（5朵花）", sku="SKU-A"),
            sku_row(1, spec="香槟色牡丹一束（一朵花）", sku="SKU-B",
                    channel_status=C.CHANNEL_UNKNOWN_STATUS),
        ]
        if observations is None:
            observations = {"1": {"SKU-B": {
                "美团闪购": self.unknown_observation(detail_path)}}}
        progress = {"detail_complete_count": 0,  # 多 SKU 父卡含未知子行 → 整卡不完整
                    "observation_complete": True, "business_complete": False,
                    "exceptions": exceptions if exceptions is not None else [
                        {"type": "channel_row_absent", "sku": "SKU-B",
                         "channel": "美团闪购", "spec": "香槟色牡丹一束（一朵花）"}]}
        progress.update(progress_extra or {})
        artifacts_extra = {}
        scope_extra = {}
        if list_evidence:
            # 例外态一律要逐屏列表证据：不短收（只有渠道行缺失）同样要证
            # 「页面已采全」。
            artifacts_extra, scope_extra = self.attach_list_evidence(
                directory, rows, category=category)
        return self.build(directory, rows,
                          state=state or C.OBSERVED_MANIFEST_STATE,
                          observation_contract=observation_contract,
                          listings={"1": children}, observations=observations,
                          progress_extra=progress,
                          artifacts_extra=artifacts_extra,
                          scope_extra=scope_extra)


class UnknownChannelObservationTests(ObservationCase):
    def test_unknown_without_credential_still_fails_in_success_manifest(self):
        """普通 success 里的「未知」依旧失败：声称成功就不该有未知。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(
                directory, detail_path=detail, state="success",
                observation_contract=None)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("详情字段不完整" in error for error in report.errors),
                            report.errors)

    def test_unknown_without_credential_fails_in_observed_manifest(self):
        """例外态也不能凭空记未知：没有凭据就还是失败。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(directory, detail_path=detail,
                                             observations={})
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("详情字段不完整" in error for error in report.errors),
                            report.errors)

    def test_observed_manifest_with_credential_passes_local_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(directory, detail_path=detail)
            report = C.validate_manifest(path, require_complete=False)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.observation_count, 1)

    def test_unknown_does_not_count_as_detail_complete(self):
        """未知就是未知：不能被伪成 detail_complete。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(directory, detail_path=detail)
            report = C.validate_manifest(path, require_complete=False)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.detail_complete_count,
                             report.manifest["progress"]["detail_complete_count"])

    def test_multisku_card_with_unknown_is_not_detail_complete(self):
        """多 SKU 父卡含未知子行 → 整张卡不算 detail_complete（门禁自己重算）。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(directory, detail_path=detail)
            report = C.validate_manifest(path, require_complete=False)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.detail_complete_count, 0)

    def test_manifest_claiming_complete_multisku_card_fails(self):
        """manifest 把含未知的多 SKU 卡算完整 → 门禁重算后对不上，失败。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(
                directory, detail_path=detail,
                progress_extra={"detail_complete_count": 1})
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("detail_complete_count" in error
                                for error in report.errors), report.errors)

    def test_observation_count_must_match(self):
        """progress.observation_count 与门禁重算的例外条数必须一致。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(
                directory, detail_path=detail,
                progress_extra={"observation_count": 7})
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("observation_count" in error for error in report.errors),
                            report.errors)

    def test_require_complete_true_still_rejects_observed_state(self):
        """例外态不是 success：严格门禁不许因为它放行。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(directory, detail_path=detail)
            report = C.validate_manifest(path, require_complete=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("不是 success" in error for error in report.errors),
                            report.errors)

    def test_tampered_evidence_page_fails(self):
        """detail XML 被改过：hash 对不上，凭据作废。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(directory, detail_path=detail)
            with open(detail, "w", encoding="utf-8") as fh:
                fh.write(DETAIL_XML.replace("粉色牡丹", "伪造牡丹"))
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("hash 与文件不一致" in error for error in report.errors),
                            report.errors)

    def test_forged_row_absent_is_rejected_by_reparse(self):
        """凭据说行缺失，但原始 XML 里那一行明明在：复解析打回。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory, DETAIL_XML_WITH_BOTH)
            _, path = self.observed_manifest(directory, detail_path=detail)
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("不是行缺失" in error for error in report.errors),
                            report.errors)

    def test_detail_xml_without_page_footer_fails(self):
        """没有页底「其他信息」：无法证明渠道表读到底，不能按行缺失放行。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(
                directory, DETAIL_XML.replace(
                    '  <node text="其他信息" bounds="[0,900][1080,960]" />\n', ""))
            _, path = self.observed_manifest(directory, detail_path=detail)
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("页底标记" in error for error in report.errors),
                            report.errors)

    def test_section_status_mismatch_fails(self):
        """凭据写的区块状态与原始 XML 的区块标题不符：作废。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(
                directory, detail_path=detail,
                observations={"1": {"SKU-B": {
                    "美团闪购": self.unknown_observation(
                        detail, section_status="已上架")}}})
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("区块状态" in error for error in report.errors),
                            report.errors)

    def test_unknown_with_price_is_not_an_exception(self):
        """状态是未知却带着价：自相矛盾，不能按观测例外放行。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            children = [
                sku_row(1, spec="粉色牡丹一束（5朵花）", sku="SKU-A"),
                sku_row(1, spec="香槟色牡丹一束（一朵花）", sku="SKU-B",
                        channel_status=C.CHANNEL_UNKNOWN_STATUS),
            ]
            children[1]["美团闪购零售价"] = "14.5"
            observations = {"1": {"SKU-B": {
                "美团闪购": self.unknown_observation(detail)}}}
            rows = [multi_card_row(1)]
            _, path = self.build(
                directory, rows, state=C.OBSERVED_MANIFEST_STATE,
                observation_contract="v1", listings={"1": children},
                observations=observations,
                progress_extra={"detail_complete_count": 0,
                                "observation_complete": True,
                                "business_complete": False,
                                "exceptions": [{"type": "channel_row_absent",
                                                "sku": "SKU-B",
                                                "channel": "美团闪购",
                                                "spec": "香槟色牡丹一束（一朵花）"}]})
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("详情字段不完整" in error for error in report.errors),
                            report.errors)

    def test_observed_state_requires_v1_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(directory, detail_path=detail,
                                             observation_contract=None)
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("observation_contract" in error for error in report.errors),
                            report.errors)

    def test_observed_state_requires_observation_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(
                directory, detail_path=detail,
                progress_extra={"observation_complete": False})
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("observation_complete" in error for error in report.errors),
                            report.errors)

    def test_observed_state_requires_business_complete_false(self):
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(
                directory, detail_path=detail,
                progress_extra={"business_complete": True})
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("business_complete" in error for error in report.errors),
                            report.errors)

    def test_observed_unknown_must_be_declared_in_exceptions(self):
        """观测到的例外没登记进 progress.exceptions：账对不上，失败。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(directory, detail_path=detail,
                                             exceptions=[])
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("progress.exceptions" in error for error in report.errors),
                            report.errors)

    def test_declared_exception_without_credential_fails(self):
        """声明了例外却没有观测凭据：失败（不许凭空声明）。"""
        with tempfile.TemporaryDirectory() as directory:
            detail = self.write_detail_xml(directory)
            _, path = self.observed_manifest(
                directory, detail_path=detail,
                exceptions=[{"type": "channel_row_absent", "sku": "SKU-Z",
                             "channel": "饿了么", "spec": "别的规格"}])
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("没有观测凭据" in error for error in report.errors),
                            report.errors)

    def test_unknown_is_not_faked_as_zero(self):
        """「未知」不许被伪成 0：价、库存、上架状态都留空。"""
        row = card_row(1)
        row["美团闪购状态"] = C.CHANNEL_UNKNOWN_STATUS
        row["美团闪购零售价"] = ""
        row["美团闪购库存"] = ""
        fields = C.fact_fields_from_csv(row, "meituan", "美团闪购价", "/tmp/x.csv")
        self.assertIsNone(fields["listing_status"])
        self.assertIsNone(fields["stock_quantity"])
        self.assertIsNone(fields["front_display_price_amount"])
        self.assertEqual(fields["sale_status"], "unknown")


class CountMismatchObservationTests(ObservationCase):
    """平台标题数 ≠ 可见卡片数：只有逐屏列表证据能放行。"""

    def observed_capture(self, directory, *, cards, platform_count=3,
                         list_exhausted=True, category="萌宠专区/猫粮", rows=None):
        rows = rows or [card_row(1, "猫粮A", "6900000000001"),
                        card_row(2, "猫粮B", "6900000000002")]
        evidence_path = self.build_list_evidence(
            directory, cards=cards, platform_count=platform_count,
            list_exhausted=list_exhausted, category=category)
        _ObservationStub.platform_count = platform_count
        _ObservationStub.cards = cards
        _ObservationStub.list_exhausted = list_exhausted
        return self.build(
            directory, rows, state=C.OBSERVED_MANIFEST_STATE,
            observation_contract="v1", expected_count=platform_count,
            scope_extra={"category": category},
            progress_extra={
                "observation_complete": True, "business_complete": False,
                "exceptions": [{"type": "platform_count_mismatch",
                                "platform_count": platform_count,
                                "visible_card_count": len(rows)}]},
            artifacts_extra={"list_evidence_path": evidence_path,
                             "list_evidence_sha256": C.sha256_file(evidence_path)})

    def test_count_mismatch_with_valid_evidence_passes(self):
        cards = [{"name": "猫粮A", "barcode": "6900000000001", "spec": "100g"},
                 {"name": "猫粮B", "barcode": "6900000000002", "spec": "100g"}]
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.observed_capture(directory, cards=cards)
            report = C.validate_manifest(path, require_complete=False)
            self.assertTrue(report.ok, report.errors)
            self.assertIsNotNone(_ObservationStub.seen_pages)

    def test_count_mismatch_without_evidence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [card_row(1, "猫粮A", "6900000000001"),
                    card_row(2, "猫粮B", "6900000000002")]
            _, path = self.build(
                directory, rows, state=C.OBSERVED_MANIFEST_STATE,
                observation_contract="v1", expected_count=3,
                progress_extra={"observation_complete": True,
                                "business_complete": False,
                                "exceptions": [{"type": "platform_count_mismatch",
                                                "platform_count": 3,
                                                "visible_card_count": 2}]})
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("list_evidence_path" in error for error in report.errors),
                            report.errors)

    def test_tampered_list_evidence_fails(self):
        cards = [{"name": "猫粮A", "barcode": "6900000000001", "spec": "100g"},
                 {"name": "猫粮B", "barcode": "6900000000002", "spec": "100g"}]
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.observed_capture(directory, cards=cards)
            manifest = C.read_json(path)
            evidence_path = manifest["artifacts"]["list_evidence_path"]
            evidence = C.read_json(evidence_path)
            evidence["platform_count"] = 99
            C.atomic_write_json(evidence_path, evidence, overwrite=True)
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("SHA-256 与 manifest 不一致" in error
                                for error in report.errors), report.errors)

    def test_tampered_list_page_fails(self):
        cards = [{"name": "猫粮A", "barcode": "6900000000001", "spec": "100g"},
                 {"name": "猫粮B", "barcode": "6900000000002", "spec": "100g"}]
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.observed_capture(directory, cards=cards)
            manifest = C.read_json(path)
            evidence_path = manifest["artifacts"]["list_evidence_path"]
            evidence = C.read_json(evidence_path)
            page_path = evidence["pages"][0]["path"]
            with open(page_path, "w", encoding="utf-8") as fh:
                fh.write(LIST_XML.replace("猫粮A", "伪造猫粮A"))
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("hash 与文件不一致" in error for error in report.errors),
                            report.errors)

    def test_evidence_cards_must_match_reparse(self):
        """证据自己写的 cards 与复解析结果不一致：自己说的不算数。"""
        cards = [{"name": "猫粮A", "barcode": "6900000000001", "spec": "100g"},
                 {"name": "猫粮B", "barcode": "6900000000002", "spec": "100g"}]
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.observed_capture(directory, cards=cards)
            manifest = C.read_json(path)
            evidence_path = manifest["artifacts"]["list_evidence_path"]
            evidence = C.read_json(evidence_path)
            evidence["cards"] = [dict(cards[0]), {"name": "猫粮C", "barcode": "",
                                                  "spec": "100g"}]
            C.atomic_write_json(evidence_path, evidence, overwrite=True)
            manifest["artifacts"]["list_evidence_sha256"] = C.sha256_file(evidence_path)
            C.atomic_write_json(path, manifest, overwrite=True)
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("与复解析结果不一致" in error for error in report.errors),
                            report.errors)

    def test_evidence_cards_must_match_captured_rows(self):
        """采集行必须正好是列表卡片：身份多重集相等（不是 set 相等）。"""
        cards = [{"name": "猫粮A", "barcode": "6900000000001", "spec": "100g"},
                 {"name": "猫粮B", "barcode": "6900000000002", "spec": "100g"}]
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.observed_capture(
                directory, cards=cards,
                rows=[card_row(1, "猫粮A", "6900000000001"),
                      card_row(2, "猫粮C", "6900000000003")])
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("采集行不匹配" in error for error in report.errors),
                            report.errors)

    def test_not_exhausted_list_fails(self):
        cards = [{"name": "猫粮A", "barcode": "6900000000001", "spec": "100g"},
                 {"name": "猫粮B", "barcode": "6900000000002", "spec": "100g"}]
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.observed_capture(directory, cards=cards,
                                            list_exhausted=False)
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("list_exhausted" in error for error in report.errors),
                            report.errors)

    def test_platform_count_must_match_scope(self):
        cards = [{"name": "猫粮A", "barcode": "6900000000001", "spec": "100g"},
                 {"name": "猫粮B", "barcode": "6900000000002", "spec": "100g"}]
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.observed_capture(directory, cards=cards,
                                            platform_count=3)
            manifest = C.read_json(path)
            manifest["scope"]["expected_product_count"] = 5
            C.atomic_write_json(path, manifest, overwrite=True)
            report = C.validate_manifest(path, require_complete=False)
            self.assertFalse(report.ok)
            self.assertTrue(any("expected_product_count" in error
                                for error in report.errors), report.errors)


class LoaderObservationContractTests(ObservationCase):
    """例外态产物现在可以入库（未知渠道格记 unknown，快照状态记 partial）。

    契约仍然把住两头：没有 v1 契约或没有逐条例外凭据的产物，进不来。
    """

    def test_observed_manifest_without_exceptions_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [card_row(1)]
            _, path = self.build(
                directory, rows, state=C.OBSERVED_MANIFEST_STATE,
                observation_contract="v1",
                progress_extra={"observation_complete": True,
                                "business_complete": False,
                                "exceptions": []})
            with self.assertRaises(loader.LoadError) as ctx:
                loader.load_capture(manifest_path=path)
            self.assertIn("exceptions", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
