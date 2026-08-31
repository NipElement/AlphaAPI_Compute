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
  **账单不会自己处理这段停机** —— 见下面「停机与账单」。
- 该节点是 **VAST_RENTED**:marketplace 侧租约照走;标记 BMC 工单,修好后
  控制器自行收敛。
- 硬件报修:记录 SN/BMC 日志/`nvidia-bug-report.sh`(若可达)后再动电源。

## 事后

- 在 evidence 里留:告警时间线、BMC/系统日志摘录、恢复动作与时刻。
- 若根因是磁盘/GPU,交叉链接对应 runbook 的事后段。

## 停机与账单:系统会做什么、不会做什么(2026-08-31 核对)

**会做**:`billing/invoice.py` 把整机按**预留时段**计费(`node-month`,按日比例),
与那台机器上有没有 pod 在跑无关 —— 这正是「包整机」的定义,DIRECT 客户不因为自己
没提交任务而少付。

**不会做**:**没有任何自动的停机抵扣**。台账记的是分配区间与预留时段,不记
「不可用」;价格本里也没有 SLA 条目(属决策 D6:合同/账务政策)。所以一次节点故障
**不会**自己变成账单上的一笔减免。

**人工抵扣怎么记(有据可查的那种)**:把预留时段裁掉不可用的那一段 ——

```bash
# 例:dgx04 从 8/10 起预留,8/18 12:00 到 8/20 09:00 整机不可用
python3 billing/invoice.py --ledger ... --pricebook billing/pricebook.yaml \
  --tenants platform/tenants.yaml --tenant tenant-direct \
  --dedicated-nodes dgx04 --dedicated-from 2026-08-10T00:00:00Z \
  --dedicated-to   2026-08-18T12:00:00Z --from ... --to ...
# 再跑一张 8/20 09:00 起的,两张合并;CSV 里的 "pro-rated N day(s)" 就是凭证,
# 可以直接和 NodeOwnership 的相位历史对账。
```

裁剪必须能对上 `NodeOwnership` 的相位轨迹(MAINTENANCE / QUARANTINED 的起止)与
本 runbook 的时间线,并连同 evidence 一起归档 —— 否则账单上少的那几天没有出处。
