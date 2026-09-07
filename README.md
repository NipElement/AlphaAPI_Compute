# ARISE Compute — B300 算力租赁平台

多租户开发机、训练任务、私有在线服务、存储与用量控制台，基于 Kubernetes、Volcano 和 NodeOwnership 所有权控制器。前端使用 Vue 3、TypeScript、Arco Design；网关和业务后端按权限分离。

当前验证环境是单台 EC2 上的 CPU kind 集群。控制面和浏览器验收已经完成，真实 GPU、生产网络、存储与故障恢复仍需验收。当前结论见 [生产就绪清单](docs/production-readiness.md)，最近代码与测试结果见 [9 月 7 日架构复查](docs/review-2026-09-07-architecture.md)。

## 文档入口

| 目的 | 文档 |
|---|---|
| 了解结构与底层约束 | [产品架构](docs/product-architecture.md) |
| 查看前端、运行检查、提交代码 | [开发指南](docs/development.md) |
| 上真实集群 | [DGX 部署顺序](runbooks/day0-setup.md)、[上线清单](docs/production-readiness.md)、[D1–D8 决策](docs/decisions-D1-D8.md) |
| 开通客户和交付连接 | [运维入驻流程](runbooks/customer-onboarding.md)、[客户快速上手](docs/customer/quickstart.md) |
| 配置公网与 SSH | [公网入口](runbooks/public-edge.md)、[租户接入](runbooks/tenant-access.md) |
| 查其他手册或历史记录 | [文档索引](docs/README.md) |

## 本地检查与预演

从仓库根目录运行。工具前置条件见 [开发指南](docs/development.md)。本机有共享业务数据，部署前先读 [主机豁免与保护要求](evidence/WAIVER-2026-08-11-001.md) 和 [实验环境限制](runbooks/gaps.md)。

```bash
make check         # 后端/配置门禁 + 前端类型检查与构建，不部署
make guard         # 保护路径与磁盘停止线
make tools         # 安装固定版本 kind/kubectl/helm 到 ~/.local/bin

# Docker 尚未安装时，先读 runbooks/docker-install-review.md，再由运维执行：
./scripts/install-docker.sh plan
./scripts/install-docker.sh apply

make new-run       # 新建部署证据目录，避免改写已封存记录
make cluster       # kind 八节点：控制面、4 个模拟 GPU、2 个 CPU、1 个存储节点
make plugin        # 构建 fake-gpu device plugin 并载入 kind
make devbox-image  # 构建 SSH 开发机镜像并载入 kind
make deploy        # 发布前面 make check 构建的前端及后端，然后自动 verify
make test          # 完整 lab 矩阵；自动创建新的测试 campaign
make evidence      # 采集当前 campaign 并封存；同时保留前面的部署 campaign
make evidence-verify
```

只需重建前端时运行 `make web`；已有 lab 更新前端用 `make web-assets`。`make validate` 单独跑后端和配置门禁。`make help` 列出全部入口。完整验收还包括独立浏览器与恢复测试，见 [测试说明](web/e2e/README.md)。

拆除仅使用 `make teardown`，不要对共享宿主机执行全局 Docker prune。回退见 [lab 回滚](runbooks/rollback.md)。

## 查看控制台

在运行 lab 的机器上保持以下命令运行：

```bash
kubectl --context kind-b300-prelab -n platform-system \
  port-forward --address 127.0.0.1 svc/platform-gateway 8888:8080
```

同机浏览器访问 **http://127.0.0.1:8888**。远程开发机可在自己电脑另开 SSH 隧道：

```bash
ssh -N -L 8888:127.0.0.1:8888 ubuntu@YOUR_DEV_HOST
```

随后在自己电脑访问同一个地址。Pod 发布后旧 port-forward 可能退出或失效，重新运行即可。开发源码热更新方式见 [开发指南](docs/development.md#前端开发)。

以下仅是 **lab 默认账号**，对应环境变量可覆盖：

| 账号 | Lab 密码 | 环境变量 | 权限 |
|---|---|---|---|
| `admin` | `arise-admin` | `GW_ADMIN_PASSWORD` | 全部模块与用户管理 |
| `arise-dev` | `arise-dev` | `GW_ARISE_PASSWORD` | `tenant-arise` 工作台 |
| `direct-cust` | `direct-cust` | `GW_DIRECT_PASSWORD` | `tenant-direct` 工作台 |

生产 overlay 启用 `GW_PUBLIC_MODE=true`：默认或过短密码、缺失或过短的会话密钥会使启动失败。生产凭据通过 `make dgx-gateway-secret` 创建，切流后使用 HTTPS 与 Secure cookie。账号和退出撤销持久化到 retained SQLite，网关只支持单写入者；恢复见 [账号恢复](runbooks/auth-recovery.md)。

客户 SSH 通过内置堡垒和组织公钥授权，在线服务可使用本机私有隧道。默认公钥表为空、堡垒 0 副本，启用需按 [租户接入](runbooks/tenant-access.md) 配置及验收。

## 目录与配置来源

| 路径 | 职责 |
|---|---|
| `web/` | 前端源码、组件、JSON 词库及 Chromium 验收 |
| `services/` | 网关、租户与运维 API、控制器、计量、备份、SSH 和 lab 模拟组件 |
| `controller/crd.yaml` | NodeOwnership schema 与不可绕过的状态约束 |
| `platform/tenants.yaml` | 租户、所有权类别、队列与优先级注册表 |
| `platform/base/` | 命名空间、租户配额、RBAC、网络隔离 |
| `platform/components/` | 两套 overlay 共用的组件清单，如账号备份 |
| `platform/overlays/lab/` | 模拟资源和 kind 部署 |
| `platform/overlays/dgx/` | 真实 GPU 部署、监控、持久化；`edge/` 显式启用公网入口 |
| `platform/access/` | 组织公钥注册表与隔离堡垒清单 |
| `platform/vendor/` | 固定版本的上游 Volcano / Calico 清单 |
| `infra/dgx/` | 节点初始化、kubeadm、Operator 和硬件验收 |
| `billing/` | 价格表与可复算 CSV 发票 |
| `kind/` | Lab 节点拓扑与逻辑映射 |
| `scripts/`、`tests/` | 部署编排、检查、恢复演练与业务矩阵 |
| `versions.env` | 平台工具与镜像版本；前端和 Go 依赖另有各自锁文件 |
| `docs/`、`runbooks/` | 当前设计、开发和操作手册；带日期的审查记录单独保留 |
| `evidence/RUN-*/` | 本机验收产物，脱敏并封存，Git 忽略；需单独归档 |

构建产物、运行数据库和私钥不入 Git。目录与忽略规则见 [开发指南](docs/development.md#文件与提交约定)。
