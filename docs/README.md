# 文档索引

当前部署与上线判断从 [生产就绪清单](production-readiness.md) 开始。本文档中的相对链接按所在文件解析；命令和代码块内的项目路径均相对于仓库根目录。

## 当前设计与开发

- [产品架构](product-architecture.md)：组件边界、所有权、调度、存储、身份和计量。
- [开发指南](development.md)：工具、前端预览、验证、产物和提交约定。
- [D1–D8 决策](decisions-D1-D8.md)：业务与基础设施待确认项，建议不等于已获批准。
- [浏览器验收](../web/e2e/README.md)：业务、主题、响应式和无障碍检查的执行及边界。

## 交付与运维

| 场景 | 手册 |
|---|---|
| 初始化真实集群 | [DGX 部署顺序](../runbooks/day0-setup.md)、[硬件验收](../infra/dgx/acceptance/README.md)、[机器注册](../runbooks/machine-registration.md) |
| 公网与客户接入 | [TLS 入口](../runbooks/public-edge.md)、[SSH 堡垒](../runbooks/tenant-access.md)、[入驻](../runbooks/customer-onboarding.md)、[客户快速上手](customer/quickstart.md) |
| 账号、账本与控制面恢复 | [账号恢复](../runbooks/auth-recovery.md)、[计量事故](../runbooks/incident-metering.md)、[etcd 恢复](../runbooks/etcd-restore.md) |
| 故障处理 | [节点下线](../runbooks/incident-node-down.md)、[GPU 故障](../runbooks/incident-gpu-fault.md)、[磁盘满](../runbooks/incident-disk-full.md)、[隔离恢复](../runbooks/incident-quarantine-recovery.md) |
| 变更与退租 | [升级回滚](../runbooks/upgrade-rollback.md)、[租户冻结](../runbooks/tenant-freeze.md)、[lab 回滚](../runbooks/rollback.md) |
| 主机与实验环境 | [主机加固](../runbooks/host-hardening.md)、[Docker 安装审阅](../runbooks/docker-install-review.md)、[AWS 与快照](../runbooks/aws-readonly-and-snapshot.md)、[技术边界](../runbooks/gaps.md) |

## 审查与历史

这些文件记录各次改动和当时验收，数量与结论不自动跟随后续代码更新。新环境需重新执行验收；本机 `evidence/RUN-*` 不随 Git 分发，证据应通过独立归档交付。

- [2026-09-07 代码与结构](review-2026-09-07-architecture.md)。
- [2026-09-07 主题、字号与监控](review-2026-09-07-public-ui.md)。
- [2026-09-07 控制台交互](review-2026-09-07-console.md)。
- [2026-09-06 前端](review-2026-09-06-frontend.md)。
- [2026-09-05 生产问题修复](review-2026-09-05.md)。
- [2026-08 至 09-01 历史生产台账](history/production-readiness-2026-09-01.md)、[2026-08 安全记录](../runbooks/security-audit-2026-08.md)。

维护时直接修订当前指南，只有需要保存复现、决策依据和验收证据的记录才新增日期报告。过时流程应归档并链接替代入口，不继续复制到 README、架构和多个 runbook。
