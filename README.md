# ARISE B300 Prelab — Phase A (CPU 控制面预演)

单台 EC2 上的 B300 管理系统预演。基线文档：`ARISE-B300-PRELAB-DEPLOY-TEST-001 v1.0`。

> **先读这两份，再动手**
> - [`evidence/WAIVER-2026-08-11-001.md`](evidence/WAIVER-2026-08-11-001.md) — 本机 3 项 P0/P1 豁免与补偿控制
> - [`runbooks/gaps.md`](runbooks/gaps.md) — 哪些结论**不成立**、哪些用例必须保持 BLOCKED

## 这套东西验证什么、不验证什么

**验证**（CONTROL-PLANE）：资源契约、OWNER 状态机、合同门、调度与配额、准入策略、
幂等与不确定副作用恢复、证据链。

**不验证**（HARDWARE，留给 DGX 到货）：GPU / NVLink / NCCL / InfiniBand / RDMA /
BMC / NVMe / 真实 VAST 上架。CPU 沙箱不会被冒充为 GPU 验证环境。

## 运行环境事实（2026-08-11 实测）

| 项 | 值 |
|---|---|
| instance | `i-0015d25d494bae43f` c4.2xlarge, us-east-2b |
| OS | Ubuntu 24.04.3, x86_64, 8 vCPU / 14 GiB |
| 档位 | **STANDARD**（总内存 14 GiB < FULL 的 16 GiB 门槛） |
| 磁盘 | 990.9 GiB，已用 84%，可用 159 GiB |
| SSM | **未注册**（无 IAM 实例角色）→ SSH 是唯一管理通道 |
| 快照 | **无**（无 AWS 凭据）→ 无卷级回滚点 |
| 同机业务 | 735 GB MongoDB 数据（mongod 已停）+ 多人开发工作区 |

## 执行顺序

```bash
make validate      # L0 静态检查，不需要集群或 docker
make guard         # 断言保护路径未变 + 磁盘在停止线之上
make tools         # kind/kubectl/helm -> ~/.local/bin（checksum 校验，无 sudo）

# ↓ 唯一需要你亲自执行的 sudo 步骤，先读 runbooks/docker-install-review.md
./scripts/install-docker.sh plan
./scripts/install-docker.sh apply

make web           # 构建前端 SPA -> web/dist（web/ 源码改动后重跑）
make cluster       # kind 八节点 + dgx01..04 标签
make plugin        # 构建 fake-gpu device plugin 镜像并 kind load（需集群已建 + docker）
make deploy        # code(含上架 web/dist) + platform + volcano，然后自动 verify
make test          # Phase A 测试矩阵
make evidence      # 刷新证据包并 SHA-256 冻结
```

拆除：`make teardown`（仅删本 lab，**绝不** `docker system prune -a`）。
回滚：[`runbooks/rollback.md`](runbooks/rollback.md)。


## 界面 —— 单一入口

```bash
kubectl --context kind-b300-prelab -n platform-system port-forward svc/platform-gateway 8888:8080 &
```

**http://127.0.0.1:8888** —— 统一控制台，一个 **Vue 3 + Arco Design** 单页应用
（对标火山引擎机器学习平台），由 gateway **同源**伺服，左侧导航信息架构：

| 分组 | 模块 |
|---|---|
| 工作台 | 概览（GPU/节点/合同/告警总览）· 开发机 · 自定义任务（gang）· 在线服务 · 存储卷 · 镜像仓库 · 资源与队列 |
| 运维 | 机群管理（owner 状态机 + 基础设施池 + 转换门禁）· 监控（内嵌 Prometheus）· 告警 · 审计 · 用户管理 |

前端源码在 `web/`（`make web` 构建产物 `web/dist`，提交进 Git 作为构建物）。技术栈：
Vue 3 + Arco Design + vue-i18n（**按 key 取值**，切换语言即响应式重渲染，
无 DOM 文本替换那类 bug）+ vue-router（守卫：未登录→登录、越权 ops 路由拦截、401 自动踢回）+
TypeScript 严格模式 + Pinia。日/夜主题 + 中英切换。`scripts/i18n-check.py` 是 L0 静态门
（validate 8/8 + UI-03）：zh/en 两份 locale 的 key 集合必须完全一致，漏配即 FAIL。

架构：gateway 是**零权限**聚合层（不挂载 SA token，UI-03 断言），**同源**伺服 SPA
（`web/dist` 经 hostPath 挂到网关节点，因体积超 ConfigMap 上限；`make code` 用 docker cp 上架）
并做 HTTP 代理：`/papi→租户门户`（RBAC 限租户命名空间工作负载）、`/oapi→运维控制台`
（只写期望状态）、`/prom`/`/am`（GET 白名单）。同源故 cookie/SameSite 天然可用、无需 CORS。
CSP 已收紧（`script-src 'self'`，无 `unsafe-inline`——SPA 为外链 JS）。前端不弱化后端权限分离。
Grafana 降级为运维内部深查工具（保留部署，不再是产品界面）；监控内嵌在控制台里。

### 登录与角色

未登录时 SPA 的路由守卫（调 `/auth/me`）跳到登录视图，所有 API 返回 401/403。
授权在 **gateway 代理层**强制（不是靠前端隐藏；UI-03 断言 API 层不可绕过）：

| 账号 | 密码（可用环境变量覆盖） | 角色 | 能看到/能做 |
|---|---|---|---|
| `admin` | `arise-admin`（`GW_ADMIN_PASSWORD`） | admin | 全部模块 + 运维分组 + 用户管理 |
| `arise-dev` | `arise-dev`（`GW_ARISE_PASSWORD`） | user @ tenant-arise | 工作台，命名空间锁定 tenant-arise |
| `direct-cust` | `direct-cust`（`GW_DIRECT_PASSWORD`） | user @ tenant-direct | 工作台，命名空间锁定 tenant-direct |

普通用户访问他租户命名空间、`/oapi`、`/auth/users` 一律 403；admin 可在
「用户管理」里增删用户（不能删自己、不能删最后一个 admin）。密码存储为
PBKDF2-HMAC-SHA256（12 万轮），会话为 HttpOnly cookie，12 小时过期。
**预演环境限定**：用户与会话在 gateway 内存中（重启即清，回落到种子账号）；
实机产品阶段此层由 OIDC/SSO（企业 IdP）接管，角色映射保持同一模型。

### 控制台的一条架构铁律

运维后端**只写 NodeOwnership（期望状态），永远不写节点标签、污点或 cordon**；
租户后端只有租户命名空间的工作负载动词。门禁全部在 API server 准入层，
前端（含 gateway）是客户端不是策略引擎 —— UI-01/02/03 分别验证三层不可绕过。

## 目录

```
versions.env              单一版本入口（含 digest 固定）
scripts/
  guard.sh                ★ 每步前后强制断言：保护路径 + 磁盘绝对阈值
  preflight.sh            只读主机盘点 → 证据包
  install-tools.sh        kind/kubectl/helm，官方 checksum 校验
  install-docker.sh       ★ 唯一 sudo 步骤，plan/apply 双模式
  validate.sh             L0 静态门
  verify.sh               Stage 6 完成门 → verify.json
  hash-evidence.sh        证据冻结 + 脱敏扫描
kind/                     八节点拓扑 + 逻辑节点映射（单一真相源）
platform/base/            namespace/PSA/quota/LimitRange/RBAC/NetworkPolicy
platform/overlays/lab/    fake-gpu advertiser、准入策略、组件部署
platform/overlays/dgx/    到货后使用；不含任何模拟资源
web/                      ★ 前端 SPA（Vue 3 + Arco Design + vue-i18n + TS），dist 为构建物
services/                 ★ 后端服务（五个纯 stdlib Python + 一个 Go 编译组件）
  gateway/                  零权限聚合层：同源伺服 web/dist + HTTP 代理 + 会话/角色
  tenant-portal/            租户后端（工作负载客户端）
  ops-console/              运维后端（只写期望状态）
  capacity-controller/      NodeOwnership CRD + Capacity Controller 状态机
  vast-mock/                VAST Mock API（无凭据、无出网代码路径）
  fake-gpu-plugin/          Go：真 kubelet device plugin（唯一构建的镜像，见下方设计选择）
controller/               NodeOwnership CRD（lab 与 dgx overlay 共享；2026-08-16 从活集群恢复）
scripts/onboard-node.sh   机器注册/退役（NODE-01 回归）
dashboards/               Grafana 仪表盘 as code，UID 固定
tests/                    P0/P1 用例
runbooks/                 docker 审阅、回滚、AWS 只读+快照、缺口清单
evidence/<run_id>/        证据包，SHA-256 冻结
```

## 设计上几个刻意的选择

- **供应链尽量薄，且如实标注**。五个后端服务
  （gateway / tenant-portal / ops-console / capacity-controller / vast-mock）
  是纯标准库 Python，跑在同一个 digest 固定的 `python:3.12-slim` 上、代码由 ConfigMap 挂载，
  不构建镜像。**唯一的例外是 fake-gpu device plugin**（2026-08-16 起）：kubelet device
  plugin 契约必须编译，所以它是本仓库唯一构建的镜像——从 digest 固定的 Go 构建器
  （versions.env `GO_BUILDER_IMAGE`）产出 scratch 单静态二进制，`go list -m all` 记入
  evidence，经 `kind load` 交付（lab 无 registry，故 imagePullPolicy: Never + 记录 image ID
  作为 lab 级 digest 固定）。供应链 = 两个上游镜像 + Git 里可审阅的 Python 与 Go。
- **不变量写进 schema**。CRD 用 CEL 把"合同活跃时不得 SANITIZING"、
  "合同活跃时 observedOwner 不得为 ARISE"直接交给 API server 拒绝，
  而不是只写在文档里靠控制器自觉。
- **不确定即停**。外部调用结果未知时按 `transitionId` 查询而非重试；
  查询也不可用则进 QUARANTINE。卡住但安全 > 双重上架。
- **绝对阈值替代百分比**。990 GiB 盘上 80%/85% 不是有意义的度量；
  guard 用 free<60 GiB 告警、free<40 GiB 硬停，保护同盘的 735 GB 数据。
