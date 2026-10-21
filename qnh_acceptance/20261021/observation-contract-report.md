# 剩余评审遗留 F2 / F4 修复

## 结论

最后两条评审遗留已修复并有回归测试（F1 的恢复命令也顺手改稳）：

- **F1 加固** 恢复命令里的安全开关改成**按 manifest 推导**，不只看本次命令行参数：
  带观测契约的产物续跑命令自动带 `--capture-only --observe-exceptions`（两者必须同用，
  少一个扫描器就会直接拒），且永远不带 `--commit`。
- **F2** `capture.observation_contract='v1'` 只写在**真的带观测内容**（逐条例外）的
  manifest 上。观测模式跑出的普通成功产物不再挂契约——挂了就是「声称在观测契约下」
  却拿不出观测内容，入库入口也会被它无谓地拦下。续跑守卫随之变准：只有带例外的
  产物才强制「续跑必须带 `--observe-exceptions`」。
- **F4** 扫描器决定「是否升级到观测终态」时，比的是**卡片身份多重集**，不是张数。
  漏一张又错收一张，张数照样相等，但采到的不是这一页的东西——这种情况落
  `partial` 并把差异写进 `stop_reason`，不许声称观测完成、更不许声称成功。
  门禁侧同步收紧：例外态**一律**要逐屏列表证据（不只短收时），并按同一口径核对
  身份多重集。

没有连接或修改业务数据库（`db_connections_opened=0`），没有 DDL、回填、dry-run
入库或正式提交。

## F2：契约标记只给真有观测内容的产物

`_manifest_for()` 改成先算观测内容（逐条例外 + 含未知渠道的卡），再派生契约标记、
详情完整数与例外清单：

```python
if exceptions:
    capture["observation_contract"] = C.OBSERVATION_CONTRACT_VERSION
```

| 产物 | 终态 | 契约标记 |
|---|---|---|
| 观测模式跑出的普通成功（无例外） | `success` | **无**（原来也写 v1） |
| 带例外的观测终态 | `observed_with_exceptions` | `v1` |
| 带未知渠道的卡 | 必带例外 → 必带契约 | `v1` |

「带未知渠道的卡」一定带例外（例外清单就是从那些卡重建的），所以凭据支撑的「未知」
永远拿得到契约，不会因为这次收紧而被误伤。

连带修好的：续跑守卫（`qnh_product_scan`）原先会把「普通成功 + 契约」也当成必须
带 `--observe-exceptions` 的异常工件；现在只有真带例外的产物才触发。

## F4：升级到观测终态要身份对得上

扫描器侧（`cleanup()` 里的终态判定）：

```python
mismatch = C.cards_match_rows(evidence.get("cards") or [], rows)
if mismatch:
    CTX.stop_reason = ... "列表证据与采集行身份不一致：..."
    state = "partial"
elif rows_ok:
    state = "observed_with_exceptions" if (例外 or 含未知渠道的卡) else "success"
```

门禁侧（`validate_observed_state`）：例外态一律校验逐屏列表证据（原来只在
「短收」时校验）；聚合产物例外——它的逐屏证据在各个分片 manifest 里
（`scope.segments` 记着分片 manifest 路径与哈希），聚合层不再要求一份列表证据。

`_cards_match_rows` 提升为公开的 `cards_match_rows`（扫描器与门禁用同一口径，
免得两处各判各的）。

## 验证

- 新增 `tests/unit/test_qnh_observation_contract.py`（5 项）：普通成功不带契约；
  带例外才带契约；张数相同但身份不同 → `partial` 且写明差异；身份相同 →
  观测终态；身份相同且无例外 → 普通成功。
- `tests/unit/test_qnh_observed_aggregation.py` 增 2 项门禁侧用例：例外态没有列表
  证据 → 拒绝；证据张数相同但身份不同（连原始逐屏 XML 一起换掉、哈希同步）→ 拒绝。
- `tests/unit/test_qnh_observation_capture.py` 增 1 项：观测契约产物的续跑命令自带
  两个安全开关。
- 既有观测测试补上逐屏列表证据（例外态的新要求）：`test_qnh_exception_observation.py`
  30 项全通过。
- 牵牛花专项：**393 passed、8 skipped**。
- 全仓单元测试：**2,887 passed、3 failed、12 skipped**（3 项失败仍是既有模型提供商
  断言 `assert 'anthropic' == 'google'`，与本次改动无关）。
- 真机两片产物（猫粮 51 卡 / 猫零食 43 卡）重新聚合复验：宽松门禁 PASS、严格门禁
  FAIL、零证据警告，0 次数据库连接。

## 边界

- 「未知」渠道状态仍未设计入库映射：多 SKU 与观测终态产物都被入库入口拦住。
- 门禁现在对**所有**例外态要求逐屏列表证据。手写/旧产物若没有这份证据会开始被
  拒；真机 20261021 的两份产物有证据，不受影响。
- 平台后台多计 1、某 SKU 不列出某渠道的后台原因仍未知：本地契约只诚实记录 UI 已
  观察事实。
- 全店长轮次仍未启动（未获批）。
