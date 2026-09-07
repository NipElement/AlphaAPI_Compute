# D1–D8 业务与基础设施决策

更新：2026-09-07。以下列出代码已经采用的默认形态及需要确认的交付条件。默认值不是业务批准，决定这些事项也不等于完成上线；全部验收条件见 [生产就绪清单](production-readiness.md)，部署步骤见 [DGX 手册](../runbooks/day0-setup.md)。

## D1 头节点与可用性

当前 DGX overlay 将控制面、网关、计量、监控及相应持久状态放在单头节点，`node-bootstrap.sh head` 要求 `/raid` 是实际挂载点。需要确定头节点地址、硬件、镜像仓库位置、独立备份位置、停机窗口和恢复目标。

头节点故障会中断网页、新提交、计量与本机监控，SSH 入口也受影响。已有 GPU 工作负载在控制面故障下的行为必须现场演练，不能据此承诺业务不中断。外部告警要能在本机监控失联时通知值班人。

配置入口：`infra/dgx/kubeadm-cluster-config.yaml` 两处 IP、`infra/dgx/node-bootstrap.sh` 和各组件存储清单。多头节点方案涉及 etcd、API 入口、存储和业务状态一致性；不能按“加一台、改一个 endpoint”估算。SQLite 网关仍需独立迁移设计。

## D2 存储与数据承诺

当前存储为节点 local-path，`arise-longterm` 使用 Retain；没有跨节点复制或逐 PVC 写入硬限制。需要确定实机 RAID/文件系统、容量隔离、数据备份归属、保留与清理规则，并使合同和控制台说明一致。

Retain 只控制删除回收行为，不证明故障容灾。RAID 或存储架构变化涉及数据迁移与恢复，不能当普通配置改动直接执行。配置与验收入口：`platform/overlays/dgx/storage.yaml`、[客户入驻](../runbooks/customer-onboarding.md)、[主机加固](../runbooks/host-hardening.md)。

## D3 网页身份与客户开户

首发默认管理员开户、自助改密、SQLite 持久化和一致备份。生产种子账号来自 Secret，普通客户通过控制台创建，不需要逐客户修改服务源码。需要确定账号交付、重置、离职撤销及客服验证流程。

SSO/MFA 尚未集成。接 OIDC/IdP 需要角色/租户映射、身份生命周期、回调与会话撤销设计，不能只增加一个回调路由。网页与 SSH 授权独立，离职需同时撤销。入口：[账号恢复](../runbooks/auth-recovery.md)、[客户入驻](../runbooks/customer-onboarding.md)。

## D4 公网入口和 SSH

已采用 Caddy 自带 ACME 的 Web 入口，以及内置租户 SSH 堡垒。Web 边缘默认不部署，SSH 公钥表默认为空、0 副本。需要填写控制台域名/邮箱、SSH 域名、组织公钥、镜像 digest，并明确上游防火墙、流量防护和可信代理拓扑。

生产 Web 使用头节点 80/443，SSH 使用 2222。Caddy 负责 TLS、大小与超时约束，登录限流在网关，不存在旧 Ingress 限流注解。新增 CDN/LB 时需同时复核代理头信任与源 IP 限流。

配置入口：`platform/overlays/dgx/edge/caddy.yaml`、`Caddyfile`、`platform/access/keys.yaml`；操作见 [公网入口](../runbooks/public-edge.md) 和 [租户接入](../runbooks/tenant-access.md)。

## D5 共享池与整机保障

现有实现支持 `owner: ARISE` 的共享池客户和 `owner: DIRECT` 的专属节点客户；由租户注册表、准入门、队列和控制器共同约束。需要决定共享池是否与内部研发共池、各租户配额和队列，以及合同保障的实际粒度。

DIRECT 节点预留是硬保障来源；队列权重不能承诺任意拓扑下的即时容量。新增 owner 类别涉及 CRD、控制器、准入、门户及测试，不是添加一个标签即可。配置入口：`platform/tenants.yaml` 和两套 `volcano-queues.yaml`，入驻使用 `scripts/onboard-tenant.py`。

## D6 价格、收款与欠费

当前计量记录资源占用，`billing/invoice.py` 按价格表、账期及预留输入生成可复算 CSV。没有在线扣款、余额或自动欠费处理。需要确定价格、账期、税务/账单系统、付款通道、预付或后付，以及冻结/停机的合同阈值。

冻结命令已存在，但天数和执行审批属于运营规则；不得把文档建议当作客户已接受的条款。入口：`billing/pricebook.yaml`、[计量事故与复核](../runbooks/incident-metering.md)、[租户冻结](../runbooks/tenant-freeze.md)。

## D7 隔离披露

当前是共享 Linux 内核的容器，使用命名空间、cgroup、seccomp、非 root、NetworkPolicy、RBAC 和准入策略；不提供 VM 级隔离。DIRECT 独占分配给该租户的物理节点；共享池仍共享节点与内核。存储的节点绑定和故障边界也应明确披露。

需要核对合同、安全说明及客户工作负载是否接受这些边界。改变隔离等级需要相应实现和重新验收，不能只修改对外文案。

## D8 IB 织物与租户 RDMA

需要与交付方确认 subnet manager、NIC 固件/驱动归属、实际接口名、整机与共享租户的 RDMA 暴露方式。仓库预置 Network Operator 安装 values；真实织物配置在 `infra/dgx/operators/nic-cluster-policy.yaml`，仍有必须填写的镜像和接口占位，HostDevice 示例尚未启用。

不能把“Operator 已安装”视为客户已拥有可用 RDMA，也不能把高权限硬件验收任务通过视为 restricted 租户容器已通过。确认配置后分别执行硬件 NCCL 验收和客户任务验收。入口：[硬件验收](../infra/dgx/acceptance/README.md)。
