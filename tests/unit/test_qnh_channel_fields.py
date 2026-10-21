"""Tests for the 牵牛花 channel field mapping (渠道列名映射与入库口径).

真机口径（2026-10-21 ARTEMIS 探索，trace 56532808-9364-4579-9db7-d58f14b994d8）：
香飘飘原味奶茶80g/杯（条码 6938888888837，SPU 2112518307509739611 /
SKU 2112518307509891160）列表页三卡价 6.50 且已售罄；详情页美团闪购/饿了么
标题为「已上架」、零售价 6.5、库存「售罄」，采购「无数据」。
"""

import unittest
from datetime import datetime
from decimal import Decimal
from unittest import mock

import qnh_capture_common as C
import qnh_validate_db as V

CSV_PATH = "/tmp/qnh_scan_20261021-100000.csv"


def row(**overrides):
    """一行 40 列 CSV（渠道详情列按上面真机口径填）。"""
    base = {column: "" for column in C.FULL_COLUMNS}
    base.update({
        "序号": "1", "商品名称": "香飘飘原味奶茶80g/杯", "条码": "6938888888837",
        "店内码": "A-1", "货号": "H-1", "状态": "成功", "时刻": "10:00:00",
        "采集时间": "2026-10-21T10:00:00", "证据前缀": "/tmp/qnh_scan_20261021-100000",
        "规格": "80g/杯", "月售": "10", "库存": "0", "售罄": "是", "活动品": "否",
        "美团闪购价": "6.50", "饿了么价": "6.50", "京东到家价": "6.50",
        "采购价": "无数据", "价格原文": "6.50 | 6.50 | 6.50 | 无数据",
        "开放平台标识": "SPU-1", "SPU": "2112518307509739611",
        "SKU": "2112518307509891160", "重量": "80g",
        "采购价_详情": "", "采购单位_详情": "",
        "美团闪购状态": "已上架", "美团闪购零售价": "6.5", "美团闪购库存": "售罄",
        "饿了么状态": "已上架", "饿了么零售价": "6.5", "饿了么库存": "售罄",
        "京东到家状态": "未上线", "京东到家零售价": "", "京东到家库存": "",
        "可售日期": "不限", "礼袋": "0个礼袋", "是否力荐": "否", "是否标品": "标品",
    })
    base.update(overrides)
    return base


def facts(source=None, channel="meituan", price_column="美团闪购价"):
    return C.fact_fields_from_csv(source if source is not None else row(),
                                   channel, price_column, CSV_PATH)


def snapshot(hour=None):
    return {"batch_id": "b", "store_snapshot_id": "s",
            "snapshot_hour": hour or datetime(2026, 10, 21, 9, 0, 0),
            "store_code": "qnh_x", "store_name": "店", "store_role": "competitor"}


class ChannelMappingTests(unittest.TestCase):
    def test_english_channel_code_reads_chinese_detail_columns(self):
        """英文渠道码必须读到中文详情列（回归前读的是 "meituan零售价"）。"""
        found = facts()
        self.assertEqual(found["listing_status"], "on_shelf")
        self.assertEqual(found["front_display_price_amount"], Decimal("6.5"))
        self.assertEqual(found["stock_quantity"], Decimal("0"))
        self.assertEqual(found["stock_text"], "已售罄")
        self.assertEqual(found["sale_status"], "out_of_stock")

    def test_chinese_channel_name_stays_supported(self):
        found = facts(channel="美团闪购")
        self.assertEqual(found["listing_status"], "on_shelf")
        self.assertEqual(found["front_display_price_amount"], Decimal("6.5"))

    def test_unknown_channel_is_rejected(self):
        with self.assertRaises(C.CaptureContractError) as ctx:
            facts(channel="douyin")
        self.assertIn("未知渠道", str(ctx.exception))

    def test_mismatched_price_column_is_rejected(self):
        with self.assertRaises(C.CaptureContractError) as ctx:
            facts(channel="eleme", price_column="美团闪购价")
        self.assertIn("卡片价格列", str(ctx.exception))

    def test_carry_row_is_rejected(self):
        """携带行的渠道详情是历史合成值，不得当成当日真值入库。"""
        with self.assertRaises(C.CaptureContractError) as ctx:
            facts(row(状态="携带"))
        self.assertIn("重新实访详情页", str(ctx.exception))

    def test_on_shelf_channel_falls_back_to_card_price_and_list_stock(self):
        source = row(**{"美团闪购零售价": "", "美团闪购库存": "",
                        "美团闪购价": "5.50", "库存": "5", "售罄": "否"})
        found = facts(source)
        self.assertEqual(found["front_display_price_amount"], Decimal("5.50"))
        self.assertEqual(found["stock_quantity"], Decimal("5"))
        self.assertEqual(found["stock_text"], "5")
        self.assertEqual(found["sale_status"], "available")
        self.assertEqual(found["listing_status"], "on_shelf")

    def test_not_online_channel_does_not_borrow_list_values(self):
        """未上线渠道不拿列表卡片价/列表库存冒充渠道详情。"""
        found = facts(channel="jddj", price_column="京东到家价")
        self.assertEqual(found["listing_status"], "not_online")
        self.assertIsNone(found["front_display_price_amount"])
        self.assertIsNone(found["stock_quantity"])

    def test_off_shelf_channel_keeps_its_own_detail_values(self):
        source = row(**{"京东到家状态": "已下架", "京东到家零售价": "7.20",
                        "京东到家库存": "3", "京东到家价": "6.50", "库存": "9"})
        found = facts(source, channel="jddj", price_column="京东到家价")
        self.assertEqual(found["listing_status"], "off_shelf")
        self.assertEqual(found["front_display_price_amount"], Decimal("7.20"))
        self.assertEqual(found["stock_quantity"], Decimal("3"))


class ChannelStockSemanticsTests(unittest.TestCase):
    """渠道有自己的库存时完全按渠道库存判售罄，不被列表售罄盖掉。"""

    def test_channel_stock_wins_over_list_sold_out(self):
        source = row(**{"美团闪购库存": "5", "售罄": "是", "库存": "0"})
        found = facts(source)
        self.assertEqual(found["stock_quantity"], Decimal("5"))
        self.assertEqual(found["stock_text"], "5")
        self.assertEqual(found["sale_status"], "available")
        self.assertEqual(found["listing_status"], "on_shelf")

    def test_channel_zero_stock_is_sold_out(self):
        source = row(**{"美团闪购库存": "0", "售罄": "否"})
        found = facts(source)
        self.assertEqual(found["stock_quantity"], Decimal("0"))
        self.assertEqual(found["stock_text"], "已售罄")
        self.assertEqual(found["sale_status"], "out_of_stock")

    def test_detail_sold_out_stock_is_zero(self):
        source = row(**{"美团闪购库存": "已售罄", "售罄": "否"})
        found = facts(source)
        self.assertEqual(found["stock_quantity"], Decimal("0"))
        self.assertEqual(found["stock_text"], "已售罄")
        self.assertEqual(found["sale_status"], "out_of_stock")
        self.assertEqual(found["listing_status"], "on_shelf")

    def test_on_shelf_without_channel_stock_falls_back_to_list_sold_out(self):
        source = row(**{"美团闪购库存": "", "售罄": "是", "库存": ""})
        found = facts(source)
        self.assertEqual(found["stock_quantity"], Decimal("0"))
        self.assertEqual(found["stock_text"], "已售罄")
        self.assertEqual(found["sale_status"], "out_of_stock")

    def test_not_online_without_channel_stock_is_unknown(self):
        """未上线且没有渠道库存：不按列表赋可售。"""
        source = row(**{"售罄": "否", "库存": "5"})
        found = facts(source, channel="jddj", price_column="京东到家价")
        self.assertEqual(found["listing_status"], "not_online")
        self.assertEqual(found["sale_status"], "unknown")
        self.assertIsNone(found["stock_quantity"])
        self.assertIsNone(found["stock_text"])

    def test_listing_status_and_sale_status_stay_independent(self):
        source = row(**{"京东到家状态": "未上线", "售罄": "否", "库存": "5",
                        "美团闪购库存": "4", "饿了么库存": "4"})
        not_online = facts(source, channel="jddj", price_column="京东到家价")
        on_shelf = facts(source, channel="eleme", price_column="饿了么价")
        self.assertEqual(not_online["listing_status"], "not_online")
        self.assertEqual(not_online["sale_status"], "unknown")
        self.assertEqual(on_shelf["listing_status"], "on_shelf")
        self.assertEqual(on_shelf["sale_status"], "available")


class ValidatorExpectationTests(unittest.TestCase):
    def test_channel_roster_matches_loader(self):
        self.assertEqual(set(V.CHANNEL_VIEW), {code for code, _ in C.PRICE_CHANNELS})
        for code, price_column in C.PRICE_CHANNELS:
            self.assertEqual(V.CHANNEL_VIEW[code][1], price_column)

    def test_validator_expectation_matches_loader_semantics(self):
        """校验期望与加载器转换必须一致（否则事务内校验会误报失败）。"""
        variants = (
            row(),
            row(**{"美团闪购零售价": "", "美团闪购库存": "", "库存": "4", "售罄": "否"}),
            row(**{"美团闪购库存": "5", "售罄": "是", "库存": "0"}),
            row(**{"京东到家状态": "已下架", "京东到家库存": "3"}),
        )
        for channel, price_column in C.PRICE_CHANNELS:
            for source in variants:
                wanted = V._expected_fields(source, channel, price_column, CSV_PATH)
                loaded = C.fact_fields_from_csv(source, channel, price_column, CSV_PATH)
                for field, value in wanted.items():
                    self.assertEqual(loaded.get(field), value,
                                     f"{channel} {field}")

    def test_validator_expectation_survives_loader_regression(self):
        """加载器的渠道映射回归时，校验期望不能跟着错（否则抓不到回归）。"""
        original = C.fact_fields_from_csv

        def broken(source, channel, price_column, csv_path):
            """历史行为：英文渠道码拼中文列名，渠道详情列全部读空。"""
            loaded = dict(original(source, channel, price_column, csv_path))
            loaded.update({"listing_status": None, "front_display_price_amount": None,
                           "stock_quantity": None, "stock_text": None})
            return loaded

        with mock.patch.object(C, "fact_fields_from_csv", broken):
            wanted = V._expected_fields(row(), "meituan", "美团闪购价", CSV_PATH)
        self.assertEqual(wanted["listing_status"], "on_shelf")
        self.assertEqual(wanted["front_display_price_amount"], Decimal("6.5"))
        self.assertEqual(wanted["stock_quantity"], Decimal("0"))

    def test_validator_detects_list_stock_contradiction(self):
        """列表售罄 + 渠道正库存：校验期望必须按渠道库存给可售。"""
        source = row(**{"美团闪购库存": "5", "售罄": "是", "库存": "0"})
        wanted = V._expected_fields(source, "meituan", "美团闪购价", CSV_PATH)
        self.assertEqual(wanted["sale_status"], "available")
        self.assertEqual(wanted["stock_quantity"], Decimal("5"))
        self.assertEqual(wanted["stock_text"], "5")

    def test_row_snapshot_hour_follows_row_collected_at(self):
        rows = [row(采集时间="2026-10-21T10:37:00"),
                row(序号="2", 采集时间="2026-10-21T11:05:00")]
        expected = V._expected_by_key(rows, CSV_PATH, snapshot())
        hours = {item["snapshot_hour"] for item in expected.values()}
        self.assertEqual(hours, {datetime(2026, 10, 21, 10, 0),
                                 datetime(2026, 10, 21, 11, 0)})

    def test_row_without_collected_at_falls_back_to_snapshot_hour(self):
        """采集时间缺失（且证据前缀无法还原）→ 用快照批次小时兜底。"""
        rows = [row(采集时间="", 证据前缀="/tmp/qnh_evidence_x", 时刻="")]
        expected = V._expected_by_key(rows, CSV_PATH, snapshot())
        self.assertEqual({item["snapshot_hour"] for item in expected.values()},
                         {datetime(2026, 10, 21, 9, 0)})

    def test_carry_rows_are_excluded_from_expectations(self):
        expected = V._expected_by_key([row(状态="携带")], CSV_PATH, snapshot())
        self.assertEqual(expected, {})


if __name__ == "__main__":
    unittest.main()
