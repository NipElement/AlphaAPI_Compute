# 生产就绪清单

更新：2026-09-07。当前代码已在 CPU kind 集群完成控制面与浏览器验收；真实 GPU 和生产部署验收尚未完成。不能把实验环境通过理解为已经可以公开收费。

## 已验证的范围

| 范围 | 当前结果与依据 |
|---|---|
| 后端与配置 | `make check` 通过：认证、控制器、计量、备份、入驻、SSH、配置拒绝与词库回归；包含前端类型检查和构建 |
| 集群业务与隔离 | 完整 lab 矩阵 43/43；部署完成门 19/19；源码与运行组件核对通过 |
| 浏览器业务 | Chromium 39/39，覆盖管理员与客户流程、错误处理、权限、创建删除和连接配置 |
| 主题与布局 | 192/192 状态，80 次 axe 检查；浏览器异常和测试清理失败为 0 |
| 账号恢复 | 独立进程恢复测试 5/5；SQLite 一致备份和重启持久化已实测 |
| 公网入口预演 | Caddy 本地 CA/TLS、SSH 隔离容器及 kind 接入测试通过；尚未证明实际公网配置 |

具体日期、用例计数、证据路径和局限见 [架构复查](review-2026-09-07-architecture.md)、[公共界面复查](review-2026-09-07-public-ui.md) 和 [生产问题修复记录](review-2026-09-05.md)。表内结果属于当次代码与环境，不会自动覆盖后续更改；阶段性报告之间的数量差异保留原始含义。

## 上线前必须关闭

| 项目 | 当前边界 | 验收动作 |
|---|---|---|
| 真实 GPU 与网络 | Lab 使用模拟设备；不能证明驱动、NVLink、XDR、RDMA 或训练性能 | 在交付 DGX 上完成 Operator、健康指标、单机及跨机 NCCL 验收，保存硬件证据 |
| 存储 | local-path、Retain 和 PVC 配额不提供逐卷磁盘写入硬限制，也不等于容灾 | 实测生产数据盘、故障处置、容量限制，确定对客户承诺及独立备份 |
| 离机备份 | 账号、账本、etcd、SSH/ACME 身份的本机持久化不能抵御整机损坏 | 加密复制到独立故障域并实做恢复；账号恢复同时轮换会话签名密钥 |
| 公网 Web 与 SSH | 仓库提供入口实现；真实域名、证书签发、防火墙、客户密钥未替现场验收 | 按公网与租户接入手册，使用真实外部客户网络核验身份、登录、隔离与撤销 |
| 可用性与运维 | 头节点、SQLite 网关、账本和 SSH 入口有单点；增加 replicas 不能解决状态一致性 | 确认可接受停机窗口、RPO/RTO、外部告警与值班负责人，完成整机故障和持续负载演练 |
| 收费与入驻 | 支持管理员开户、用量与 CSV 发票；无自动扣款、SSO/MFA | 确认合同、价格、账期、存储及隔离披露，完成真实客户首用和退租验收 |
| 产品容量 | 用量查询仍为线性扫描，审计和分配明细有返回上限 | 结合预计客户数、历史数据量和并发定容量，必要时改分页/索引；补目标浏览器和设备验收 |
| GPU/Network Operator 供应链 | 2026-09-08 已收口：`infra/dgx/operators/operator-images.lock` 记录整套镜像的 digest（公开镜像匿名可解析，不需要 NGC 凭据），组件按 digest 钉进 values/NicClusterPolicy，`registry-mirror.sh` 镜像整套，`make validate` 16/16 校验，DGX-31/32 核对运行中 digest | 残余：两个 Operator 自身镜像与 NFD 子 chart 只能按标签拉取（Helm 模板无 digest 形式），靠 mirror 的 digest 相等校验 + DGX-31/32 事后核对；`driver.enabled` 翻转时必须同时钉 driver digest |
| 证据厚度 | 2026-09-08 已收口：14 个用例各自抓取判定所依据的观测（`tests/lib.sh` `capture`），空输出不计入 | 验收时看封存包 `results.json` 的 `verdict_without_observation` 是否为空 |
| 特权命名空间 | `platform-system`/`storage-system`/`access-system`/`edge-system` 因 hostPath/hostPort/hostNetwork 放宽 PSA 到 privileged；2026-09-08 起由两条 ValidatingAdmissionPolicy 收窄到"只有交付的控制器能建 pod、永不 privileged、access/edge 只剩各自那一项豁免" | 破窗（删 binding）是 cluster-admin 操作且进审计日志；DGX 上 `kubectl debug` 这些命名空间须用 `--profile=restricted` |

生产 VAST adapter 默认禁用，不属于本次可用产品能力。若计划对外提供，需单独实现和验收。详细技术边界见 [不可外推清单](../runbooks/gaps.md)。

## 复核记录

- [2026-09-08 对 `94a9661` 的复核](review-2026-09-08.md)：一条 critical（回收清理门 TOCTOU）、事件流从未落盘、DCGM 抓取未启用；已修并在 HEAD 上重跑矩阵。

## 操作入口

- [DGX 部署顺序](../runbooks/day0-setup.md)：从镜像、节点初始化到部署、硬件验收和切流。
- [公网入口与 TLS](../runbooks/public-edge.md)、[SSH 与私有服务](../runbooks/tenant-access.md)。
- [账号恢复](../runbooks/auth-recovery.md)、[计量事故与账本恢复](../runbooks/incident-metering.md)、[etcd 恢复](../runbooks/etcd-restore.md)。
- [客户入驻](../runbooks/customer-onboarding.md)、[业务与基础设施决策](decisions-D1-D8.md)。决策清单不是全部上线条件。

## 历史记录

8 月至 9 月 1 日的逐轮问题与判断归档在 [历史台账](history/production-readiness-2026-09-01.md)。其中已被推翻的结论、旧入口组件及旧用例数量只用于追溯；当前操作按本页链接的手册执行。
