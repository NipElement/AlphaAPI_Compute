# 事故:节点被隔离(QUARANTINED)——以及怎么把它放出来

**触发**:controller 把节点打进 QUARANTINED。console 会显示
"requires human-approved repair"——这本 runbook 就是那个 repair 的定义。

## 为什么会进隔离(reason 字段是第一线索)

| reason | 含义 | 典型根因 |
|---|---|---|
| `DrainBlocked` | 驱逐超时被 PDB/finalizer 卡住 | 租户 PDB 设得太紧;卡死的 pod |
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
