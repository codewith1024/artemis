# 评审遗留 F3 / F5 / F6 修复

## 结论

三条评审遗留已修复并各有回归测试：

- **F3** 列表观测阶段的异常（`QNHListEvidenceError`、契约错误、意外异常）不再甩出
  `main()`：一律走 `die()` → 还原设备（分辨率/密度/动画/无障碍）→ 写 failed 清单 →
  退出码 1。原先外抛会跳过收尾，把手机留在改过分辨率、无障碍被关的状态。
- **F5** 多 SKU sidecar 落盘挪到 CSV 改名之后：sidecar 描述的是那份 CSV，绝不能先于
  CSV 出现（先出现就是「凭据指向一份还不存在的 CSV」）。CSV 先落盘、sidecar 后落盘，
  两者之间不留半截文件。
- **F6** manifest 的「渠道行缺失」例外不再只留 sku/channel/spec：连区块状态、原因、
  详情 XML 证据指针（`evidence_pages`）一起带进 manifest，manifest 单读就能自证。
  门禁新增交叉核对：manifest 的证据与 sidecar 凭据必须指向同一份详情 XML。

没有连接或修改业务数据库，没有 DDL、回填、dry-run 入库或正式提交。

## 改动

| 文件 | 改动 |
|---|---|
| `qnh_product_scan.py` | 新增 `observe_list_or_die()`：观测失败按失败收口；sidecar 改到 CSV 之后写；manifest 例外带凭据 |
| `qnh_capture_common.py` | `validate_observation_exceptions` 交叉核对 manifest 证据与 sidecar 凭据；`GateReport.listings` |
| `qnh_category_pipeline.py` | 聚合的渠道行缺失例外同样带凭据（证据跟着凭据一起搬） |

### F3：观测失败不许甩出 main()

```python
def observe_list_or_die(category):
    try:
        return collect_list_observation(category)
    except C.CaptureContractError as exc:
        die(f"列表观测失败：{exc}")
    except OBS.QNHListEvidenceError as exc:
        die(f"列表证据复算失败：{exc}")
    except Exception as exc:
        die(f"列表观测异常：{type(exc).__name__}: {exc}")
```

`die()` 先 `cleanup()` 再退出：还原屏幕/密度/动画/无障碍，落一份 failed 清单
（逐屏证据留在盘上），退出码 1。分片按失败重采，不会留下一台改过设置的手机。

### F5：sidecar 后于 CSV

`write_csv()` 的顺序改成：写 CSV 临时文件 → 写 sidecar 内容 → **CSV 改名落盘** →
**sidecar 落盘**。sidecar 用 `atomic_write_json`（自身 tmp+replace），不会露出半截文件。
若在两次改名之间崩溃：留下 CSV 无 sidecar、无 manifest → 下轮重采（fail closed）；
不会出现「sidecar 指向一份还不存在的 CSV」。

### F6：例外带证据

manifest 的渠道行缺失例外现在带 `observation` / `section_status` / `reason` /
`evidence_pages`。门禁交叉核对：

- manifest 声明的 `evidence_pages` 与 sidecar 凭据不一致 → **失败**（两份证据矛盾）。
- manifest 完全没写 `evidence_pages` → **警告**（旧产物如此，证据只在 sidecar 里）。
  真机 20261021 的两份旧分片产物现在会明确报这条警告，不再是「静默没有证据」。

聚合层同样：跨片合并后的例外条目带凭据，证据指针跟着卡一起搬。

## 验证

- 新增 `tests/unit/test_qnh_review_leftovers.py`（9 项）：
  - 观测阶段三类异常都不外抛（都变成 exit 1 + 明确原因）；
  - `die()` 一定先 `cleanup()` 再退出；
  - sidecar 落盘那一刻 CSV 已经在盘上；无多 SKU 行时不产 sidecar；CSV 仍是 40 列契约。
  - manifest 例外带证据指针与区块状态。
- `tests/unit/test_qnh_observed_aggregation.py` 增 3 项门禁侧用例：证据一致放行、
  证据指向别处失败、缺证据指针只警告。共 14 项。
- 牵牛花专项：**385 passed、8 skipped**。
- 全仓单元测试：**2,879 passed、3 failed、12 skipped**（3 项失败仍是既有模型提供商断言
  `assert 'anthropic' == 'google'`，与本次改动无关）。

## 收尾与一次误触说明

- 采集/入库进程全部退出；没有数据库连接（`db_connections_opened=0`）。
- **一次误触**：写验证脚本时只打桩了 `die`/`cleanup`，没打桩 `collect_list_observation`，
  脚本真的去点了手机（尝试解析商品、切换 dump 模式、把系统动画关成 0）。没有采集商品、
  没有写库、没留采集文件（产物目录是临时目录）；发现后已把动画缩放恢复为 1
  （window/transition/animator 三项），屏幕保持 1080×2280 / density 420（与上一轮收尾
  一致），屏幕处于熄屏状态，牵牛花在前台。验证脚本已改为打桩 `collect_list_observation`，
  复跑不再碰设备。
