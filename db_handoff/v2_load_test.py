"""历史 v2 表结构实测脚本；线上库已精简，保留作历史参考，禁止直接运行。"""
raise SystemExit("此脚本对应已删除的 v2 / 11 表结构；请用仓库根目录 qnh_load_db.py 测试当前流程")

import glob, hashlib, json, os, re, sys, uuid, datetime as dt, collections as C
import pymysql

SKILL = os.path.expanduser("~/.claude/skills/meituan-store-capture/scripts")
sys.path.insert(0, SKILL)
import parse_store_products as P  # noqa: E402

cfg = json.load(open(os.path.expanduser("~/.claude/skills/qnh-database/db_config.json")))
conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
                       database="retail_price_snapshot", charset="utf8mb4", autocommit=False)
cur = conn.cursor()
sha = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()
num = lambda v: None if v in (None, "", -1, -1.0, "-1") else v


def insert(table, row):
    cols = ", ".join(row)
    cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join(['%s'] * len(row))})", list(row.values()))


def snapshot(store_code, name, method, started, **extra):
    sid, bid = str(uuid.uuid4()), str(uuid.uuid4())
    insert("dim_store", dict(channel_code="meituan", store_code=store_code, store_name=name, store_role="competitor"))
    insert("store_snapshot", dict(store_snapshot_id=sid, batch_id=bid, snapshot_hour=started.replace(minute=0, second=0, microsecond=0),
                                  channel_code="meituan", store_code=store_code, store_name=name, store_role="competitor",
                                  status="success", capture_method=method, started_at=started, **extra))
    return sid, bid, started.replace(minute=0, second=0, microsecond=0)


# ---------------- 滚屏数据：满佳喜（榆林店）整段 501 屏 ----------------
run = "/Users/jalen/artemis/web_runs/20260930-103245"
man = json.load(open(f"{run}/manifest.json"))
started = dt.datetime.fromisoformat(man["started_at"])
first = P.read_lines(f"{run}/text/0000.txt")
store = P.store_name(first)
head = first[:first.index("搜索店内商品")]
sid, bid, hour = snapshot("mt_test_manjiaxi_yulin", store, "h5_scroll_text", started,
                          finished_at=dt.datetime.fromisoformat(man["finished_at"]), device_id=man["device"],
                          stop_reason=man["stop_reason"], screen_count=len(man["shots"]), evidence_uri=run,
                          store_score=float(head[1][2:]), delivery_time_text=head[2],
                          avg_delivery_minutes=int(re.sub(r"\D", "", head[2]) or 0) or None,
                          bulletin=head[3][3:] if head[3].startswith("公告") else None)
# 门店活动
promos = C.Counter(l for l in head[4:])
for i, (t, n) in enumerate(promos.items(), 1):
    m = re.match(r"^(\d+)减(\d+)$", t)
    if m:
        ptype, th, dis = "full_reduction", m.group(1), m.group(2)
    elif t.startswith("新客减"):
        ptype, th, dis = "new_customer", 0, re.sub(r"\D", "", t)
    elif re.match(r"^领\d+元.*券$", t):
        ptype, th, dis = "coupon", None, re.sub(r"\D", "", t)
    else:
        ptype, th, dis = "other", None, None
    insert("fact_store_promotion_snapshot", dict(store_snapshot_id=sid, batch_id=bid, snapshot_hour=hour, channel_code="meituan",
                                                 store_code="mt_test_manjiaxi_yulin", promo_type=ptype, promo_text=t,
                                                 threshold_amount=th, discount_amount=dis, promo_count=n, display_order=i))
# 类目栏
_side = P.sidebar_categories(first, first.index("搜索店内商品") + 1)
for i, c in enumerate(dict.fromkeys(c for c in first[first.index("搜索店内商品") + 1:] if c in _side), 1):
    insert("fact_store_category_snapshot", dict(store_snapshot_id=sid, batch_id=bid, snapshot_hour=hour, channel_code="meituan",
                                                store_code="mt_test_manjiaxi_yulin", category_key="name:" + c, category_name=c,
                                                category_order=i))
# 每屏原始文字
for s in man["shots"]:
    idx = s["index"]
    lines = P.read_lines(f"{run}/{s['text']}")
    scr = f"{run}/text/{idx:04d}.screen.txt"
    raw = {"lines": lines, "screen_lines": P.read_lines(scr) if os.path.exists(scr) else [], "video_time": s["video_time"]}
    body = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    insert("store_raw_data_snapshot", dict(store_snapshot_id=sid, batch_id=bid, channel_code="meituan", store_code="mt_test_manjiaxi_yulin",
                                           store_name=store, record_type="screen", screen_index=idx, evidence_uri=s["shot"],
                                           collected_at=started + dt.timedelta(seconds=s["elapsed"]), raw_data=body, record_hash=sha(body)))
# 商品卡片
import csv
seen = C.Counter()
for r in csv.DictReader(open(f"{run}/products.csv", encoding="utf-8-sig")):
    seen[r["商品名"]] += 1
    n = seen[r["商品名"]]
    key = sha("name:" + r["商品名"] + (f"#{n}" if n > 1 else ""))
    mb = re.match(r"买(\d+)件¥([\d.]+)/件", r["多件价"])
    lim = re.search(r"限(\d+)", r["折扣"])
    st = re.match(r"([\d.]+)(\D*)", r["库存"])
    shot = r["截图"]
    vt = next((s["video_time"] for s in man["shots"] if s["shot"] == shot), None)
    insert("fact_store_spu_price_snapshot", dict(
        batch_id=bid, store_snapshot_id=sid, snapshot_hour=hour, capture_method="h5_scroll_text", channel_code="meituan",
        store_code="mt_test_manjiaxi_yulin", store_name=store, store_role="competitor", listing_key=key, product_name=r["商品名"],
        has_multi_spec=1 if r["多规格"] else 0, front_display_price_amount=r["售价"] or None, is_from_price=1 if r["起价"] else 0,
        price_condition=r["优惠条件"] or None, user_final_price_amount=r["到手价"] or None, original_price_amount=r["原价"] or None,
        multi_buy_text=r["多件价"] or None, multi_buy_quantity=mb.group(1) if mb else None, multi_buy_unit_price_amount=mb.group(2) if mb else None,
        discount_text=r["折扣"] or None, limit_purchase_quantity=lim.group(1) if lim else None, promo_remaining_text=r["优惠余量"] or None,
        month_sales_text=("月售" + r["月售"]) if r["月售"] else None, month_sales_qty=int(r["月售"].rstrip("+")) if r["月售"] else None,
        want_to_buy_count=r["想买人数"] or None, praise_rate_pct=r["好评率"].rstrip("%") or None,
        stock_text=r["库存"] or None, stock_quantity=st.group(1) if st else None, stock_unit=(st.group(2) or None) if st else None,
        other_text=r["其他"] or None, sale_status="available", evidence_uri=shot or None, evidence_video_time=vt))
    insert("map_source_sku_standard", dict(channel_code="meituan", listing_key=sha("name:" + r["商品名"]), listing_type="name",
                                           product_name=r["商品名"])) if n == 1 else None

# ---------------- 接口数据：乐购达（湖里店）休闲食品 全部分页 ----------------
files = sorted(glob.glob("/Users/jalen/artemis/store_capture/final/v2/v2_休闲食品__*_p*.json"), key=lambda f: int(re.search(r"_p(\d+)", f).group(1)))
sid2, bid2, hour2 = snapshot("mt_test_legouda_huli", "乐购达超市（湖里店）", "h5_api_replay", dt.datetime(2026, 9, 18, 11, 5),
                             source_store_id="-XHGb1R14At6B7FGJI29sgI", platform_spu_total_text="搜索店内商品，约8000件")
done_spu = set()
for f in files:
    page = int(re.search(r"_p(\d+)", f).group(1))
    d = json.load(open(f))["data"]
    order = {x: i + 1 for i, x in enumerate(d.get("allSortedSpuId") or [])}
    for s in d["product_spu_list"]:
        body = json.dumps(s, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        h = sha(body)
        cur.execute("INSERT IGNORE INTO store_raw_data_snapshot (store_snapshot_id, batch_id, channel_code, store_code, store_name, record_type,"
                    " source_api, request_tag_id, page_index, source_spu_id, collected_at, raw_data, record_hash) VALUES"
                    " (%s,%s,'meituan','mt_test_legouda_huli','乐购达超市（湖里店）','product','quickbuy/v1/poi/sputag/products',%s,%s,%s,%s,%s,%s)",
                    (sid2, bid2, "1674613554", page, str(s["id"]), dt.datetime(2026, 9, 18, 11, 5), body, h))
        cur.execute("INSERT IGNORE INTO fact_store_spu_category_rel (store_snapshot_id, batch_id, snapshot_hour, channel_code, store_code,"
                    " source_spu_id, category_tag_id, sub_category_tag_id, position_in_category, page_index, is_ad) VALUES"
                    " (%s,%s,%s,'meituan','mt_test_legouda_huli',%s,'1674613554',%s,%s,%s,%s)",
                    (sid2, bid2, hour2, str(s["id"]), s.get("tag"), order.get(s["id"]), page,
                     1 if (d.get("product_extra_info") or {}).get(str(s["id"]), {}).get("is_ad") else 0))
        if s["id"] in done_spu:
            continue
        done_spu.add(s["id"])
        cats = {c["level"]: c for c in s.get("standardCategorys") or []}
        up = s.get("unify_price") or {}
        acts = [num(((k.get("unify_price") or {}).get("activity_info") or {}).get("activity_price")) for k in s["skus"]]
        insert("fact_store_spu_price_snapshot", dict(
            batch_id=bid2, store_snapshot_id=sid2, snapshot_hour=hour2, capture_method="h5_api_replay", channel_code="meituan",
            store_code="mt_test_legouda_huli", store_name="乐购达超市（湖里店）", store_role="competitor",
            source_store_id="-XHGb1R14At6B7FGJI29sgI", category_name="休闲食品", category_tag_id="1674613554", sub_category_tag_id=s.get("tag"),
            **{f"platform_category_l{i}_id": cats[i]["id"] for i in (1, 2, 3) if i in cats},
            **{f"platform_category_l{i}_name": cats[i]["name"] for i in (1, 2, 3) if i in cats},
            listing_key=sha(f"spu:{s['id']}"), source_spu_id=str(s["id"]), product_name=s["name"], sku_count=len(s["skus"]),
            has_multi_spec=1 if len(s["skus"]) > 1 else 0, front_display_price_amount=num(up.get("price")) or s.get("min_price"),
            original_price_amount=num(up.get("underlined_price")), activity_price_amount=min([a for a in acts if a] or [None], key=lambda x: x or 0),
            member_price_amount=num(s.get("member_price")), activity_tag_id=s.get("activity_tag_id") or None, activity_type=s.get("activity_type"), activity_type_code=s.get("wmActivityType"),
            month_sales_text=s.get("month_saled_content") or None, want_to_buy_count=s.get("want_to_Buy"),
            selling_points="|".join(s.get("selling_points") or []) or None, picture_url=s.get("picture"),
            sale_status="available" if any(k.get("stock") for k in s["skus"]) else "out_of_stock", raw_record_hash=h))
        for k in s["skus"]:
            ai = (k.get("unify_price") or {}).get("activity_info") or {}
            spec = json.loads(k.get("spec_num_unit_string") or "{}")
            insert("fact_store_sku_price_snapshot", dict(
                batch_id=bid2, store_snapshot_id=sid2, snapshot_hour=hour2, channel_code="meituan", store_code="mt_test_legouda_huli",
                store_name="乐购达超市（湖里店）", store_role="competitor", category_name="休闲食品", category_tag_id="1674613554",
                source_spu_id=str(s["id"]), source_sku_id=str(k["id"]), product_name=k["name"], spec_name=k.get("spec") or None,
                combine_spec=k.get("combine_spec") or None, net_content_value=num((spec.get("weight") or {}).get("numHigh")) or None,
                net_content_unit=(spec.get("weight") or {}).get("unit") or None, pack_count=num((spec.get("total_count") or {}).get("num")) or None,
                pack_unit=(spec.get("total_count") or {}).get("unit") or None, upc=k.get("upccode") or None,
                front_display_price_amount=num((k.get("unify_price") or {}).get("price")) or k.get("price"), original_price_amount=num(k.get("origin_price")),
                activity_price_amount=num(ai.get("activity_price")), activity_type=num(ai.get("activity_type")), activity_id=num(ai.get("act_id")),
                activity_tag_id=k.get("activity_tag_id_for_spu") or None, activity_price_source=ai.get("activity_price_source"),
                discount_strength=num(ai.get("discount_strength")), subsidy_price_amount=num(ai.get("sg_subsidy_price")),
                member_price_amount=num(k.get("member_price")), box_price_amount=k.get("box_price"), min_purchase_quantity=k.get("min_order_count"),
                limit_purchase_quantity=num(ai.get("quota_per_order")) or None, stock_quantity=k.get("stock"), activity_stock_quantity=k.get("activity_stock"),
                sale_status="available" if k.get("stock") else "out_of_stock", picture_url=k.get("picture"), raw_record_hash=h))
            insert("map_source_sku_standard", dict(channel_code="meituan", listing_key=sha(f"sku:{k['id']}"), listing_type="sku",
                                                   source_spu_id=str(s["id"]), source_sku_id=str(k["id"]), product_name=k["name"],
                                                   spec_name=k.get("spec") or None, upc=k.get("upccode") or None))

# ---------------- 查询验证 ----------------
def q(sql):
    cur.execute(sql)
    return cur.fetchall()

print("写入行数:")
for t in ["dim_store", "store_snapshot", "store_raw_data_snapshot", "fact_store_promotion_snapshot", "fact_store_category_snapshot",
          "fact_store_spu_category_rel", "fact_store_spu_price_snapshot", "fact_store_sku_price_snapshot", "map_source_sku_standard"]:
    print(f"  {t:32} {q(f'SELECT COUNT(*) FROM {t}')[0][0]}")
print("视图（按门店）:", q("SELECT store_name, capture_method, COUNT(*), ROUND(AVG(front_display_price_amount),2) FROM v_store_spu_price_latest GROUP BY 1,2"))
print("滚屏-字段覆盖:", q("SELECT COUNT(price_condition), SUM(is_from_price), COUNT(user_final_price_amount), COUNT(original_price_amount),"
                          " COUNT(multi_buy_text), COUNT(discount_text), COUNT(limit_purchase_quantity), COUNT(stock_quantity), COUNT(praise_rate_pct),"
                          " SUM(has_multi_spec), COUNT(evidence_video_time) FROM fact_store_spu_price_snapshot WHERE capture_method='h5_scroll_text'"))
print("滚屏-同名商品:", q("SELECT product_name, COUNT(*) FROM fact_store_spu_price_snapshot WHERE capture_method='h5_scroll_text' GROUP BY 1 HAVING COUNT(*)>1 LIMIT 3"))
print("门店活动:", q("SELECT promo_type, promo_text, threshold_amount, discount_amount, promo_count FROM fact_store_promotion_snapshot ORDER BY display_order"))
import html as _h, re as _re
_page = open("/Users/jalen/artemis/db_handoff/门店商品价格快照库_使用说明.html", encoding="utf-8").read()
_sqls = [_re.sub(r"<[^>]+>", "", _h.unescape(x)) for x in _re.findall(r"<pre>(.*?)</pre>", _page, _re.S)]
for _i, _sql in enumerate(_sqls, 1):
    _sql = _sql.replace("满佳喜超市（榆林店）", store).replace("要看的竞对门店编码", "mt_test_legouda_huli")
    cur.execute(_sql); _rows = cur.fetchall()
    print(f"文档查询{_i}: {len(_rows)} 行", _rows[:1])
print("原始屏 JSON 取值:", q("SELECT screen_index, JSON_LENGTH(raw_data->'$.lines'), raw_data->>'$.video_time' FROM store_raw_data_snapshot WHERE record_type='screen' ORDER BY screen_index LIMIT 2"))
print("最大单行原始记录(KB):", q("SELECT record_type, ROUND(MAX(LENGTH(raw_data))/1024,1), ROUND(AVG(LENGTH(raw_data))/1024,1) FROM store_raw_data_snapshot GROUP BY 1"))
conn.rollback()
print("已回滚，剩余:", q("SELECT (SELECT COUNT(*) FROM store_snapshot),(SELECT COUNT(*) FROM fact_store_spu_price_snapshot),(SELECT COUNT(*) FROM store_raw_data_snapshot)"))
