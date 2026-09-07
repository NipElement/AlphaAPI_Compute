# 客户入驻：运维流程

从登记合同到客户通过自己的密钥连接开发机。生产上线条件以 [生产就绪清单](../docs/production-readiness.md) 为准；真实硬件、存储保障、生产域名和离机恢复须单独验收。

## 1. 登记租户和配额

确认客户的资源池、GPU/CPU/内存/存储配额、优先级、合同计费方式及联系人。向 `platform/tenants.yaml` 加入条目，例如：

```yaml
- namespace: tenant-acme
  short: acme
  display: Acme
  kind: customer
  queue: direct-customer
  owner: DIRECT
  priorities: [arise-contract-bound]
```

`gatewayAccount` 只描述原有两个实验种子账号；新客户不需要添加它或修改 `seed_users()`。

```bash
scripts/onboard-tenant.py tenant-acme > platform/base/tenant-acme.yaml
# 把生成文件加入 platform/base/kustomization.yaml，并按合同调整配额。
make validate
```

生成器默认使用 DGX 出向围栏；Lab 排练加 `--overlay lab`。它生成 Namespace、配额、LimitRange、网络隔离、ServiceAccount、租户 RBAC 和门户 RBAC。使用新队列时，还需将 Queue 对象加入两套 `volcano-queues.yaml` 并复核权益，校验器会检查队列确实存在。

**不再需要逐客户修改服务代码。** 网关和门户从挂载注册表加载租户/优先级，生产注册表缺失或损坏时拒绝启动。控制器、计量及运维控制台使用注册表和带租户标签的命名空间并集。`tests/test_onboarding.py` 实际演练第三个客户通过校验和获得持久化账号。

## 2. 应用配置

```bash
make dgx-platform DGX_KCTX=arise-dgx
make dgx-code DGX_KCTX=arise-dgx
make dgx-verify DGX_KCTX=arise-dgx
kubectl --context arise-dgx get ns tenant-acme --show-labels
kubectl --context arise-dgx auth can-i list secrets \
  --as=system:serviceaccount:tenant-acme:tenant-runner -n platform-system
```

最后一条应返回 `no`。`dgx-code` 更新注册表 ConfigMap 并重启相关服务。现有客户账号和已撤销会话都保存在 retained SQLite 中，正常更新不会丢失，无需重发密码；备份/恢复见 [auth-recovery.md](auth-recovery.md)。

## 3. 整机客户预留节点

在管理员机群页面选择目标客户，提交 DIRECT 所有权切换；或通过 NodeOwnership 清单指定 `tenant: tenant-acme`、新的 `transitionId` 和变更审批信息。修改已有对象时保留其当前 resourceVersion，避免覆盖并发操作。

等待 `DIRECT_ASSIGNED` 后，检查节点同时包含 `arise.ai/owner=DIRECT`、`arise.ai/tenant=tenant-acme` 及 DIRECT 污点。客户只能选择分配给自己的节点，其他 DIRECT 客户也不能使用它。按需客户登记 `owner: ARISE`，跳过整机预留。

## 4. 创建账号和登记 SSH 公钥

管理员控制台「用户管理」创建 `user` 角色，租户选 `tenant-acme`。随机初始密码至少 12 位，通过既定安全渠道交付；客户可自助修改密码，旧会话失效。

另收集客户的 **Ed25519 组织入口公钥**，登记到 `platform/access/keys.yaml`，按 [tenant-access.md](tenant-access.md) 启用堡垒并交付域名、端口、可信主机公钥与独立客户端。组织入口密钥与网页账号是两套授权，必须分别维护。

## 5. 客户首用验收

1. 客户登录控制台，创建开发机并填写自己的工作负载 SSH 公钥；需要保留的数据应挂载数据卷到 `/data`。
2. 运维交付该开发机的可信主机公钥，客户按 [接入文档](tenant-access.md) 配置 SSH Host，运行 `ssh` 和 `scp`。客户不持有管理员 kubeconfig。
3. 客户创建在线服务，通过客户端的 `service` 模式建立 loopback 隧道，检查实际 HTTP 响应。
4. 确认客户能访问自己资源、跨租户请求被拒绝、用量页开始记录。GPU 规格与性能需在实际硬件上验收。

## 6. 变更和退租

- 开发机公钥可在控制台更新；已有开发机 SSH 会话不会因此立即切断。入口密钥的撤销和紧急断连使用 [堡垒运维流程](tenant-access.md)。
- 欠费/违约：`scripts/tenant-freeze.sh tenant-acme freeze|stop|restore "原因"`；冻结提交与撤销登录/SSH 访问是不同操作，按合同分别执行。
- 出账：`billing/invoice.py` 读取台账与价格表；包机节点及预留区间需运营核对，避免错误的 reservation 输入。当前没有自动扣费或 CPU-only 收费 SKU。
- 退租：同时撤销网页账号、堡垒公钥及开发机密钥；节点按所有权流程经过 SANITIZING 后回池。删除 Namespace/PVC 前确认 retained 数据的导出、留存和处置，Retain 本身不是备份。
