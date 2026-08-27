# 事故:磁盘将满(HostDiskLowOS / HostDiskLowRAID)

**触发**:P1 告警,绝对阈值(house rule,不用百分比):
OS 盘 < 100 GB;/raid < 1 TB。

## 先判断是哪块盘、谁在写

```bash
ssh <node> df -h / /raid
ssh <node> du -xsh /var/lib/containerd /var/log /var/lib/arise 2>/dev/null
ssh <node> du -sh /raid/arise/volumes/* 2>/dev/null | sort -rh | head
```

## OS 盘(镜像/日志/etcd 备份)

1. 容器镜像垃圾:`crictl rmi --prune`(只清无引用镜像,安全)。
2. 日志:`journalctl --vacuum-size=2G`;容器日志上限本应由 kubelet 配置兜底。
3. 头节点专有:`/var/lib/arise/etcd-backups` 保留 28 份(约 350MB/份),
   异常增大说明轮转坏了 → 看 CronJob 最近 Job 的 rotate 容器日志。
4. **不许**清理的:`/var/lib/etcd`(头节点)、`/etc/kubernetes`。

## /raid(租户卷)

**这是客户数据。默认动作是"找人",不是"删文件"。**

1. 定位最大卷 → 反查归属:
   ```bash
   $K get pv -o json | python3 -c "…claimRef→namespace/claim…"   # 或 console 存储页
   ```
2. 联系该租户(kind=customer 的走客户联系人;internal 的直接找负责人)。
   local-path **不做磁盘层配额**(storage.yaml 头注明确此缺口,随 D2 关闭):
   quota 只挡"申领",挡不住"写超"。
3. 租户不响应且逼近满盘:先 cordon 该节点(挡新卷),再按合同条款走升级流程。
   **任何删除都要 approvedBy 记录。**
4. Retain 类(arise-longterm)已释放但未删的 PV 是常见大头:
   `$K get pv | grep Released` → 与 owner 确认后(双人复核):
   `$K patch pv <pv> -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'`
   ——只有这样 local-path 才会在节点上跑 teardown 真正删掉 `/raid/arise/volumes/<pv>_<ns>_<name>`;
   **直接 `kubectl delete pv` 只删对象、不删数据**(目录留在 NVMe 上,直到有人 SSH 上去 `rm -rf`)。
   删完在节点上确认目录已不存在,再把 pv 名、时间、复核人写进 evidence。

## 事后

- 若因写超:把该租户与容量写进 D2 决策材料(磁盘层配额的优先级证据)。
- 若因镜像:查是谁在节点上拉了什么(crictl images + 审计日志)。
