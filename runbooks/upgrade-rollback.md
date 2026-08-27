# 服务升级与回滚(不重建集群)

> 以前唯一排练过的变更流程是"拆掉重建"(rebuild-verify.sh / make teardown)。
> 一个有付费客户的集群不能这么升级。本 runbook 定义**三层**变更,每层的
> 影响面、步骤与回滚点都不同。

## 层 0:平台服务代码(最常见)

gateway / tenant-portal / ops-console / capacity-controller / metering 都是
ConfigMap 挂载的 Python;升级 = 换 ConfigMap + 滚动重启。**客户负载不受影响**
(它们不经过这些服务运行,只经过这些服务被创建)。

```bash
# lab: make code && kubectl rollout restart ...   dgx: 同理用 dgx-code
make dgx-code                      # 重新生成全部代码 ConfigMap(幂等)
K="kubectl --context $DGX_KCTX -n platform-system"
# 顺序有讲究:先无状态、后单写者
$K rollout restart deploy/tenant-portal deploy/ops-console
$K rollout status  deploy/tenant-portal --timeout=120s
$K rollout status  deploy/ops-console  --timeout=120s
$K rollout restart deploy/platform-gateway       # 会话是无状态签名令牌:客户不掉线
$K rollout status  deploy/platform-gateway --timeout=120s
# 单写者最后,且一次一个(Recreate 策略保证不会双写)
$K rollout restart deploy/capacity-controller; $K rollout status deploy/capacity-controller --timeout=120s
$K rollout restart deploy/metering;            $K rollout status deploy/metering --timeout=120s
KUBE_CONTEXT=$DGX_KCTX ./scripts/verify-dgx.sh
```

**回滚**:`git checkout <上一个提交> -- services/` → 再跑一遍上面。ConfigMap 是
声明式的,回滚就是把旧内容再 apply 一次;没有"回滚工具",只有 Git。

控制器升级中途死亡是**排练过的场景**(CHAOS-01):它按 transitionId 恢复,
不会重复外部副作用。metering 同理按 Pod UID 恢复。

## 层 1:清单/策略(overlay)

```bash
make dgx-render                    # 静态门先过
make dgx-platform                  # server-side apply,幂等
KUBE_CONTEXT=$DGX_KCTX ./scripts/verify-dgx.sh
```

准入策略(VAP)变更**立即生效于新请求**,不影响已运行的 Pod。回滚同上:Git
checkout 旧 overlay 再 apply。CRD 变更只做**增量**(加 enum 值、加可选字段——
MAINTENANCE 与 spec.tenant 都是这么加的);删字段或改类型需要迁移方案,不在本
runbook 范围。

## 层 2:节点(内核/驱动/固件/kubelet)

一次一台,经状态机:

```bash
# 1. 进维护(排空所有租户、cordon、taint;有 VAST 合同先等合同)
desiredOwner: MAINTENANCE, approvedBy: <你>, reason: "kubelet 1.36.x -> y"
# 2. 等 phase=MAINTENANCE,然后在节点上做变更(apt / 固件 / 重启)
# 3. 回池:desiredOwner: ARISE —— 出场必经 sanitize + health
# 4. verify-dgx;再下一台
```

DIRECT 客户的整机:**先通知,约窗口**,进维护前客户的 Pod 会被排空(计划停机)。

## 层 3:控制面(kubeadm / etcd)

先 `etcd 快照`(runbooks/etcd-restore.md 的备份段,手动触发一次 CronJob:
`kubectl create job --from=cronjob/etcd-backup etcd-pre-upgrade -n platform-system`),
再按 kubeadm 官方升级流程(`kubeadm upgrade plan/apply`)。回滚点 = 那份快照。

## 演练记录

| 日期 | 层 | 结果 |
|---|---|---|
| 2026-08-27 | 0(gateway/controller 多次 make code + restart) | ✓ 会话不掉、矩阵 37/37 |
| 2026-08-27 | 2(MNT-01:维护进出全链路) | ✓ |
| 2026-08-27 | 0(CHAOS-01:controller 中途死亡) | ✓ 同 tid 恢复 |
| 待做 | 3(kubeadm 升级) | 需真机 |
