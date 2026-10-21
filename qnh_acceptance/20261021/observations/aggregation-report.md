# 跨分片异常聚合：观测终态分片聚合后仍是观测终态

## 结论

跨分片「异常观测聚合」已实现并用真机两片观测终态产物离线验证：

- 含观测终态分片时，聚合终态是 `observed_with_exceptions`，**不是 success**；
  `observation_complete=true`、`business_complete=false`。
- 聚合的宽松门禁通过、**严格业务门禁拒绝**；入库入口在打开数据库连接之前拒绝。
- 分片级平台计数差按类目登记成 `segment_platform_count_mismatch`，**平台数原样保留**
  （52 / 44 不许改成 51 / 43），且必须与聚合自己的分片记录对得上。
- 渠道行缺失凭据跟着卡重编号搬进聚合 sidecar（下标 = 聚合后序号），凭据内容原样保留。

没有连接或修改业务数据库（`db_connections_opened=0`），没有 DDL、回填、dry-run 入库或正式提交。
真机采集产物与历史 20261020 聚合 CSV/manifest 哈希未变。

## 真机两片聚合结果

| 类目 | 平台标题数 | 实抓卡数 | 聚合后唯一卡 | 分片终态 |
|---|---:|---:|---:|---|
| 猫粮 | 52 | 51 | 51 | observed_with_exceptions |
| 猫零食 | 44 | 43 | 41 | observed_with_exceptions |

- 卡片并集 **92**（51 + 43 = 94，跨类目重复 2 张）：诚实一口奶糕罐头、蓝氏猫罐头
  在两个叶子类目里是同一 SKU 同一规格，按既有卡级去重口径只留一次。
- SKU 行 **110**（65 + 47 − 2）；详情完整 **90/92**（两张含未知子行的卡不算完整）。
- 平台标题数合计 96，并集 92，差 4 = 2 跨类目重复 + 2 平台计数差。

### 聚合例外清单（5 条）

- `segment_platform_count_mismatch` 萌宠专区/猫粮：52 vs 51。
- `segment_platform_count_mismatch` 萌宠专区/猫零食：44 vs 43。
- `channel_row_absent` SKU 2098339639743791201 / 京东到家 / 鳕鱼虾味40g。
- `channel_row_absent` SKU 2112073004715569247 / 京东到家 / 3包。
- `channel_row_absent` SKU 2112073004715569247 / 美团闪购 / 3包。

凭据挂的还是真机详情页 XML（`*.detail_32_0.xml`、`*.detail_40_0.xml`），门禁复解析时
逐页校验 hash 并重新确认「该 SKU 在该渠道真的没有行」。

## 契约与门禁

### 聚合不再是「成功或失败」二选一

- 聚合终态 = 分片终态的最坏值：有一片是观测终态，聚合就是观测终态。
- `progress.exceptions` 逐条登记；聚合级新增 `segment_platform_count_mismatch`。
  旧的 `platform_count_mismatch` 是分片级口径（`platform_count` 必须等于该分片自己的
  `scope.expected_product_count`），直接搬到聚合会把 52 和 92 混为一谈，所以另立类型。
- 该例外必须对得上聚合 `scope.segments[]` 里那条分片记录的
  `expected_count`/`captured_count`，改数或写不存在的类目都会被门禁拒绝。
- 聚合 `scope.expected_product_count` 仍是去重后的时间窗并集（92），不因观测例外改写。

### 凭据跟着卡搬

- 分片 sidecar 的 `observations`（卡片序号 → SKU → 渠道 → 缺行凭据）按聚合后的主 CSV
  序号重编号；凭据内容（区块状态、原因、详情 XML 路径与 hash）原样保留。
- 跨类目重复的卡被丢弃时，它那一侧的凭据一起丢弃（保留的是先出现那次观测的凭据）；
  分片自己的 manifest 仍留着原始例外，聚合 `scope.observation.segments[]` 也逐片留档。

### 观测终态不许变成功

- `validate_segments` 接受 `observed_with_exceptions` 分片：平台目标数保留不改写，
  实抓行数照常计入 raw 合计；严格门禁下该分片仍判「未完成（观测终态，业务不完整）」。
- 聚合写盘后自查：观测终态聚合必须仍被 `require_complete=True` 拒绝，被放行就是 bug。
- 入库入口（`qnh_load_db`）先判观测契约再判多 SKU，理由说得准：
  「观测契约产物不入库：state='observed_with_exceptions'（只有 success 可入库）」。
- 采集流水线拿到聚合产物时不再给「续跑」或「重新采集一次」的采集命令（聚合不是一次
  采集，那些命令必然报错），改为指向分片器的观测模式。

## 验证

- 新增回归 `tests/unit/test_qnh_observed_aggregation.py`（11 项）：观测分片聚合成观测
  聚合、计数差按类目登记且平台数不 lowered、凭据随重编号搬运、成功分片与观测分片混聚
  仍保留全部行、纯成功分片聚合仍是 success、不带 `--observe-exceptions` 仍拒绝、
  伪造/错数的分片计数差被拒、聚合被严格门禁拒绝、入库入口连库前拒绝、采集流水线对
  聚合给分片器指引。
- 其中一项直接用真机两片产物（`cat_food` 51 卡 / 65 SKU、`cat_snacks` 43 卡 / 47 SKU）
  离线聚合，产物不在本机时自动跳过。
- 端到端 CLI 复跑：`qnh_category_pipeline.py --aggregate-only --observe-exceptions
  --capture-only` 只落 manifest，不入库、不连库。
- 牵牛花专项：**372 passed、8 skipped**。
- 全仓单元测试：**2,867 passed、3 failed、12 skipped**。
  3 项失败仍是既有模型提供商配置断言（`admin_console/test_model_service.py` 两项、
  `test_config_package.py` 一项），未改这些模块。

## 产物

- `qnh_acceptance/20261021/observations/aggregation/verification.json`：聚合终态、两片
  计数、例外清单、宽松/严格门禁结果、入库拒绝与零连接的独立记录。
- `qnh_acceptance/20261021/observations/aggregation/work/`：聚合 CSV、manifest、
  sidecar（凭据已重编号）与 capture.state.json。

## 尚未做与边界

- **全店长轮次仍未启动**（未获批）。本轮只做跨片聚合的逻辑与离线验证。
- 「未知」渠道状态仍未设计入库映射：多 SKU 或观测终态的聚合都被入库入口拦住。
- 未处理 F3/F5/F6 三条评审遗留（异常 XML 复解析抛错边界、sidecar 落盘与 CSV 改名
  的先后、例外清单重建时丢 evidence_pages）——本轮的凭据是从 sidecar 原样搬运，
  不重建，所以不受影响；但独立重造例外清单的路径仍要修。
- 聚合的跨类目去重沿用既有卡级口径（单卡按条码优先身份 + 规格，多 SKU 卡按 SPU）。
  同名同规格但身份不同的卡不会被合并（宁可多留，不许静默吞 SKU）。
