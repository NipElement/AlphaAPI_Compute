# 事故:GPU 故障(Xid / ECC / 掉卡)

**触发**(GPU Operator 落地后):DCGM 告警;或 `nvidia.com/gpu` allocatable 从 8
掉档;或客户报障。**在 DCGM 接好之前(Day-0 步骤 7),这一类只能靠客户报障与
verify-dgx 的 DGX-03 抽查——这正是 verify-dgx 对缺席 DCGM 打 WARN 的原因。**

## 快速定性

```bash
ssh <node> nvidia-smi                      # 掉卡? ERR? 温度/功耗异常?
ssh <node> nvidia-smi -q | grep -A2 'Xid'  # 最近 Xid
ssh <node> dmesg -T | grep -iE 'xid|nvrm' | tail -20
```

Xid 速查(完整表见 NVIDIA Xid Catalog):
- **63/64(ECC page retirement / row remap)**:单卡可继续观察;重复出现 → RMA 征兆。
- **79(GPU fell off the bus)**:硬故障,整节点走维护。
- **48(DBE ECC)**:该卡停用。
- **13/31(应用非法访存)**:通常是**客户代码**问题,不是硬件——先看是谁的 pod。

## 处置

1. **客户代码类**(13/31 且单租户复现):把证据(Xid + pod 对应关系)给到租户,
   不动硬件。
2. **单卡硬件类**:当前粒度是整节点(单卡隔离属 D5/切分决策)。走计划维护:
   ```bash
   # console 或直接 CR;MAINTENANCE 会先等 VAST 合同结束、排空所有租户
   desiredOwner: MAINTENANCE, approvedBy: <你>, reason: "GPU <uuid> Xid <n>"
   ```
3. **DIRECT 客户的节点**:先通知客户(是他们付费的机器),约定窗口再进维护。
4. 修复/换卡后:`MAINTENANCE → ARISE`(sanitize + health 是出场必经),
   然后 `nvidia-smi` 全量自检 + (有 DCGM 后) `dcgmi diag -r 2`,
   最后 verify-dgx 确认 DGX-03/04 恢复。

## 事后

- Xid、SN、处置时间线入 evidence;重复 Xid 的卡进 RMA 台账。
- 若客户负载因此中断:按合同的 SLA 条款记账(WS5 计量台账落地后自动化)。
