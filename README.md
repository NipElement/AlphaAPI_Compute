# ARISE B300 Prelab — Phase A (CPU 控制面预演)

单台 EC2 上的 B300 管理系统预演。基线文档：`ARISE-B300-PRELAB-DEPLOY-TEST-001 v1.0`。

> **先读这两份，再动手**
> - [`evidence/WAIVER-2026-08-11-001.md`](evidence/WAIVER-2026-08-11-001.md) — 本机 3 项 P0/P1 豁免与补偿控制
> - [`runbooks/gaps.md`](runbooks/gaps.md) — 哪些结论**不成立**、哪些用例必须保持 BLOCKED
> - [`docs/production-readiness.md`](docs/production-readiness.md) — 生产就绪台账（按日期的状态更新）；[`docs/decisions-D1-D8.md`](docs/decisions-D1-D8.md) — **8 个待拍板决策的简报**（推荐/默认/解锁/改口代价）

## 这套东西验证什么、不验证什么

**验证**（CONTROL-PLANE）：资源契约、OWNER 状态机（含 MAINTENANCE 计划停机态,
MNT-01）、合同门、调度与配额、准入策略、幂等与不确定副作用恢复（含控制器中途
死亡演练,CHAOS-01）、证据链。

**不验证**（HARDWARE，留给 DGX 到货）：GPU / NVLink / NCCL / InfiniBand / RDMA /
BMC / NVMe / 真实 VAST 上架。CPU 沙箱不会被冒充为 GPU 验证环境。

## 运行环境事实（2026-08-11 实测）

| 项 | 值 |
|---|---|
| instance | `i-REDACTED-PRELAB-HOST` c4.2xlarge, us-east-2b |
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

前端源码在 `web/`（`make web` 产出 `web/dist`；**构建产物，不入 Git**，部署前先 `make web`）。技术栈：
Vue 3 + Arco Design + vue-i18n（**按 key 取值**，切换语言即响应式重渲染，
无 DOM 文本替换那类 bug）+ vue-router（守卫：未登录→登录、越权 ops 路由拦截、401 自动踢回）+
TypeScript 严格模式 + Pinia。日/夜主题 + 中英切换。`scripts/i18n-check.py` 是 L0 静态门
（validate 9/9 + UI-03）：zh/en 两份 locale 的 key 集合必须完全一致，漏配即 FAIL。

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
PBKDF2-HMAC-SHA256（12 万轮）。

**会话是无状态签名令牌**（HMAC-SHA256 over 用户 + 身份版本 + 过期 + 令牌 id），
装在 HttpOnly / SameSite=Lax cookie 里，12 小时过期。之所以无状态：旧的进程内
会话表让每次发布都等于把所有付费客户踢下线，也让第二个副本不可能存在。角色与租户
每次请求都从用户表重读，所以降权/删号下一个请求即生效；登出把令牌 id 记入撤销集
直到它自己过期。登录按**源 IP 与用户名分别限速并锁定**（默认 5 分钟内 8 次失败 →
锁 15 分钟），且限速在 PBKDF2 之前判定，锁定期间攻击者一分 CPU 也拿不到。跨站写
请求按 Origin/Host 比对拒绝（CSRF 第一层，SameSite 是第二层）。

`GW_PUBLIC_MODE=true`（dgx overlay 设置）把预演便利变成上线要求，**一律 fail
closed**：种子密码未设、等于 README 里的默认值、短于 12 位，或 `GW_SESSION_KEY`
缺失/短于 32 位 —— 进程拒绝启动。公网模式下 cookie 带 `Secure` 且用 `__Host-`
前缀（随 TLS 边缘一起打开，见 Day-0 步骤 12）。凭据从 Secret 注入，**永不入 Git**：
`make dgx-gateway-secret` 随机生成并只打印一次。

**仍是预演形态的部分**：用户表还在 gateway 进程内存里（种子账号由 Secret 决定，
因而重启/多副本一致；运行时新建的用户不持久）。这一层由 OIDC/SSO（企业 IdP，决策
D3）接管，角色映射保持同一模型；也正因为用户表还在进程内，dgx 的 gateway 副本数
刻意保持 1。

### 新增租户（入驻）

租户的**围栏与权益由命名空间标签驱动**,不再靠散落各处的枚举清单:
`arise.ai/tier=tenant` 决定准入策略与平台内网围栏是否覆盖它,
`arise.ai/queue=<queue>` 决定它能提交到哪个 Volcano 队列。
两者都只有管理员能改(任何租户身份对 namespaces 无任何动词,SEC-06)。

```bash
# 1. 在 platform/tenants.yaml 加一条目
# 2. 生成该租户的全部 k8s 对象
scripts/onboard-tenant.py tenant-acme > platform/base/tenant-acme.yaml   # 默认 dgx 围栏（拒绝全部私网段）；lab 排练加 --overlay lab
#    单独 apply 时用 kubectl apply --server-side（清单含 default ServiceAccount 的 automount 关闭，client-side apply 会与 SA 控制器竞争）
#    并把它加进 platform/base/kustomization.yaml；按合同复核配额数值
make validate     # §11 精确告诉你还有哪个消费者没接上
make deploy       # 或 make dgx-deploy（会重新生成 portal/gateway 读的注册表）
# 3. 在控制台「用户管理」里建该租户的账号 —— 凭据不进 Git
```

历史教训(2026-08-27 修复):平台内网围栏原先靠枚举租户命名空间名来排除租户,
**第三个租户不在名单里就会被放行**,可按 Pod IP 直连内部 API、绕过网关鉴权;
队列绑定同理,漏改 CEL 会把付费客户静默降级到 `default` 队列(无权重、无
reclaim 保护)。所以现在既改成标签驱动,又加了 L0 一致性门。

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
platform/tenants.yaml     ★ 租户注册表(单一真相源;标签驱动围栏与队列绑定)
platform/base/            namespace/PSA/quota/LimitRange/RBAC/NetworkPolicy
platform/overlays/lab/    fake-gpu advertiser、准入策略、组件部署
platform/overlays/dgx/    到货后使用；不含任何模拟资源（2026-08-26 起已含全部产品
                          工作负载 + 真存储；静态门 `make dgx-render`）
web/                      ★ 前端 SPA（Vue 3 + Arco Design + vue-i18n + TS），dist 为构建物
services/                 ★ 后端服务（五个纯 stdlib Python + 一个 Go 编译组件）
  gateway/                  零权限聚合层：同源伺服 web/dist + HTTP 代理 + 会话/角色
  tenant-portal/            租户后端（工作负载客户端）
  ops-console/              运维后端（只写期望状态）
  capacity-controller/      NodeOwnership CRD + Capacity Controller 状态机
  vast-mock/                VAST Mock API（无凭据、无出网代码路径）
  fake-gpu-plugin/          Go：真 kubelet device plugin（lab 构建镜像，见下方设计选择）
  web/                      dgx 的 SPA 内容镜像（web/dist + digest 固定 busybox；`make web-image`）
controller/               NodeOwnership CRD（lab 与 dgx overlay 共享；2026-08-16 从活集群恢复）
infra/dgx/                ★ Day-0 主机层：kubeadm 集群配置、审计策略、节点引导脚本、
                            GPU/Network/cert-manager Operator values（⟪DECIDE⟫ 留空）
platform/overlays/dgx/edge/  公网边缘（独立 kustomization，等 D4 再 apply）
services/devbox/          ★ 客户可 SSH 的开发机镜像（非 root sshd，restricted PSA）
services/metering/        ★ 分配台账：append-only 哈希链（可核查：改/删/换序都能定位），每张发票的来源；
                          防篡改还需外部锚点——链头由 Prometheus 独立留存（arise-billing 告警组盯着它）
billing/                  价格本（唯一写价格的地方）+ 确定性 CSV 发票
docs/customer/            客户快速上手
docs/decisions-D1-D8.md   ★ 拍板简报：上线前唯一还卡着的 8 个决策
scripts/onboard-node.sh   机器注册/退役（NODE-01 回归）
scripts/verify-dgx.sh     ★ 硬件完成门（verify.sh 的镜像：断真 GPU、零模拟）
scripts/onboard-tenant.py 租户入驻：按注册表生成全部 k8s 对象
scripts/tenant-check.py   L0 门：注册表与全部消费者一致（validate §11）
scripts/tenants-json.py   注册表 -> platform-tenants ConfigMap（portal/gateway 读取）
dashboards/               Grafana 仪表盘 as code，UID 固定
tests/                    P0/P1 用例
runbooks/                 客户入驻（customer-onboarding.md）、docker 审阅、回滚、etcd 恢复、四本事故 runbook、升级回滚、
                            租户冻结、Day-0 切流、主机加固基线、缺口清单
evidence/<run_id>/        证据包，SHA-256 冻结
```

## 到货那天（Day-0)

测试矩阵与完成门**直接打真机**(`OVERLAY=dgx`:申请 `nvidia.com/gpu`;23/41 条用例可打真机,其余 18 条 lab 专属用例 SKIPPED 并列名;市场(VAST)流在硬件上零验证,直到有生产 adapter——这是 DGX-12/13 的含义)。
顺序即依赖顺序——每一步都以上一步为前提,三处文档(这里、台账 §3、kubeadm 头注)按同一序号:

```bash
# 0. 管理机:构建自有镜像,起私有 registry(D1 决定它落在头节点还是别处),全部 pinned 镜像进 mirror
make web-image && make devbox-image                       # arise/web、arise/devbox(本机 docker)
REGISTRY=<registry:port> ./scripts/registry-mirror.sh     # digest 逐个相等否则失败;打印 arise/* 的新 digest
#    → 把打印的 digest 写进 platform/overlays/dgx/kustomization.yaml(images:)与 tenant-portal.yaml(DEVBOX_IMAGE)
#    → 明文 HTTP registry 需要 docker daemon 的 insecure-registries(rehearsal 用 127.0.0.1:5001 天然豁免)
# 1. 每台节点(root;sudo 会丢环境变量,所以用 env 显式传):
sudo env KUBE_VERSION=v1.36.2 REGISTRY_MIRROR=<registry:port> infra/dgx/node-bootstrap.sh head   # 头节点(/raid 必须已挂载)
sudo env KUBE_VERSION=v1.36.2 REGISTRY_MIRROR=<registry:port> infra/dgx/node-bootstrap.sh gpu    # 4 台 DGX
# 2. 头节点:填 kubeadm-cluster-config.yaml 的 REPLACE_WITH_HEAD_NODE_IP(render 门会拒绝残留占位),然后
sudo install -m 0644 infra/dgx/audit-policy.yaml /etc/kubernetes/audit-policy.yaml
sudo kubeadm init --config infra/dgx/kubeadm-cluster-config.yaml
export DGX_KCTX=<你的 dgx kube context>
make dgx-cni           # 3. vendored Calico(digest 固定、pod CIDR 预设);此前节点 NotReady
#    4. 4 台 DGX:粘贴 kubeadm join
make dgx-approve-csrs  # 4b. 批准 kubelet serving 证书 CSR(否则 logs/exec 与 DGX-28 报 TLS 错)
make dgx-render        # 5. 静态门:清单本身是否可以安全 apply(含 kubeadm 占位符/版本/CIDR 交叉检查)
make dgx-platform      # 6. CRD + 命名空间 + 策略(先于凭据:Secret 需要 platform-system 存在)
make dgx-onboard       # 7. onboard-node.sh gpu ×4:node-id/pair/role 标签 + NodeOwnership(DGX-02..05、node_for 都靠它)
                       #    默认 DGX_HOSTS="dgx01 dgx02 dgx03 dgx04";主机名不同时 DGX_HOSTS="h1 h2 h3 h4"
make dgx-gateway-secret # 8. 随机生成网关凭据,只打印一次
make dgx-deploy        # 9. render 门 -> 哨兵镜像检查 -> overlay -> 代码/注册表 ConfigMap -> vendored Volcano(控制面放头节点)
# 10. GPU Operator / Network Operator:helm,values 在 infra/dgx/operators/(填 ⟪DECIDE⟫;driver.enabled 看实机);
#     先 kubectl -n gpu-operator create configmap arise-dcgm-metrics --from-file=dcgm-metrics.csv=infra/dgx/operators/dcgm-metrics.csv;
#     Network Operator 的真实配置是 infra/dgx/operators/nic-cluster-policy.yaml(NicClusterPolicy CR),operator 起来后再 apply
#     直到这一步之前 nvidia.com/gpu=0,dgx-verify 的 DGX-03/04/05 必红——所以 verify 放在它后面
make dgx-verify        # 11. DGX-01..32 完成门(真 GPU 在、模拟资源为零、门禁齐备、CSR 已批、围栏实测、Volcano/Calico 就绪)
make dgx-test          # 11b. OVERLAY=dgx 矩阵:41 条里 23 条打真机;18 条只在 lab 有意义(vast-mock 市场流、假广播器故障注入、
                       #      lab 指标、CPU 池、grafana)SKIPPED 并在 results.json 列名——它们不是对真机的断言,别把 SKIPPED 读成 PASS
make dgx-hw-accept     # 12/13. 硬件验收:NVLink 单节点 + XDR 双节点 all-reduce,按 infra/dgx/acceptance/hw-thresholds.env 评分
make dgx-alert-receiver WEBHOOK_URL=https://...   # 14. 接真实 pager(DGX-22 从 WARN 变 PASS)
# 15. 切流(D4 拍板、cert-manager 已按 runbooks/day0-cutover.md §1 装好之后):
make dgx-edge          #     edge/ 与网关 GW_TRUST_PROXY/GW_COOKIE_SECURE 一步同翻(DGX-26);回退 make dgx-edge-off
```

之所以能这样,是因为断言里**没有任何物理节点名**:逻辑 id 经 `node_for`
由标签解析(`arise.ai/node-id` / `arise.ai/aux-name`)。这不只是可移植性——
硬编码的名字在别的集群上不存在时 kubectl 返回空串,"空==空"的断言会**假通过**。
`make validate` 的 §12 守住这条线。

## 设计上几个刻意的选择

- **供应链尽量薄，且如实标注**。五个后端服务
  （gateway / tenant-portal / ops-console / capacity-controller / vast-mock）
  是纯标准库 Python，跑在同一个 digest 固定的 `python:3.12-slim` 上、代码由 ConfigMap 挂载，
  不构建镜像。**本仓库只构建两个镜像**：fake-gpu device plugin（2026-08-16 起；kubelet
  device plugin 契约必须编译——digest 固定的 Go 构建器产出 scratch 单静态二进制，
  `go list -m all` 记入 evidence，经 `kind load` 交付，仅 lab）与 arise/web
  （2026-08-26 起；dgx 的 SPA 内容镜像 = web/dist + digest 固定 busybox，`make web-image`
  构建、Day-0 推 registry 交付，取代 lab 的 docker-cp）。上游镜像共四个、全部 digest 固定
  （python、Go builder、busybox、local-path-provisioner，见 versions.env）+ Git 里可审阅的
  Python 与 Go。
- **不变量写进 schema**。CRD 用 CEL 把"合同活跃时不得 SANITIZING"、
  "合同活跃时 observedOwner 不得为 ARISE"直接交给 API server 拒绝，
  而不是只写在文档里靠控制器自觉。
- **不确定即停**。外部调用结果未知时按 `transitionId` 查询而非重试；
  查询也不可用则进 QUARANTINE。卡住但安全 > 双重上架。
- **绝对阈值替代百分比**。990 GiB 盘上 80%/85% 不是有意义的度量；
  guard 用 free<60 GiB 告警、free<40 GiB 硬停，保护同盘的 735 GB 数据。
