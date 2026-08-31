# D1–D8 决策简报(拍板用)

> 2026-08-27。台账(`docs/production-readiness.md` §0)里 8 个决策是**唯一**还卡着上线的东西:
> 工程侧能预建的已经预建并在 kind 上排练过(40 用例矩阵、L0 validate、dgx render 门、verify-dgx DGX-01..28)。
> 每个决策给出:**推荐**、**不拍板时的默认**(代码里现在就是这个)、**它解锁什么**、**改口的代价**。
> 目标是让拍板变成"选一行",而不是"设计一套"。

---

## D1 头节点方案

**问题**:控制面 / etcd / gateway / 监控 / metering / registry 放哪。4 台 DGX 全部要卖,不能有一台常驻基础设施。

**推荐**:**1 台普通 x86 服务器做头节点**(≥16 核、≥64 GB、两块盘:OS 盘 + **独立 NVMe 数据盘挂到 `/raid`**),先单头节点;第二台头节点是 Phase B(etcd 三副本)而不是 Day-0 前置。
理由:整个 dgx overlay 已按"所有平台组件 `nodeSelector: control-plane` + 容忍其污点"写死;etcd 备份**与恢复**都已演练(`make dgx-restore-drill`,2026-08-31 在 lab 上实跑通过,并用截断快照证伪);单点的风险由 etcd CronJob 备份 + 台账 CronJob 备份 + `runbooks/etcd-restore.md` 兜底。

**单头节点真正的代价(2026-08-31 核对全部 7 个组件:capacity-controller / metering / platform-gateway / tenant-portal / ops-console / prometheus / alertmanager,全部 `replicas: 1` 且钉在头节点)**:

| 头节点挂掉时 | 结果 |
|---|---|
| 已经在跑的 GPU 任务 | **继续跑**——kubelet 不依赖 API server 维持已有 Pod。⚠ 这一行是 Kubernetes 语义,**没有在本机实测**(实测要停 API server,属于到货后的演练项);其余各行都是 2026-08-31 在 lab 上核对过的部署事实 |
| 登录 / 控制台 / 提交新任务 / SSH 开发机新建 | **全停**,客户看到的是连不上 |
| 计量 | **停止记录**;停机期间的用量不进台账 = **少计费**,`MeteringDown` 会响,按 `runbooks/incident-metering.md` 人工补 |
| 告警 | Prometheus/Alertmanager 也在头节点 —— **告警自己也停了**,这就是为什么 pager 的接收端必须在集群外 |
| 恢复路径 | 换机 + `etcd-restore.md`;备份在**头节点自己的盘上**,离机那一段仍未做(本决策的另一半) |

第二台头节点买不买,权衡的就是上表第 2/4/5 行 —— 不是"客户任务会不会挂"。

**不拍板的默认**:代码就是"单头节点"形态。`node-bootstrap.sh head` 现在**要求 `/raid` 是挂载点**(否则 metering/prometheus/alertmanager 的 PVC 永远 Pending,DGX-09 变红)。
**解锁**:`kubeadm-cluster-config.yaml` 的两处 `REPLACE_WITH_HEAD_NODE_IP`;Day-0 步骤 4 可执行。
**改口代价**:换成"两台头节点 / 外部 LB"→ 只改 `controlPlaneEndpoint` 与 etcd 拓扑,overlay 不用动。

## D2 存储架构与承诺

**问题**:每节点 30.72 TB 本地 NVMe 卖给客户——RAID 级别、数据丢失责任怎么写。

**推荐**:**DGX OS 默认的 RAID-0 `/raid` 保持不变,以"高性能临时/工作数据,不承诺持久"对外披露**;`arise-longterm`(Retain)只承诺"我们不主动删",不承诺跨节点故障存活。**不做**跨节点复制(没有第二套存储,做了就是假承诺)。备份是客户自己的责任,offer 页写明。
理由:local-path + Retain 语义已实现且被 FLV-03 / ACC-01 证明;任何"复制"承诺都需要不存在的第二套介质。

**不拍板的默认**:代码就是这样(两种存储类、`.raid-verified` 哨兵、Retain)。
**解锁**:offer 页与合同措辞;`runbooks/host-hardening.md` 的"磁盘加密"一行(推荐:**不做 dm-crypt**,性能与责任披露一起写清楚)。
**改口代价**:换 RAID 级别只是重做 `/raid` 与重种哨兵;换外部存储是全新工作流(不建议 Phase A)。

## D3 客户身份

**问题**:自建认证升级 vs 接 OIDC/IdP;注册、改密、MFA 谁提供。

**推荐**:**Phase A 保持网关自带账号(三个种子 + 控制台建账号),客户数 ≤5 时够用;Phase B 接 OIDC(Keycloak 自托管,放头节点)**。
理由:网关认证已做到 fail-closed、HMAC 无状态会话、限速、CSRF、种子账号禁删(轮换密码即撤销)。它的真实短板只有两条——运行时建的账号**不持久**(重启即失)、logout **进程内**(重启后 token 在 TTL 内复活)——这两条正是 IdP 该解决的,不值得自研。

**不拍板的默认**:种子账号 + 控制台建账号;客户凭据由运维在 `make dgx-gateway-secret` 打印时交付。
**解锁**:入驻流程的"账号"一步;客户自助改密/MFA。
**改口代价**:网关加一个 OIDC 回调路由,会话格式不变。

## D4 公网边缘

**问题**:域名、LB/边缘节点、DDoS 姿态、TLS 在哪终结。

**推荐**:**头节点 hostNetwork 的 ingress-nginx + cert-manager(Let's Encrypt HTTP-01)+ 只放行 80/443 的上游防火墙**;域名 `console.<公司域>`;不上 CDN/WAF(Phase A 客户数少,不值)。
理由:`platform/overlays/dgx/edge/` 已按此写好(镜像 digest 固定、限速注解、HSTS),`make dgx-edge` 一步把边缘和网关两个开关同翻(DGX-26 断言配对),`runbooks/day0-cutover.md` 已写完切流与回退。

**不拍板的默认**:边缘**不部署**(`dgx-deploy` 故意不含 edge),网关只在头节点 hostPort 内网可达——安全但不可售。
**解锁**:`edge/issuer-and-ingress.yaml` 里 3 个 `REPLACE_WITH_*`(FQDN、运维邮箱)。
**改口代价**:换成云 LB → 只改 `ingress-nginx.yaml` 的 Service 形态。

## D5 按需池

**问题**:$9.57/卡·时的按分钟客户跑在哪个 owner 池、隔离粒度。

**推荐**:**按需客户 = 一个 `kind: customer` 租户命名空间,跑在 ARISE 池(`arise.ai/owner=ARISE`),粒度为"命名空间 + 单卡整数配额"**;不做单卡级节点隔离(共享内核下本来也隔不住,见 D7)。整机客户走 DIRECT 预留(已实现、DIR-01/02 证明)。
理由:计量已按 pod 驻留记账、单位 gpu-hour、客户/内部两套价目;入驻生成器 `onboard-tenant.py` 一条命令出全部对象;队列/优先级绑定已标签化。**唯一**要定的是"按需客户是否与内部研发共池"——推荐共池(池子只有 4 台,分池等于浪费),用 Volcano 权重 + reclaim 保护客户队列。

**不拍板的默认**:注册表里 `owner: ARISE` 的 customer 租户就是这个形态。
**解锁**:第一个按需客户的入驻;`billing/pricebook.yaml` 的 `tenant_kinds` 已就绪。
**改口代价**:分池 → 给节点打第三个 owner 值 + 一条准入映射,控制器状态机不变。

## D6 收款与记账

**问题**:支付通道、记账系统、预付 vs 后付、欠费冻结。

**推荐**:**Phase A 月末后付 + 人工开票(`billing/invoice.py` 出 CSV,链校验、开区间、专属节点摊分都已实现)+ Stripe 收款链接**;预付/自动扣款是 Phase B。欠费冻结用已有的 `scripts/tenant-freeze.sh freeze|stop|restore`(SUS-01 证明),**冻结阈值:逾期 7 天 freeze、14 天 stop**——写进合同。
理由:计量台账是 append-only 哈希链、可离线复算;发票确定性(同输入同字节);差的只是"钱怎么进来"。

**不拍板的默认**:台账照记、发票可出、没人收钱、没人冻结。
**解锁**:合同的付款条款;`runbooks/tenant-freeze.md` 里的天数。
**改口代价**:换记账系统 = 把 CSV 喂给它,台账不动。

## D7 隔离披露

**问题**:共享内核、无 VM——对客户怎么诚实披露。

**推荐**:披露原文(建议直接放 offer/合同):
> "工作负载以容器运行于共享 Linux 内核之上,隔离手段为命名空间、cgroup、seccomp、只读根文件系统、非 root、NetworkPolicy 默认拒绝与准入策略;**不提供虚拟机级隔离**。整机预留(DIRECT)客户独占物理节点;按需客户与其他按需客户共享节点但不共享 GPU。本地 NVMe 数据不承诺跨节点故障存活。"
理由:这就是 PSA restricted + VAP + NetworkPolicy 的真实边界(SEC-02/05/06、SVC-01 证明),说多了是假的,说少了是风险。

**不拍板的默认**:代码边界如上;文案缺失。
**解锁**:合同附件、offer 页安全段。
**改口代价**:无(纯文案)。

## D8 IB 织物管理

**问题**:subnet manager 跑在交换机还是主机 opensm;谁负责。

**推荐**:**交换机托管 SM(NVIDIA Quantum 自带)**,主机侧不跑 opensm;`network-operator-values.yaml` 里 RDMA 路径先选 **HostDevice**(最少活动件),IPoIB/SR-IOV 仅当客户明确要多租户 IB 隔离时再开。责任:交换机由到货时的集成商/厂商配置,我们只做 `ibstat` 链路速率与双节点 NCCL all-reduce 验收(HW-xx)。
理由:4 节点单交换机拓扑没有理由自己养 SM;values 文件里 ⟪DECIDE-D8⟫/⟪DECIDE-WITH-VENDOR⟫ 已把选项枚举好。

**不拍板的默认**:Network Operator 的 RDMA/IPoIB 全部 `enabled: false`——集群能跑,但双节点 XDR 验收无法做、跨节点训练走以太网。
**解锁**:`infra/dgx/operators/network-operator-values.yaml` 的 5 个 ⟪DECIDE⟫;HW 验收里的织物项。
**改口代价**:主机 opensm → 加一个 DaemonSet + 一条"只在一台上跑"的约束。

---

## 如果今天只拍一个

**D1**。它卡的是 Day-0 步骤 4(建集群)——其它七个都可以在集群跑起来之后再落;D1 不定,硬件到货那天什么都开始不了。
第二个是 **D4**(域名要 DNS 生效时间,证书要 80 端口可达)。

## 填完之后该做的(全部已有工具)

| 决策 | 改哪里 | 谁校验 |
|---|---|---|
| D1 | `infra/dgx/kubeadm-cluster-config.yaml` 两处 IP;头节点 `/raid` 挂载 | `node-bootstrap.sh head` 拒绝无 `/raid`;DGX-09 |
| D2 | offer/合同文案;`runbooks/host-hardening.md` 磁盘加密行 | — |
| D3 | (Phase A 无改动) | UI-03 |
| D4 | `platform/overlays/dgx/edge/issuer-and-ingress.yaml` 3 处 | `make dgx-edge` 拒绝残留占位;DGX-26 |
| D5 | `platform/tenants.yaml` 加客户 → `onboard-tenant.py` | `make validate` §11;DGX-25 |
| D6 | `runbooks/tenant-freeze.md` 天数;合同 | SUS-01 |
| D7 | 合同附件 | — |
| D8 | `infra/dgx/operators/network-operator-values.yaml` | HW 验收 |
