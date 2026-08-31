# 事故:节点被隔离(QUARANTINED)——以及怎么把它放出来

**触发**:controller 把节点打进 QUARANTINED。console 会显示
"requires human-approved repair"——这本 runbook 就是那个 repair 的定义。

## 为什么会进隔离(reason 字段是第一线索)

| reason | 含义 | 典型根因 |
|---|---|---|
| `DrainBlocked` | 排空超过 `DRAIN_TIMEOUT_SECONDS`(600s)仍有租户 pod 在 | **两种,message 会写明是哪一种**:(a) 驱逐被拒(PDB 太紧)——message 含 `eviction blocked past timeout` 和 429;(b) 驱逐被接受但 pod 不死——message 含 `eviction was ACCEPTED but ... never terminated`,查 pod 的 `terminationGracePeriodSeconds`(准入上限 300s)、finalizer、卡住的卷卸载 |
| `AllocationNonZero` | 排空后 GPU 仍被占 | 泄漏的 pod;device plugin 记账错 |
| `PreListCheckFailed` | 上架前检查失败 | 节点 NotReady / DiskPressure |
| `ListOutcomeUnknown` | 上架结果不明且读不回 | marketplace API 抖动 |
| `ListReadbackMismatch` | 上架被接受但读回说没上 | marketplace 侧不一致 |
| `NodeNotFound` | 逻辑名解析不到节点 | label 被清;节点被删 |
| `SanitizationFailed` | 清理检查未过 | 见失败的具体 check |
| `OperatorRequested` | 人工紧急隔离 | 按当时的 reason 记录 |

```bash
$K get nodeownership <node> -o jsonpath='{.status.conditions}' | python3 -m json.tool
$K get events --field-selector involvedObject.name=<node> --sort-by=.lastTimestamp | tail
```

## 谁会告诉你(2026-08-31 之前:没有人)

隔离是 controller 的「停下来叫人」状态,在此之前**没有任何告警看它**:节点
`sum(arise_node_owner)` 依然等于 1,OwnerConflict 不会响。现在有两条:

| 告警 | 表达式 | 含义 |
|---|---|---|
| `NodeQuarantined` (P1) | `arise_node_owner{owner="QUARANTINED"} == 1` | 本 runbook 的入口 |
| `NodeTransitionStuck` (P1) | `arise_node_transition_seconds > 1800` | 交接卡在某个中间态,而排空超时**本该**先把它隔离——它没隔离,这件事本身就是故障 |

`NodeTransitionStuck` 响而 `NodeQuarantined` 不响,说明卡在 DRAINING 以外的
阶段(SANITIZING / HEALTH_CHECK / PENDING),那里没有截止时间兜底;先看
`arise_node_transition_seconds` 的 `phase` 标签,再看该阶段的日志。

## 放行三步(顺序不可换)

1. **修根因**,并留下证据(上表对应:删卡死 pod / 修 PDB / 等 marketplace
   恢复 / 恢复 label……)。`ListOutcomeUnknown/Mismatch` 类,先人工对账
   marketplace 侧的真实 listed/contract 状态——**不确定永远不当作零**。
2. **经状态机放行**,永远不要手剥 taint/label(那是 OwnerConflict 的告警面):
   ```bash
   cat <<Y | $K apply -f -
   apiVersion: infrastructure.arise.ai/v1alpha1
   kind: NodeOwnership
   metadata: { name: <node> }
   spec:
     desiredOwner: ARISE            # 或 MAINTENANCE(还要修硬件时)
     transitionId: t-repair-$(date +%s)
     approvedBy: <你>@ariselabs.ai  # 审计必填,console 也会强制
     reason: "repair: <一句话根因>"
   Y
   ```
   出场必经 sanitize + health(CRD 的 CEL 保证 ARISE 出场不可跳过)。
3. **验证**:phase=READY、taint 清空、`verify-dgx` 或 lab `verify.sh` 过门;
   OwnerConflict / ControllerReconcileErrors 两条告警归零。

## dgx 特有:none 适配器下的 VAST 残留

`VastStateWithoutMarketplace` / `InheritedTransitionState` 条件(controller
的 no-marketplace 门)不是隔离,是**持仓**:节点带着 marketplace 痕迹而当前
没有适配器可查证。处理 = 人工对账真实 marketplace → 清掉痕迹(或恢复适配器)
→ 控制器自行继续。细节见 capacity_controller.py 该门的注释。
