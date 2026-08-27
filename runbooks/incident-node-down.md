# 事故:节点失联(NodeNotReady)

**触发**:P0 告警 `NodeNotReady`(kube-state-metrics 系列,3 分钟窗口);或
console 机群页节点变灰。4 台可售节点上,一台失联 = 25% 收入容量。

## 铁律

1. **不要为了"恢复调度"去改 owner 标签/taint/cordon。** 控制器持仓
   (holding position)是设计行为;OwnerConflict 告警存在的意义就是抓住任何
   绕过控制器的手。所有权变更只经 NodeOwnership。
2. 客户合同状态**不因失联而改变**:VAST_RENTED/DIRECT_ASSIGNED 的节点失联,
   处理的是硬件,不是合同。

## 排查序(每步都可能就地终结)

```bash
K="kubectl --context <ctx>"
$K get node <物理名> -o wide          # NotReady 多久了? kubelet 心跳时间?
$K describe node <物理名> | tail -30  # conditions + 最近事件
```

1. **电源/BMC**:`ipmitool -H <bmc> chassis status`(Day-0 的 BMC 清单)。
   掉电 → 电力/机房问题,上电后 kubelet 自愈,无需集群侧动作。
2. **网络**:能 ping 通节点 IP 但 NotReady → kubelet 层;完全不通 → 交换机/线缆。
3. **kubelet**:`ssh <node> systemctl status kubelet && journalctl -u kubelet --since -30m | tail -50`。
   常见:磁盘满导致 kubelet 驱逐循环(转 incident-disk-full.md)、
   containerd 死锁(`systemctl restart containerd kubelet`)。
4. **确认恢复**:节点 Ready 后,`arise_node_owner` 应回到恰好 1;
   跑 `KUBE_CONTEXT=<ctx> ./scripts/verify-dgx.sh` 收尾。

## 超过 30 分钟仍不可恢复

- 该节点是 **DIRECT_ASSIGNED**(客户整机):这是对客户的 SLA 事件。
  按合同通知客户;修复窗口按"计划维护"走:
  `desiredOwner: MAINTENANCE`(先让状态机把帐记清),修好后 ARISE→再 DIRECT。
- 该节点是 **VAST_RENTED**:marketplace 侧租约照走;标记 BMC 工单,修好后
  控制器自行收敛。
- 硬件报修:记录 SN/BMC 日志/`nvidia-bug-report.sh`(若可达)后再动电源。

## 事后

- 在 evidence 里留:告警时间线、BMC/系统日志摘录、恢复动作与时刻。
- 若根因是磁盘/GPU,交叉链接对应 runbook 的事后段。
