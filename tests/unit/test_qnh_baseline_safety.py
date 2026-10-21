"""Safety tests for the daily carry baseline (qnh_daily_baseline.py).

覆盖三条底线：
  * 渠道上架状态只认 DB 里真实的 listing_status，NULL/未知不伪造；
  * payload 带店内码/货号（CSV 店内码/货号列），40 列契约不变；
  * SKU 跨名称/SPU 冲突整组不携带。

所有 DB 访问都是假的：FakeCursor 按 SQL 关键词返回预置行，从不连库。
"""

import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import qnh_capture_common as C
import qnh_daily_baseline as B


def make_fact(**overrides):
    """一条 fact 行（DB 行形状）。默认 listing_status 为 NULL——真实库里
    fact_fields_from_csv 的英文渠道键 bug 让渠道详情从未入库。"""
    fact = {
        "listing_key": "k1",
        "channel_code": "meituan",
        "product_name": "香飘飘奶茶80g/杯",
        "spec_name": "80g/杯",
        "upc": "6901234567890",
        "store_inner_code": "NC-1",
        "article_no": "HW-1",
        "open_platform_id": "2044659046392676353",
        "source_spu_id": "SPU-1",
        "source_sku_id": "SKU-1",
        "weight_text": "80克(g)",
        "front_display_price_amount": Decimal("6.5"),
        "purchase_price_amount": None,
        "purchase_price_unit": None,
        "purchase_price_text": "无数据",
        "month_sales_text": "月售10",
        "month_sales_qty": 10,
        "sale_status": "out_of_stock",
        "stock_text": "已售罄",
        "stock_quantity": Decimal(0),
        "is_promo_item": 0,
        "listing_status": None,
        "selling_points": None,
        "sellable_date_text": "不限",
        "gift_bag_text": "0个礼袋",
        "is_recommended": 0,
        "is_standard": 1,
        "other_text": None,
    }
    fact.update(overrides)
    return fact


def channel_rows(product, listing_key=None):
    """同一商品的三条渠道 fact 行。"""
    listing_key = listing_key or product["listing_key"]
    rows = []
    for channel in ("meituan", "eleme", "jddj"):
        row = dict(product)
        row["channel_code"] = channel
        row["listing_key"] = listing_key
        rows.append(row)
    return rows


class FakeCursor:
    """按 SQL 关键词返回预置行；不碰数据库。"""

    def __init__(self, tables):
        self._tables = tables
        self._rows = []

    def execute(self, sql, args=None):
        text = " ".join(str(sql).split())
        if "FROM store_snapshot" in text:
            self._rows = self._tables["snapshot"]
        elif "channel_code='meituan'" in text:
            self._rows = self._tables["meituan"]
        elif "IN ('eleme','jddj')" in text:
            self._rows = self._tables["others"]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConn:
    def __init__(self, tables):
        self._tables = tables
        self.closed = False

    def cursor(self):
        return FakeCursor(self._tables)

    def close(self):
        self.closed = True


def fake_conn(products):
    """products: [(meituan_fact, eleme_fact, jddj_fact), ...] → FakeConn。"""
    meituan_rows = []
    others = []
    for meituan, eleme, jddj in products:
        meituan_rows.append(meituan)
        others.extend((eleme, jddj))
    return FakeConn({
        "snapshot": [{"store_snapshot_id": "snap-1"}],
        "meituan": meituan_rows,
        "others": others,
    })


class ListingStatusTruthTests(unittest.TestCase):
    """上架状态只认 DB 真实 listing_status。"""

    def test_null_listing_status_is_not_fabricated_as_on_shelf(self):
        for sale in ("out_of_stock", "available", "unknown"):
            status, _, _ = B._channel_columns(
                make_fact(listing_status=None, sale_status=sale))
            self.assertEqual(status, "", f"sale_status={sale} 不该推出上架状态")

    def test_real_listing_status_maps_to_chinese_text(self):
        for value, text in (("on_shelf", "已上架"), ("off_shelf", "已下架"),
                            ("not_online", "未上线")):
            status, price, _ = B._channel_columns(
                make_fact(listing_status=value, sale_status="available",
                          stock_quantity=Decimal(3)))
            self.assertEqual(status, text)
            self.assertEqual(price, "6.5")

    def test_unknown_enum_value_is_blank(self):
        status, _, _ = B._channel_columns(make_fact(listing_status="maybe"))
        self.assertEqual(status, "")

    def test_row_keeps_channel_status_empty_and_40_columns(self):
        row = B.row_from_fact(*channel_rows(make_fact()))
        self.assertEqual(row["美团闪购状态"], "")
        self.assertEqual(row["饿了么状态"], "")
        self.assertEqual(row["京东到家状态"], "")
        self.assertEqual(set(row), set(C.FULL_COLUMNS))
        self.assertEqual(len(C.FULL_COLUMNS), 40)


class BaselineSelectionTests(unittest.TestCase):
    """fetch_baseline 的入选/剔除规则。"""

    def test_row_with_unknown_channel_status_is_excluded(self):
        meituan, eleme, jddj = channel_rows(make_fact())
        conn = fake_conn([(meituan, eleme, jddj)])
        snapshot_id, products, _ = B.fetch_baseline(conn, "S1")
        self.assertEqual(snapshot_id, "snap-1")
        self.assertEqual(products, {})

    def test_old_null_channel_fact_never_enters_baseline(self):
        """美团有真实状态、饿了么仍是旧 NULL：该商品整行不携带。"""
        meituan, eleme, jddj = channel_rows(make_fact(listing_status="on_shelf"))
        eleme["listing_status"] = None
        conn = fake_conn([(meituan, eleme, jddj)])
        _, products, _ = B.fetch_baseline(conn, "S1")
        self.assertEqual(products, {})

    def test_real_status_row_is_carried_with_payload_fields(self):
        meituan, eleme, jddj = channel_rows(make_fact(listing_status="on_shelf"))
        conn = fake_conn([(meituan, eleme, jddj)])
        _, products, _ = B.fetch_baseline(conn, "S1")
        entry = products["barcode:6901234567890"]
        self.assertEqual(entry["name"], "香飘飘奶茶80g/杯")
        self.assertEqual(entry["barcode"], "6901234567890")
        self.assertEqual(entry["store_inner_code"], "NC-1")
        self.assertEqual(entry["article_no"], "HW-1")
        self.assertEqual(len(entry["detail"]), len(C.DETAIL_COLUMNS))
        self.assertIn("fingerprint", entry)

    def test_sku_conflict_group_is_excluded_entirely(self):
        """同一非空 SKU 映射到不同商品名 → 两行都不携带。"""
        first = make_fact(listing_key="k1", upc="6900000000001", source_sku_id="SKU-X",
                          product_name="同 SKU 商品甲")
        second = make_fact(listing_key="k2", upc="6900000000002", source_sku_id="SKU-X",
                           product_name="同 SKU 商品乙")
        third = make_fact(listing_key="k3", upc="6900000000003", source_sku_id="SKU-Y",
                           product_name="正常商品", listing_status="on_shelf")
        conn = fake_conn([
            channel_rows(first),
            channel_rows(second),
            channel_rows(third),
        ])
        _, products, _ = B.fetch_baseline(conn, "S1")
        self.assertEqual(list(products), ["barcode:6900000000003"])

    def test_sku_conflict_is_detected_across_spu_difference(self):
        first = make_fact(listing_key="k1", upc="6900000000001", source_sku_id="SKU-X",
                          source_spu_id="SPU-1", listing_status="on_shelf")
        second = make_fact(listing_key="k2", upc="6900000000002", source_sku_id="SKU-X",
                           source_spu_id="SPU-2", listing_status="on_shelf")
        conn = fake_conn([channel_rows(first), channel_rows(second)])
        _, products, _ = B.fetch_baseline(conn, "S1")
        self.assertEqual(products, {})

    def test_same_sku_same_name_different_spec_is_allowed(self):
        """同 SKU 同名不同规格合法（口径同采集侧门禁 validate_product_identities）。"""
        first = make_fact(listing_key="k1", upc="6900000000001", source_sku_id="SKU-X",
                          spec_name="80g/杯", listing_status="on_shelf")
        second = make_fact(listing_key="k2", upc="6900000000002", source_sku_id="SKU-X",
                           spec_name="160g/杯", listing_status="on_shelf")
        conn = fake_conn([channel_rows(first), channel_rows(second)])
        _, products, _ = B.fetch_baseline(conn, "S1")
        self.assertEqual(sorted(products),
                         ["barcode:6900000000001", "barcode:6900000000002"])

    def test_duplicate_listing_identity_group_is_excluded(self):
        """同名同码的两行：身份重叠，整组不携带。"""
        first = make_fact(listing_key="k1", listing_status="on_shelf")
        second = make_fact(listing_key="k2", listing_status="on_shelf")
        conn = fake_conn([channel_rows(first), channel_rows(second)])
        _, products, _ = B.fetch_baseline(conn, "S1")
        self.assertEqual(products, {})

    def test_lossy_row_is_excluded_by_roundtrip_guard(self):
        """DB 存不下原文精度（6 位小数被截断）→ 不携带。"""
        fact = make_fact(listing_status="on_shelf", sale_status="available",
                         stock_quantity=Decimal(3), stock_text="3",
                         purchase_price_text="1.234567/袋",
                         purchase_price_amount=Decimal("1.2346"),
                         purchase_price_unit="袋")
        conn = fake_conn([channel_rows(fact)])
        _, products, _ = B.fetch_baseline(conn, "S1")
        self.assertEqual(products, {})

    def test_carried_row_roundtrips_against_db_facts(self):
        fact = make_fact(listing_status="on_shelf", sale_status="available",
                         stock_quantity=Decimal(3), stock_text="3")
        rows = channel_rows(fact)
        conn = fake_conn([rows])
        _, products, rebuilt = B.fetch_baseline(conn, "S1")
        self.assertTrue(products)
        for row, meituan, eleme, jddj in rebuilt:
            self.assertEqual(B._roundtrip_errors(row, meituan, eleme, jddj), [])

    def test_sold_out_row_keeps_db_stock_and_roundtrips(self):
        """售罄渠道按 DB 存的数值还原库存（不由 sale_status 空造）。"""
        fact = make_fact(listing_status="on_shelf", sale_status="out_of_stock",
                         stock_quantity=Decimal(0), stock_text="已售罄")
        conn = fake_conn([channel_rows(fact)])
        _, products, rebuilt = B.fetch_baseline(conn, "S1")
        self.assertTrue(products)
        row, meituan, eleme, jddj = rebuilt[0]
        self.assertEqual(row["美团闪购库存"], "0")
        self.assertEqual(row["售罄"], "是")
        self.assertEqual(B._roundtrip_errors(row, meituan, eleme, jddj), [])

    def test_price_is_kept_even_when_sale_status_is_unknown(self):
        """sale_status 未知（渠道未上线且无渠道库存）也不能清空真实价格。"""
        fact = make_fact(listing_status="off_shelf", sale_status="unknown",
                         stock_quantity=None, stock_text=None,
                         front_display_price_amount=Decimal("6.5"))
        conn = fake_conn([channel_rows(fact)])
        _, products, rebuilt = B.fetch_baseline(conn, "S1")
        self.assertTrue(products)
        row, meituan, eleme, jddj = rebuilt[0]
        self.assertEqual(row["美团闪购零售价"], "6.5")
        self.assertEqual(row["美团闪购库存"], "")
        self.assertEqual(B._roundtrip_errors(row, meituan, eleme, jddj), [])


class ExportContractTests(unittest.TestCase):
    """导出文件契约（schema_version 2 + 来源/携带说明 + payload 字段）。"""

    def _export(self, products):
        conn = fake_conn(products)
        with tempfile.TemporaryDirectory() as directory:
            argv = ["qnh_daily_baseline.py", "--out-dir", directory, "--check-sample", "0"]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(B, "_db_open", return_value=conn), \
                    mock.patch.object(C, "load_profile", return_value={"store_code": "S1"}):
                B.main()
            with open(os.path.join(directory, "carry_baseline.json"), encoding="utf-8") as fh:
                return json.load(fh)

    def test_schema_version_two_carries_source_and_carry_state(self):
        fact = make_fact(listing_status="on_shelf")
        data = self._export([channel_rows(fact)])
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["baseline_snapshot_id"], "snap-1")
        self.assertIn("generated_at", data)
        self.assertEqual(data["generation_source"],
                         "retail_price_snapshot.fact_store_product_snapshot")
        self.assertIs(data["carry_enabled"], False)
        self.assertTrue(data["carry_note"])
        entry = data["products"]["barcode:6901234567890"]
        self.assertEqual(entry["store_inner_code"], "NC-1")
        self.assertEqual(entry["article_no"], "HW-1")

    def test_unknown_status_products_are_not_exported(self):
        data = self._export([channel_rows(make_fact())])
        self.assertEqual(data["products"], {})


if __name__ == "__main__":
    unittest.main()
