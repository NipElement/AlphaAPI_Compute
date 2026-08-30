# 客户入驻(端到端,运维视角)

> 从"合同签了"到"客户第一次 `ssh` 进去"。每一步都有对应的门或探针;
> 全部命令在 lab 上排练过(SEC-05/SCH-12/UI-02/UI-03/DIR-01/SUS-01)。
> 前置:集群已过 `make dgx-verify`(DGX-01..30 全绿或仅 DGX-22/23/24 WARN)。

## 0. 决定三件事(合同里写明)

| 项 | 选项 | 落在哪 |
|---|---|---|
| 产品形态 | 整机预留(DIRECT,$52,750/月)/ 按需(ARISE 共享池,$9.57/卡·时) | `platform/tenants.yaml` 的 `owner: DIRECT` 或 `ARISE` |
| 配额 | GPU 张数、vCPU、内存、存储 | `onboard-tenant.py --profile customer` 的默认值 → **按合同改** |
| 联系人/账号 | 网关登录名、SSH 公钥、账单邮箱 | 网关用户(不进 Git)、开发机(客户自己粘贴公钥)|

## 1. 注册租户(Git)

```bash
# platform/tenants.yaml 加一条(namespace/short/display/kind: customer/queue/owner/priorities/gatewayAccount)
scripts/onboard-tenant.py tenant-acme > platform/base/tenant-acme.yaml   # dgx 围栏默认;lab 排练 --overlay lab
#   把它加进 platform/base/kustomization.yaml;按合同改 ResourceQuota / LimitRange 数值
make validate                # §11 会精确列出还没接上的消费者(队列、策略、RBAC……)
git commit                   # 入驻是一次代码变更,有审阅、有回滚
```

## 2. 应用到集群

```bash
make dgx-platform DGX_KCTX=<ctx>     # server-side apply(清单含 default SA 的 automount 关闭)
make dgx-code DGX_KCTX=<ctx>         # 重新渲染 platform-tenants ConfigMap;控制器按 mtime 热重载,门户/网关重启后读到
kubectl --context <ctx> -n platform-system rollout restart deploy/tenant-portal deploy/platform-gateway
make dgx-verify DGX_KCTX=<ctx>       # DGX-25 断言新命名空间带硬件出向围栏;DGX-10 策略齐备
```
验证:`kubectl --context <ctx> get ns tenant-acme --show-labels` 有 `arise.ai/tier=tenant`、`arise.ai/queue=<queue>`;
`kubectl auth can-i list secrets --as=system:serviceaccount:tenant-acme:tenant-runner -n platform-system` → **no**。

## 3. 整机客户:预留节点(DIRECT)

```bash
cat <<Y | kubectl --context <ctx> apply -f -
apiVersion: infrastructure.arise.ai/v1alpha1
kind: NodeOwnership
metadata: { name: dgx03 }
spec:
  desiredOwner: DIRECT
  tenant: tenant-acme          # 为谁预留;多个客户时控制器拒绝猜测
  transitionId: acme-2026-09-01
  pair: "03-04"
  approvedBy: <你>
  notBefore: "2026-09-01T00:00:00Z"   # 可选:合同生效时刻;等待期间稳态纠偏照常
Y
kubectl --context <ctx> get nodeownership dgx03 -w    # PENDING -> DRAINING -> DIRECT_ASSIGNED
```
到 `DIRECT_ASSIGNED` 时节点带 `arise.ai/owner=DIRECT` 标签与 `arise.ai/direct-owned` 污点,只有该租户的 pod 能落上去(DIR-01)。
计费:预留区间按自然月天数摊分,发票用 `--dedicated-nodes dgx03 --dedicated-from <时刻>`。
**按需客户跳过这一步**(`owner: ARISE`,落共享池,按 GPU·时计)。

## 4. 网关账号(凭据不进 Git)

控制台「用户管理」→ 新建:用户名 = 注册表里的 `gatewayAccount`,角色 `user`,租户 `tenant-acme`,
随机 ≥ 12 位初始密码,**通过既定的安全渠道交付**。客户登录后可在用户菜单「修改密码」自助更换
(所有会话失效)。
> 现状(D3):运行时创建的账号保存在网关进程内,**网关重启会丢失**——重启后按本步重建并重新交付;
> 注意 **`make dgx-code` / `make code` 现在会重启网关**(2026-08-30 起:ConfigMap 里的代码不重启就不生效),
> 所以"改注册表 → dgx-code"这条入驻路径本身就会清掉运行时账号。顺序建议:先 dgx-code,再建账号并交付;
> 或在 dgx-code 前 `GET /auth/users` 导出一份名单(密码无法导出,只能重发)。
> 三个种子账号(admin / arise-dev / direct-cust)由 Secret 派生,不受影响。上 IdP 前,每次 `rollout restart platform-gateway` 前先导出用户列表(`GET /auth/users`)。

## 5. 客户第一次进来

1. 客户登录 → 「开发机」→ 粘贴 SSH 公钥 → 创建(规格按合同;GPU 规格需要第 3 步的预留节点,否则 Pending)。
2. 列表的 **SSH 端点** 列是集群内地址 `dev@<name>-ssh.tenant-acme.svc:22`。公网接入(D4 前):
   ```bash
   kubectl --context <ctx> -n tenant-acme port-forward svc/<name>-ssh <本地端口>:22   # 跳板机上常驻,或
   ```
   把 `<跳板机>:<端口>` 交给客户;`ssh dev@<跳板机> -p <端口>`。
3. 客户看「用量」页确认区间在记(MTR-01 语义:从调度到结束)。

## 6. 之后

- 换公钥:客户自助(`PUT /papi/devmachines/<name>/ssh-key` / 控制台「换公钥」)。
- 欠费/违约:`scripts/tenant-freeze.sh tenant-acme freeze|stop|restore "<原因>"`(runbooks/tenant-freeze.md)。
- 月末:`billing/invoice.py --ledger <ledger> --pricebook billing/pricebook.yaml --tenants platform/tenants.yaml --tenant tenant-acme --from … --to … [--dedicated-nodes dgx03 …]`。
- 退租:预留节点 `desiredOwner: ARISE`(经清理门 SANITIZING 回池,E2E-04);删除命名空间前确认 `arise-longterm` Retain PV 的处置已书面确认(D2)。
