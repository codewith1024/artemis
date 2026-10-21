"""安全测试：map_source_sku_standard 的 listing 粒度映射。

覆盖三条底线：
  * 同 SKU 同名多规格 = 多个 listing_key，map 每个 listing 都留一行——不会被
    uk_map_source_sku 唯一约束下的 INSERT IGNORE 静默吞掉；
  * 每个 listing 的标准身份（standard_spu_id/standard_sku_id）跟着自己的条码走；
  * map 行真的缺了时整批回滚，不提交半映射快照。

DB 全是假的：FakeDB 按 SQL 关键词分派，忠实模拟 uk_map_listing /
uk_map_source_sku 与 INSERT IGNORE 语义（冲突跳过、NULL 不冲突），从不连库。
"""

import csv
import os
import re
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from unittest import mock

import qnh_capture_common as C
import qnh_load_db as L

TABLES = ("store_snapshot", "dim_store", "fact_store_product_snapshot",
          "fact_store_category_snapshot", "map_source_sku_standard")

INSERT_RE = re.compile(
    r"INSERT(?:\s+IGNORE)?\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\((.*)\)$", re.S)

# goods_master_goods 主档：条码 → (标准 SPU, 标准 SKU)
MASTER = {
    "6901234567890": ("SPU-1", "SKU-1"),
    "6901234567891": ("SPU-1", "SKU-2"),
}


def _literal(text):
    value = text.strip().strip("'\"")
    try:
        return Decimal(value)
    except InvalidOperation:
        return value


class FakeDB:
    """内存假库：committed + pending 两层，rollback 丢弃 pending、commit 落库。"""

    def __init__(self, master=None):
        self.committed = {table: [] for table in TABLES}
        self.pending = {table: [] for table in TABLES}
        self.master = dict(master or {})
        self.inserts = []        # 捕获的 INSERT：{"table", "row", "sql"}
        self.skipped = []        # 被唯一约束（或故障注入）跳过的 map 行
        self.drop_map_keys = set()  # 故障注入：强制吞掉这些 (channel, listing_key)
        self.next_id = 1

    def commit(self):
        for table in TABLES:
            self.committed[table].extend(self.pending[table])
            self.pending[table] = []

    def rollback(self):
        for table in TABLES:
            self.pending[table] = []

    def rows(self, table):
        return self.committed[table] + self.pending[table]

    def map_inserts(self):
        return [item for item in self.inserts if item["table"] == "map_source_sku_standard"]

    def _id(self):
        self.next_id += 1
        return self.next_id

    def insert(self, table, row, sql=""):
        if table == "map_source_sku_standard":
            return self.insert_map(row, sql)
        row = dict(row)
        row["id"] = self._id()
        self.pending[table].append(row)
        self.inserts.append({"table": table, "row": row, "sql": sql})
        return 1

    def insert_map(self, row, sql=""):
        """模拟 MySQL：uk_map_listing / uk_map_source_sku 冲突 → INSERT IGNORE 跳过整行。"""
        row = dict(row)
        if (row["channel_code"], row["listing_key"]) in self.drop_map_keys:
            self.skipped.append(row)
            return 0
        for existing in self.rows("map_source_sku_standard"):
            if existing["channel_code"] != row["channel_code"]:
                continue
            if existing["listing_key"] == row["listing_key"]:
                self.skipped.append(row)
                return 0
            sku = row.get("source_sku_id")
            if sku is not None and existing.get("source_sku_id") == sku:
                self.skipped.append(row)
                return 0
        row["id"] = self._id()
        self.pending["map_source_sku_standard"].append(row)
        self.inserts.append({"table": "map_source_sku_standard", "row": row, "sql": sql})
        return 1

    def latest_ids(self):
        """v_store_product_latest 的近似：每个 (channel, store_code, listing_key) 取最新一行。"""
        rows = self.rows("fact_store_product_snapshot")
        store_latest = {}
        for row in rows:
            key = (row["channel_code"], row["store_code"])
            hour = row["snapshot_hour"]
            store_latest[key] = max(hour, store_latest.get(key, hour))
        best = {}
        for row in rows:
            key = (row["channel_code"], row["store_code"], row["listing_key"])
            current = best.get(key)
            if current is None or ((row["snapshot_hour"], row["id"])
                                   > (current["snapshot_hour"], current["id"])):
                best[key] = row
        return {row["id"] for row in best.values()
                if row["snapshot_hour"] >= store_latest[(row["channel_code"],
                                                        row["store_code"])] - timedelta(days=7)}


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, args=None):
        text = " ".join(str(sql).split())
        args = list(args or [])
        self.rowcount = 0
        if text.startswith("INSERT"):
            self._insert(text, args)
        else:
            self._rows = self._select(text, args)
        return self.rowcount

    def _insert(self, text, args):
        match = INSERT_RE.match(text)
        assert match, text
        table = match.group(1)
        columns = [column.strip() for column in match.group(2).split(",")]
        values = []
        params = list(args)
        for part in match.group(3).split(","):
            part = part.strip()
            if part == "%s":
                values.append(params.pop(0))
            else:
                values.append(_literal(part))
        assert not params, "INSERT 参数多于占位符"
        self.rowcount = self.db.insert(table, dict(zip(columns, values)), text)

    def _select(self, text, args):
        db = self.db
        if "WITH ranked AS" in text:
            return [{"id": row_id} for row_id in sorted(db.latest_ids())]
        if "v_store_product_latest" in text:
            return [{"id": row_id} for row_id in sorted(db.latest_ids())]
        if "goods_master_goods" in text:
            return [{"upc": upc, "spu": spu, "sku": sku}
                    for upc, (spu, sku) in db.master.items() if upc in args]
        if "FROM map_source_sku_standard" in text:
            groups = re.findall(r"IN \(([^)]*)\)", text)
            channels = args[:groups[0].count("%s")]
            keys = args[len(channels):]
            return [row for row in db.rows("map_source_sku_standard")
                    if row["channel_code"] in channels and row["listing_key"] in keys]
        if "FROM fact_store_product_snapshot" in text:
            return [row for row in db.rows("fact_store_product_snapshot")
                    if row["store_snapshot_id"] == args[0]]
        if "FROM fact_store_category_snapshot" in text:
            return [row for row in db.rows("fact_store_category_snapshot")
                    if row["store_snapshot_id"] == args[0]]
        if "FROM dim_store" in text:
            return [row for row in db.rows("dim_store")
                    if row["channel_code"] == args[0] and row["store_code"] == args[1]]
        if "FROM store_snapshot" in text:
            if text.startswith("SELECT *"):
                return [row for row in db.rows("store_snapshot")
                        if row["store_snapshot_id"] == args[0]]
            return [row for row in db.rows("store_snapshot")
                    if row["channel_code"] == args[0] and row["store_code"] == args[1]
                    and row.get("evidence_uri") in args[2:]]
        return []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, *args, **kwargs):
        return FakeCursor(self.db)

    def commit(self):
        self.commits += 1
        self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        self.db.rollback()

    def close(self):
        self.closed = True


def csv_row(**overrides):
    row = {
        "序号": "1",
        "商品名称": "香飘飘奶茶",
        "条码": "6901234567890",
        "店内码": "NC-1",
        "货号": "HW-1",
        "状态": "成功",
        "视频时间点": "00:00:01",
        "时刻": "10:00:00",
        "采集时间": "2026-10-20T10:00:00",
        "证据前缀": "",
        "规格": "80g/杯",
        "月售": "10",
        "库存": "10",
        "售罄": "否",
        "活动品": "否",
        "美团闪购价": "6.50",
        "饿了么价": "6.50",
        "京东到家价": "6.50",
        "采购价": "5.00/袋",
        "价格原文": "",
        "开放平台标识": "2044659046392676353",
        "SPU": "SPU-1",
        "SKU": "PLAT-SKU-X",
        "重量": "80克(g)",
        "采购价_详情": "5.00/袋",
        "采购单位_详情": "袋",
        "美团闪购状态": "已上架",
        "美团闪购零售价": "6.50",
        "美团闪购库存": "10",
        "饿了么状态": "已上架",
        "饿了么零售价": "6.50",
        "饿了么库存": "10",
        "京东到家状态": "已上架",
        "京东到家零售价": "6.50",
        "京东到家库存": "10",
        "卖点": "好喝",
        "可售日期": "不限",
        "礼袋": "0个礼袋",
        "是否力荐": "否",
        "是否标品": "标品",
    }
    row.update(overrides)
    return row


def multi_spec_rows():
    """同 SKU 同名多规格：两个条码 → 两个 listing_key，一个平台 SKU。"""
    return [
        csv_row(序号="1", 条码="6901234567890", 规格="80g/杯", 重量="80克(g)"),
        csv_row(序号="2", 条码="6901234567891", 规格="160g/杯", 重量="160克(g)",
                月售="5", 库存="3", 美团闪购库存="3", 饿了么库存="3", 京东到家库存="3"),
    ]


def write_csv(directory, rows):
    path = os.path.join(directory, "capture.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


class FakeConstraintTests(unittest.TestCase):
    """假库必须忠实模拟唯一约束，否则下面的回归测试等于没测。"""

    def map_row(self, **overrides):
        row = {
            "channel_code": "meituan", "listing_key": "k1", "listing_type": "barcode",
            "source_spu_id": "SPU-1", "source_sku_id": "SKU-X", "product_name": "商品",
            "spec_name": None, "upc": "6901234567890", "standard_spu_id": "SPU-1",
            "standard_sku_id": "SKU-1", "match_status": "matched", "match_method": "upc",
            "match_score": Decimal("1.0000"), "remark": "r",
        }
        row.update(overrides)
        return row

    def test_duplicate_listing_key_is_ignored(self):
        db = FakeDB()
        self.assertEqual(db.insert_map(self.map_row()), 1)
        self.assertEqual(db.insert_map(self.map_row()), 0)
        self.assertEqual(len(db.rows("map_source_sku_standard")), 1)

    def test_duplicate_source_sku_id_is_ignored(self):
        db = FakeDB()
        self.assertEqual(db.insert_map(self.map_row()), 1)
        self.assertEqual(db.insert_map(self.map_row(listing_key="k2")), 0)
        self.assertEqual(len(db.rows("map_source_sku_standard")), 1)

    def test_null_source_sku_id_never_conflicts(self):
        """MySQL 唯一索引里 NULL 互不冲突——这正是可以安全不写源 SKU 的原因。"""
        db = FakeDB()
        self.assertEqual(db.insert_map(self.map_row(source_sku_id=None)), 1)
        self.assertEqual(db.insert_map(self.map_row(source_sku_id=None, listing_key="k2")), 1)
        self.assertEqual(len(db.rows("map_source_sku_standard")), 2)


class LoaderCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB(MASTER)
        self.conn = FakeConn(self.db)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.csv_path = write_csv(directory.name, multi_spec_rows())

    def load(self, **kwargs):
        kwargs.setdefault("commit", True)
        with mock.patch.object(L, "_open_connection", return_value=self.conn):
            return L.load_capture(csv_path=self.csv_path, store_name="测试门店",
                                  warehouse_id="W1", tenant_id="T1", store_code="S1",
                                  status_override="partial",
                                  allow_legacy_partial_commit=True, **kwargs)


class MultiSpecSkuMappingTests(LoaderCase):
    """同 SKU 同名多规格：每个 listing 都得有 map 行。"""

    def test_every_listing_keeps_its_map_row(self):
        result = self.load()
        self.assertTrue(result["ok"])
        map_rows = self.db.rows("map_source_sku_standard")
        self.assertEqual(len(map_rows), 6)        # 2 规格 × 3 渠道
        self.assertEqual(self.db.skipped, [])      # 没有被唯一约束吞掉的行
        for channel in ("meituan", "eleme", "jddj"):
            keys = {row["listing_key"] for row in map_rows
                    if row["channel_code"] == channel}
            self.assertEqual(len(keys), 2, f"{channel} 少了 listing")
        self.assertEqual(len({row["listing_key"] for row in map_rows}), 2)

    def test_standard_identity_follows_each_listing_barcode(self):
        self.load()
        identities = {}
        for row in self.db.rows("map_source_sku_standard"):
            identities.setdefault(row["listing_key"], set()).add(
                (row["upc"], row["standard_spu_id"], row["standard_sku_id"]))
        self.assertEqual({next(iter(values)) for values in identities.values()}, {
            ("6901234567890", "SPU-1", "SKU-1"),
            ("6901234567891", "SPU-1", "SKU-2"),
        })
        for row in self.db.rows("map_source_sku_standard"):
            self.assertEqual(row["standard_spu_id"], "SPU-1")
            self.assertEqual(row["match_status"], "matched")
            self.assertEqual(row["match_method"], "upc")

    def test_map_row_never_writes_source_sku_id(self):
        """源 SKU 不进 map：uk_map_source_sku 与多规格互斥。"""
        self.load()
        inserts = self.db.map_inserts()
        self.assertEqual(len(inserts), 6)
        for item in inserts:
            self.assertNotIn("source_sku_id", item["sql"])
            self.assertIsNone(item["row"].get("source_sku_id"))

    def test_fact_rows_keep_the_real_platform_sku(self):
        """平台 SKU 仍按行留在事实表。"""
        self.load()
        facts = self.db.rows("fact_store_product_snapshot")
        self.assertEqual(len(facts), 6)
        self.assertEqual({row["source_sku_id"] for row in facts}, {"PLAT-SKU-X"})

    def test_historical_source_sku_owner_does_not_drop_new_listing(self):
        """历史行占了 (channel, source_sku_id)，新 listing 仍要落库。"""
        self.db.committed["map_source_sku_standard"].append({
            "id": 1, "channel_code": "meituan", "listing_key": "other-listing",
            "listing_type": "barcode", "source_spu_id": "SPU-9",
            "source_sku_id": "PLAT-SKU-X", "product_name": "别的商品", "spec_name": None,
            "upc": "6909999999999", "standard_spu_id": "SPU-9", "standard_sku_id": "SKU-9",
            "match_status": "matched", "match_method": "upc",
            "match_score": Decimal("1.0000"), "remark": "历史行",
        })
        result = self.load()
        self.assertTrue(result["ok"])
        new_rows = [row for row in self.db.rows("map_source_sku_standard")
                    if row["listing_key"] != "other-listing"]
        self.assertEqual(len(new_rows), 6)


class RollbackSafetyTests(LoaderCase):
    """map 行缺失时整批回滚，不提交半映射快照。"""

    def test_missing_map_row_rolls_back_whole_batch(self):
        target = C.listing_key("香飘飘奶茶", "6901234567891", {}, "PLAT-SKU-X", "SPU-1")
        self.db.drop_map_keys = {("meituan", target)}   # 故障注入：这一行被吞掉
        with self.assertRaises(L.LoadError) as caught:
            self.load()
        self.assertIn("精确 UPC 映射逐渠道完整", str(caught.exception))
        self.assertEqual(self.conn.commits, 0)
        self.assertGreaterEqual(self.conn.rollbacks, 1)
        self.assertEqual(self.db.rows("fact_store_product_snapshot"), [])
        self.assertEqual(self.db.rows("map_source_sku_standard"), [])

    def test_successful_load_commits_once(self):
        result = self.load()
        self.assertTrue(result["ok"])
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.rollbacks, 0)
        self.assertTrue(result["validation"]["ok"])
        self.assertEqual(result["validation"]["failed"], 0)


if __name__ == "__main__":
    unittest.main()
