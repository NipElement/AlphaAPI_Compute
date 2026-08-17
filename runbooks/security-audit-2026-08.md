# 全系统安全审计 — 2026-08-15（Arco 重写前）

在把前端换成 Arco 之前，对四个后端服务（gateway / tenant_portal / console /
capacity_controller）做了一次多维穷尽审计（5 维并行 finder + 对抗验证），因为后端
在重写中原样保留，其缺陷会带进新前端。共 **32 条发现**（3 严重 / 4 高 / 9 中 / 16 低），
其中 **5 条经独立对抗验证 CONFIRMED**。本文件记录已修与暂缓项的处置与理由。

## 已修复（5 条 CONFIRMED，均现场验证 + 回归覆盖）

### C1/C3 — 严重：跨租户越权（省略 ns → 落到 tenant-arise）
`gateway.py` 的租户校验用 `(q.get("ns") or [me["tenant"]])[0]`，调用方**省略** ns 时
默认成自己的租户、通过校验；网关随后转发**空 query**，而 `tenant_portal._ns()` 把缺失
的 ns 默认成固定的 `tenant-arise`。两个默认值不一致 → 任何非 arise 租户用户（如种子账号
`direct-cust`）省略 ns 即可对 tenant-arise 命名空间**读/写/删**。papi 不在只读白名单，
GET/POST/DELETE 全部穿透。

**修复（双层）**：
- 网关：非 admin 的 papi 请求，转发前用 `urlencode` 把 ns **强制重写**为 `me["tenant"]`，
  剥离任何客户端 ns。缺失/重复/显式跨租户全部收敛到调用方自己的租户。
- 门户：`_ns()` 改为**缺失即 400**（fail-closed），不再默认到某个真实租户。
- 现场验证：`direct-cust` 省略 ns → 只拿到 tenant-direct；显式 `ns=tenant-arise` → 403。
- 回归：UI-03 新增 “omitted ns pins to the caller tenant / never leaks tenant-arise”。

### C2 — 严重：VAST→DIRECT 不 unlist → 一个 GPU 节点被双主
`capacity_controller.py` 的 DIRECT 分支只在 `activeContracts>0` 时阻塞。一个**已上架
（listed）但空闲**的 VAST 节点被预留给 DIRECT 客户时，直接 drain/uncordon/贴 DIRECT 标签，
**从未 unlist**。于是 VAST 那边仍可租、k8s 已交给客户 → 同一物理节点两个主；且 owner
标签只读出单值，`OwnerConflict` 告警看不见这次双主。

**修复**：DIRECT 交付前先 `unlist_machine` 并确认（`listed` 每轮从 adapter 实时读取），
delist 成功后才 drain——与 ARISE 回收路径同一道门。unlist 失败则回退 VAST_RENTED 不前进。
- 回归：新增 DIR-02（VAST_READY listed=True → DIRECT → 断言 DIRECT_ASSIGNED 时 listed=False、
  owner=DIRECT、未 cordon）。

### H4 — 高：k8s API 路径注入（pod/workload 名未编码）
`tenant_portal.py` 的 `instances()`（直取 pod 回退）和 `pod_logs()` 把用户提供的名字直接
拼进 API URL 路径，`../`、`?`、`#` 可越出 `pods/<name>` 段、注入 query。

**修复**：新增 RFC1123 名称校验 `name_ok()`；`pod_logs` 非法名直接 400，`instances`
回退仅在名字合法时执行；`tail` 用 `min(max(int,1),2000)` 钳制。

### H5 — 高：请求体无上限（未认证内存耗尽）
登录等路径按 `Content-Length` 无上限读取，14GB 机器上可被超大/谎报 body 打爆。

**修复**：网关 `_read_body()` 硬上限 1 MiB，超限 413；`_body_json` 与代理 POST/PUT 共用。
现场验证：2MB 登录 body → 413。

## 顺带修掉（中危，明确 bug）
- **500 泄漏内部异常**（tenant_portal ×3 + console ×1）：`{type}: {exc}` 回显改为通用
  `"internal error"`，服务端仍记录异常类。

## 暂缓 / 由 Arco 重写消解（记录在案，非遗漏）

| 发现 | 处置 |
|---|---|
| 前端：双提交无禁用、20s 自动刷新冲掉表单、无渲染取消、401 当普通 toast、后端错误串直显 | **Arco 重写消解**：受控组件提交即禁用、路由守卫处理 401、错误集中展示；旧 PAGE 将删除，不回补 |
| `/prom` 对租户用户开放任意 PromQL（跨租户指标/DoS） | **随重写收敛**：新前端只发预定义查询，届时把 `/prom` 收成查询白名单 |
| 会话存储无界 + 每次登录尝试触发 120k 轮 PBKDF2（轻量 DoS） | 暂缓：预演内存态，实机由 OIDC/SSO 接管；如需可加登录限流 |
| 环境变量未设时落到弱默认口令（admin/arise-admin） | 预演刻意行为（README 已载明）；实机由 OIDC 取代，不发弱口令 |
| Cookie 无 `Secure`、SameSite=Lax | 预演走 127.0.0.1 无 TLS；实机 Ingress 终结 TLS 时置 `Secure` |
| console 接受 `desiredOwner=QUARANTINED` 绕过合同门 | **设计取舍**：隔离是运维紧急动作（疑似失陷即刻隔离），优先级高于合同门；需产品决定是否隔离时也 unlist，暂不改紧急语义 |
| CSP 含 `unsafe-inline` | **已修**：SPA 为外链 JS，CSP 收成 `script-src 'self'`（`img/font-src 'self' data:`、`connect-src 'self'`）|
| POST/DELETE 到 `/` 触发 IndexError；错误信封跨端点不一致 | 低危：`/` 现走静态伺服（不再 IndexError）；API 信封统一留待后续 |

## Arco 重写引入的一处姿态权衡（记录在案）

网关同源伺服 `web/dist`：dist（1.7MB）超 ConfigMap 1MiB 上限，故 **readOnly hostPath**
把 dist 从网关所在节点（control-plane）挂进 pod。`platform-system` 的 PSA `enforce=privileged`
（它本就是唯一允许特权工作负载的基础设施命名空间）放行 hostPath，但 `warn=baseline` 会在 apply 时
打一条 hostPath 告警——只读挂载，风险低，属有意取舍。网关的**零 k8s 凭据**属性不变
（仍 `automountServiceAccountToken=false`，UI-03 断言）。实机阶段前端由 Ingress/nginx 或 CDN 伺服，
不用 hostPath，此告警随之消失。

## 未复现（对抗验证 REFUTED，非缺陷）
审计初判 32 条中有 2 条在对抗验证阶段被判 REFUTED（无法对真实代码构造可达复现），未列入修复。
