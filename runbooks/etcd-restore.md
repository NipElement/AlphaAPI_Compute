# etcd 备份与恢复

> 备份不等于能恢复。这本 runbook 的恢复段落**必须在 lab 上演练过**才算数——
> 最近一次演练记录在文末。

## 备份在哪、怎么来的

`platform/overlays/dgx/etcd-backup.yaml`:每 6 小时一次 CronJob,在头节点上
`etcdctl snapshot save` → `etcdutl snapshot status` 完整性校验 → 时间戳改名 +
保留最近 28 份(7 天)。落盘 `/var/lib/arise/etcd-backups/`(头节点本地盘)。

三段式(save → verify → rotate)的原因:etcd 镜像是 **distroless、没有
`/bin/sh`**(2026-08-27 在 lab 排练时当场失败发现)。etcdctl/etcdutl 用纯参数跑在
initContainers 里,只有需要 shell 的改名+轮转跑在 digest 固定的 busybox 里。

**镜像与集群的 etcd 静态 Pod 同源同 digest**(registry.k8s.io/etcd@sha256:3971894…),
一个集群里只有一个 etcd 版本。

### ⚠ 离节点副本(尚未自动化,D1/D2 决策后补)

本地快照挡得住 etcd 损坏与误删,挡不住**头节点整机丢失**。在自动化落地前,
每次变更窗口手工执行:

**两份都要拉,不是只有 etcd**:etcd 是「谁拥有哪台机器」,台账是「客户欠多少钱」。
掉头节点会同时失去这两样,而它们各在各的目录里。

```bash
# 1) 最新的 etcd 快照
scp head-node:/var/lib/arise/etcd-backups/$(ssh head-node 'ls -1t /var/lib/arise/etcd-backups | head -1') ./offsite/
# 2) 最新的台账副本 + 它的 .meta(.meta 里的 head 可脱离 HMAC 钥匙与 Prometheus 锚点比对)
L=$(ssh head-node 'ls -1t /var/lib/arise/ledger-backups/*.jsonl | head -1')
scp "head-node:$L" "head-node:${L%.jsonl}.meta" ./offsite/
```

## 先演练:这份快照到底能不能恢复(不停任何东西)

```bash
make dgx-restore-drill      # 或 KUBE_CONTEXT=$DGX_KCTX scripts/etcd-restore-drill.sh
```

一个一次性 Job:挑最新快照 → `etcdutl snapshot restore` 到 emptyDir → 读回
`member/snap/db` 与 WAL 是否真的生成。**跑的就是下面第 3 步那条命令**,只是落在
临时目录里,集群完全不受影响。

CronJob 每次备份后跑的 `etcdutl snapshot status` 证明文件**可读**;这条演练证明
它**可恢复**(会校验完整性哈希、真的建出数据目录)。实测 2026-08-31:截断的快照
被拒(`snapshot missing hash but --skip-hash-check=false`),完好的通过。
**维护窗口打开之前就把这一步做掉**——3 点钟不是发现备份坏了的时间。

## 恢复(kubeadm 单成员 etcd,即 D1 的默认拓扑)

前提:头节点还能开机、或换了同名新机。全程约 10–15 分钟,期间 API server 不可用
——**已运行的客户负载不受影响**(kubelet 与容器不依赖 API server 存活)。

```bash
# 0. 选定快照并先校验完整性 —— 恢复一个坏文件比不恢复更糟
SNAP=/var/lib/arise/etcd-backups/etcd-<ts>.db
etcdutl snapshot status "$SNAP" -w table   # 头节点上;或用 etcd 镜像跑

# 1. 停 API server 与 etcd(kubeadm 静态 Pod:挪走 manifest 即停)
sudo mkdir -p /etc/kubernetes/manifests.stopped
sudo mv /etc/kubernetes/manifests/kube-apiserver.yaml \
        /etc/kubernetes/manifests/etcd.yaml \
        /etc/kubernetes/manifests.stopped/
# 等两个容器确实退出:
sudo crictl ps | grep -E 'etcd|kube-apiserver' || echo both-stopped

# 2. 把旧数据目录挪开（不要删——它是回退点）
sudo mv /var/lib/etcd /var/lib/etcd.pre-restore.$(date -u +%Y%m%dT%H%M%SZ)

# 3. 从快照重建数据目录（etcdutl 是官方推荐入口，etcdctl snapshot restore 已弃用）
sudo etcdutl snapshot restore "$SNAP" \
  --data-dir /var/lib/etcd \
  --name <etcd 成员名，见 etcd.yaml 里 --name> \
  --initial-cluster <成员名>=https://<头节点IP>:2380 \
  --initial-advertise-peer-urls https://<头节点IP>:2380
# 单成员集群这三个参数必须与 /etc/kubernetes/manifests.stopped/etcd.yaml
# 里的 --name / --initial-advertise-peer-urls 完全一致，否则成员身份对不上。

# 4. 恢复静态 Pod
sudo mv /etc/kubernetes/manifests.stopped/etcd.yaml \
        /etc/kubernetes/manifests.stopped/kube-apiserver.yaml \
        /etc/kubernetes/manifests/
# 等就绪:
until kubectl get --raw /readyz >/dev/null 2>&1; do sleep 3; done

# 5. 验证恢复到的世界是对的 —— 按本平台最在乎的三件事:
kubectl get nodeownership          # 每台节点的 owner/phase 与预期一致?
kubectl get ns -l arise.ai/tier=tenant
kubectl -n platform-system get cm capacity-controller-state -o name
# 然后跑完成门:
KUBE_CONTEXT=<ctx> ./scripts/verify-dgx.sh

# 6. 快照之后发生过的变更会丢(最多 6 小时窗口)。逐一核对:
#    - 恢复点之后创建/转换过的 NodeOwnership → 与 ops 记录对账后重放
#    - 恢复点之后入驻的租户 → 重跑 onboard 清单
#    - controller 的 transition state ConfigMap 若落后于真实世界,
#      controller 的 readback/幂等设计会按 transitionId 重新收敛——
#      这正是它存在的意义,不要手工改 owner 标签去"帮忙"。
```

### 回退

恢复后发现选错了快照:重复步骤 1-2-4,但第 2 步把 `/var/lib/etcd.pre-restore.<ts>`
挪回 `/var/lib/etcd`。旧目录在演练确认无误前**不删**。

## lab 演练记录

| 日期 | 内容 | 结果 |
|---|---|---|
| 2026-08-27 | 备份 Job 全链路(save→verify→rotate)在 kind control-plane 实跑 | ✓ 12MB/699 键/哈希校验通过 |
| 2026-08-27 | 发现 etcd 镜像无 /bin/sh,重构为三段式 | ✓ 已修 |
| 2026-08-27 | **完整恢复演练**(kind):新鲜快照 → 停 apiserver+etcd → 挪走旧数据目录 → `etcdutl snapshot restore`(经 `ctr run` 用 etcd 镜像,--name/--initial-cluster/--peer-urls 取自静态 Pod 清单)→ 恢复清单 → readyz | ✓ API 6 秒内恢复;NodeOwnership 逐条一致;13 命名空间一致;controller 状态 CM 在;随后全量矩阵回归 |
| 2026-08-27 | **步骤 6 的真实例证**:演练用的备份 Job 在拍快照后被删除;恢复后它**复活**了(快照早于删除),并重新跑了一次备份 | 证实"快照之后的变更会丢/回来"不是理论——恢复后必须按步骤 6 逐项对账,然后清掉复活的对象 |
| (待做) | 离节点副本自动化(**etcd 与台账两份**) | 等 D1/D2 定去处 |

> kind 差异:kind 的 etcd 数据在节点容器内 `/var/lib/etcd`,manifests 同路径;
> `docker exec b300-prelab-control-plane` 代替 ssh。演练踩到并已写进上文的坑:
> 节点上**没有 etcdutl 二进制**——用 etcd 镜像跑(kind 里 `ctr -n k8s.io run`,
> 真机上 `crictl`/`ctr` 同理,或直接 `docker run` etcd 镜像挂 /var/lib),
> 不要为此在头节点装额外软件。
