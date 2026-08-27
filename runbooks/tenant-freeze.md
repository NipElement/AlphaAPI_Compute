# 租户冻结(欠费 / 违约)

> **决定何时冻结是商务的事(D6)。这本 runbook 只保证杠杆存在、行为确定、
> 可回退、有审计。** 机制:`scripts/tenant-freeze.sh`,三步,顺序不可换。

## 机制一句话

命名空间打上 `arise.ai/suspended=true` → 准入策略 `arise-tenant-suspended` 在
API server 层拒绝该命名空间**所有新工作负载**(pod / deployment / volcano job /
podgroup / PVC / service),拒绝信息写明"账户已暂停,联系 ARISE 支持"。
**运行中的负载不受影响**——一张迟到的账单不是杀掉别人训练任务的理由。
只有 admin 能给命名空间打标签(SEC-06:租户身份对 namespaces 没有任何动词)。

## 三步

```bash
export KUBE_CONTEXT=<ctx> APPROVED_BY=<你>@ariselabs.ai
# 1. 冻结:新工作被拒;运行中的照跑;命名空间上留下 who/when/why 注解
./scripts/tenant-freeze.sh tenant-acme freeze "invoice #2026-09 overdue 15d"
# 2. 宽限期(商务定,D6)结束后:停掉运行中的工作。卷**不动**。
./scripts/tenant-freeze.sh tenant-acme stop   "invoice #2026-09 overdue 30d"
# 3. 恢复:去掉标签;不会自动重建任何东西,租户自己重提。
./scripts/tenant-freeze.sh tenant-acme restore "invoice #2026-09 settled"
```

## 每步的"验证"

| 步 | 看什么 |
|---|---|
| freeze | 以租户身份建一个 pod 应得到 `SUSPENDED … Contact ARISE support`;`kubectl get ns tenant-acme -o yaml` 有 suspended-* 注解;其它租户不受影响 |
| stop | 该命名空间无 Running pod;PVC 全在;metering 台账在下一轮 poll 关闭这些区间(账单到此为止) |
| restore | 以租户身份建 pod 成功 |

SUS-01 矩阵用例按上面三步逐条断言。

## 明确不做的事

- **不删卷、不删命名空间。** 数据处置是另一个双人复核的流程
  (incident-disk-full.md 的规则),并且要等合同条款说话。
- 不改 NodeOwnership。DIRECT 客户欠费:节点仍是他的,直到合同/商务决定回收——
  回收走 `desiredOwner: ARISE`(卷门会拦下他的残留卷,正是为了这一刻)。
- 不改网关账号。租户仍能登录看到自己的状态与账单;这是刻意的——冻结页面比
  "登录失败"更能促成付款。(跟进项:控制台顶部显示"账户已暂停"横幅。)

## 审计

apiserver 审计策略(infra/dgx/audit-policy.yaml)记录 namespace 的标签变更
(Request 级);脚本额外把 who/when/why 写进注解,因为审计日志不记"为什么"。
