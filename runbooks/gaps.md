# 已知缺口与不可外推清单

> 文档 §8.1 要求每项测试标注 **SIMULATED / CONTROL-PLANE / HARDWARE**，
> 且 Phase A 只允许前两类通过。当前上线条件见 [生产就绪清单](../docs/production-readiness.md)。
> 本文件保留技术边界与带日期的历史复现；历史结论以较新的修复与验收记录为准。

## 1. §8.2 Device Plugin 契约 —— 已于 2026-08-16 关闭(附边界)

Phase A 初期用 `fake-gpu-advertiser` PATCH Node `status.capacity` 模拟扩展资源,
两行契约因此开放。现已由 **真实的 kubelet device plugin**
(`services/fake-gpu-plugin`,Go/gRPC v1beta1,DaemonSet `fake-gpu-plugin`,
仅 overlays/lab)接管 `arise.dev/fake-gpu`。逐行对照 §8.2 契约:

| §8.2 契约行 | 现状 | 证据 |
|---|---|---|
| 扩展资源名 `arise.dev/fake-gpu` | ✅ | 不注册 `nvidia.com/gpu` |
| 每 worker 8,共 32,control-plane 0 | ✅ | SCH-01a/b/c |
| 设备 ID `<node>-fake-gpu-0..7` 稳定唯一 | ✅ | ListAndWatch 上报;SCH-13 断言 ID 归属调度节点 |
| 分配行为返回 `ARISE_FAKE_GPU_IDS` | ✅ **已实现** | Allocate 注入 env;**SCH-13**(容器内实读) |
| 健康行为:Unhealthy 后 allocatable 随 **kubelet** 更新 | ✅ **已实现** | SCH-07 增强断言:capacity 保持 8、allocatable 降 7 —— 这是 device manager 独有语义,状态补丁做不到 |
| 仅 platform-system SA 可运行、digest 固定、只读根 | ✅ | scratch 镜像单静态二进制、只读根、drop ALL;构建器按 digest 固定(versions.env `GO_BUILDER_IMAGE`);lab 无 registry,镜像经 `kind load` 交付,imagePullPolicy: Never + 构建日志记录 image ID 作为 lab 级 digest 固定 |
| production overlay 删除该 DaemonSet | ✅ | 仅存在于 `overlays/lab`;dgx overlay 走 NVIDIA GPU Operator |

**分工(2026-08-16 起)**:plugin 独占 fake-gpu(不访问 API server,SA 不挂载
token);advertiser 只 PATCH `sim-vcpu`/`sim-mem-gi` 尺寸资源,并保留 §11.1 故障
API(`/test/unhealthy` 等),按 node-map 把故障**路由**到对应节点的 plugin pod ——
SCH-07 的调用面因此逐字节兼容,但 allocatable 的变更方已换成 kubelet。

**仍属模拟、不得外推的边界**:设备本身是假的 —— 无 /dev 节点、无 NUMA/拓扑提示
(`GetPreferredAllocation` 为空实现)、无真实健康探测(健康状态来自故障注入 API)。
DGX 到货后由 GPU Operator 的 NVIDIA plugin 接管,本组件不上生产。

## 1b. 事故记录:2026-08-15 重构丢失 `controller/` 目录源文件

2026-08-15 的仓库重构(pre-git)误删了 `controller/` 目录(NodeOwnership CRD 的
源清单),`make platform` 自那时起断链,直到 2026-08-16 部署 device plugin 时才
暴露 —— 期间集群靠先前已应用的活对象运行,无人发现。已从活集群对象恢复
(`controller/crd.yaml` 头部注明恢复过程;CEL 不变量完整保留)。

**教训与要求**:本仓库在下一次任何破坏性重构之前 **必须 `git init` 并提交基线**。
无版本控制状态下的删除没有回滚点,这次靠"集群还活着"侥幸恢复,不可依赖第二次。

## 2. 环境导致的 BLOCKED（非技术缺陷）

以下用例因本机**无 IAM 角色 / 无 AWS 凭据 / SSM 未注册**而无法执行。
按 §10.2，它们**必须保持 BLOCKED，不得记为 PASS**：

`PRE-02` `PRE-03` `AWS-02` `AWS-03` `AWS-04` `AWS-05` `AWS-06` `CST-01` `CST-02`

关闭方式：见 `runbooks/aws-readonly-and-snapshot.md`，在授权终端执行后回传 JSON。

`PRE-04`（EBS 快照）当前为 **FAIL + 豁免**，不是 BLOCKED —— 因为它确实不满足，
只是被 WAIVER-2026-08-11-001 接受了。做完快照后可改判 PASS。

## 2b. 全库安全自审与修复（2026-08-16,多智能体审查 + 对抗复核）

六维自审(Go 插件 / Python 服务 / 清单+RBAC / 测试质量 / 前端 / 文档),
高危项逐条独立验证,确认 2 CRITICAL + 9 HIGH,**全部已修并实测**:

- **CRITICAL 网络隔离破损**:租户 egress 白名单 `deny-imds-and-host-links`
  漏了 **pod CIDR**,租户 pod 可绕过网关按 pod IP 直连内部服务
  (tenant-portal `?ns=<别的租户>`、ops-console、fake-gpu-plugin `/unhealthy`
  伪造设备故障)。**修**:两租户 ns 的 except 加 `10.244.0.0/16`(Layer 1,egress,
  SEC-05 已证生效);另加 `platform-internal-ingress`(Layer 2,平台侧纵深防御,
  排除 gateway 保留外部入口)。实测:租户→portal BLOCKED、控制台 200、
  内部故障注入端到端仍通、探针存活。
- **HIGH owner-gate 容忍绕过**:门只看 `spec.nodeSelector`,可用 nodeAffinity
  绕过。**修**:两 overlay 各加容忍门,禁止租户容忍 `arise.ai/*` 污点
  (含无 key 的"容忍一切"),从污点层气密封堵。实测拒绝生效。
- **HIGH quarantine() 死代码**:`adapter.unlist_machine()` 引用不存在的全局,
  NameError 被吞,被隔离节点仍在 VAST 可售。**修**:adapter 改为必传参数,
  10 个调用点全部线程化。
- **HIGH device plugin**:ListAndWatch 不监听流上下文(kubelet 重注册时旧
  handler 泄漏、偷健康令牌)→ 加 `s.Context().Done()`;管理口注释谎报隔离 →
  改为诚实(0.0.0.0 + 靠 platform-internal-ingress 封,依赖显式声明)。
- **HIGH 测试可信度**:`blocked()/invalid()` 会把已记录的 FAIL 降级 → FAIL 变黏性;
  VST-06 "无生产 VAST 端点" grep 的是不存在的目录 → 改扫真实源码、缺目录判失败。
- **HIGH 文档**:README 执行顺序 `make plugin` 排在 `make cluster` 前(kind load 无集群)
  → 重排;"不构建镜像"与 Go 插件矛盾 → 改为五 Python + 一 Go 的诚实分述。

供应链补强:生成并提交 `go.sum`(47 行,间接依赖按 hash 固定),Dockerfile 由
`go mod tidy` 改 `go mod download`(校验而非重解析)。

### 2b.1 稳健性打磨批次(2026-08-17 完成)

上述审查登记的 medium/low 项,本批已修并经全量矩阵(34/34 PASS)确认:

| 项 | 问题实质 | 处理 |
|---|---|---|
| 退出门 | `[[ $FAIL_N -eq 0 ]]` 让 **INVALID 也算通过** —— 证据不可信的 run 会被记成绿 | FAIL→exit 1、INVALID→exit 2;BLOCKED 按 §10.2 是合规结果,不失败但显式提示 |
| OBS-02 | `wait_for 120 "1" get --raw "/readyz"` **永不可能成功**(/readyz 返回 `ok` 不是 `1`),超时又被 `\|\| true` 吞掉 | 删除。该测试 204s→92s,每轮矩阵省 ~112s |
| AlertsView | fetch 失败被渲染成"没有告警" —— **运维页上最危险的谎言**:Alertmanager 挂了却显示一切平静 | 区分空列表与失败;失败显红色警示 + 具体错误,文案明写"这不等于「没有告警」" |
| QuotaView | `.replace('requests.','')` 先跑,把 `storage.k8s.io/requests.storage` 里的也吃掉,后缀规则永不匹配 | 长规则优先 + `^requests.` 锚定 |
| Overview tile | 标题 "GPU healthy (sim)" 但值是 `gpuUsed/gpuTotal`,**显示的是分配量** | 改名 `gpuAllocated`(中英同步);MonitoringView 的 `gpuHealthy3h` 是真健康指标,保留 |
| `npm run typecheck` | `composite:true` + `-b --noEmit` 是 TS 禁止组合(TS6310),**从来没跑过**;不加 `--noEmit` 就吐出 `vite.config.js` 影子文件(即那个陷阱的根因) | composite 产物导到 `node_modules/.tmp`;`vue-tsc -b` 现在 exit 0,源码树干净 |
| go-plugin serve() | self-dial 失败时 gRPC server / listener / socket 全不回收,而重试循环会再 Listen 同一路径 —— **每次尝试泄漏一个 server** | 失败即 `srv.Stop()` + 删 socket + 置空 |
| go-plugin 探针 | 只查 admin HTTP;与 kubelet 断开注册时探针仍绿,节点不再上报设备却没人重启它 | `/healthz` 未注册返回 **503** |

**仍未做(低优先,不影响安全/正确性)**:测试无 trap 清理(所有权链测试靠矩阵顺序清场)、
SCH-02/03 拒因匹配与资源无关(可能因错误原因通过)、SCH-11/SCH-06 二阶段缺 gang 存在性守卫、
`verify.sh` SCH-01a 选择器空匹配时空洞通过、负向断言把 kubectl 失败当作"不存在"、
前端 `/auth/users` 的 401 不回登录页、WorkloadDrawer 列不随语言响应、少量硬编码文案绕过 i18n、
`sync_health_mirror` 在插件列表为空时清空镜像、VAST 交接每次泄漏一个 ConfigMap key、
`make deploy` 不构建/加载插件镜像。

## 3. NetworkPolicy 生效性（SEC-05）—— 已于 2026-08-11 实测关闭

原先的担忧是 kind 默认 CNI（kindnet）可能不执行 NetworkPolicy，
使得「对象存在」被误当作「策略生效」。**已用带对照组的探针实测证伪该担忧：**

```
tenant-arise -> vast-mock : first=BLOCKED_TimeoutError steady=BLOCKED_TimeoutError
test-system  -> vast-mock : first=REACHABLE           steady=REACHABLE   (对照组)
```

拒绝路径被阻断、同时对照路径可达 —— 二者同时成立才能排除「网络本身坏了」这一
替代解释。本 kind/kindnet 版本**确实执行** NetworkPolicy，SEC-05 判定为 PASS。

### 但发现了一个真实的策略编程时间窗

探针最初单次采样时出现结果翻转（一次 REACHABLE、一次 BLOCKED）。根因是
**CNI 为新 Pod 编程策略规则不是瞬时的**：Pod 在被调度到规则装载完成之间，
存在一个短暂窗口，其出站连接可以成功。

- 探针改为 12 秒内采样三次，同时报告 `first=` 与 `steady=`，判定以 steady 为准，
  并在 first≠steady 时显式记录该窗口，而不是把它平均掉。
- **对 DGX 实机的含义**：租户 Pod 启动瞬间可能有一个亚秒级的出站窗口。
  若客户隔离要求严格，Phase B/实机需要评估是否用
  「先创建策略、再允许调度」的准入顺序，或改用在 Pod 网络就绪前即完成编程的 CNI。
  该项已进入到货验收清单。

## 4. Phase A 永远不能证明的事（§1.3）

| 领域 | 为什么不行 | 到货后对应用例 |
|---|---|---|
| CUDA / 显存 / 驱动 / NVML / DCGM / MIG | 无 GPU 硬件 | HW-03, HW-09 |
| NVLink / NVSwitch / NCCL | 无互连硬件 | HW-04, HW-05, HW-07（到货后 `make dgx-hw-accept`：infra/dgx/acceptance/） |
| InfiniBand / RDMA / 8-rail / OpenSM | 无 IB 网卡与交换机 | HW-06（同上，双节点任务） |
| BMC / Redfish / PDU / ToR / 固件 | 无带外硬件 | HW-02, HW-14 |
| E1.S NVMe 吞吐 / 擦除耗时 / 耐久 | 无该存储 | HW-08, HW-12 |
| 真实 VAST host 上架 / 计费 / 客户镜像 | 只连 Mock，硬禁真实端点 | HW-11 |
| 裸金属性能 / 可靠性 / 租户强隔离 | kind 容器节点不提供 | 全部 HW-* |
| **头节点整机故障**:已在跑的 GPU 任务是否继续 | 停 lab 的 API server 会毁掉正在跑的矩阵与集群本身 | **到货后必做的一次演练**:停头节点(或其 kubelet↔API 通路)5 分钟,断言 (a) DGX 上已有的租户 Pod 仍在 Running、(b) 登录/提交如期失败、(c) 恢复后 `MeteringDown` 已响过且台账缺口按 `runbooks/incident-metering.md` 补齐。`docs/decisions-D1-D8.md` D1 的后果表里唯一没有实测的那一行 |

`services/capacity-controller/capacity_controller.py` 的 `run_sanitization()` 中
`data_erasure` 与 `health_score` 两项**恒为 True 且标注 SIMULATED**——
它们证明的是**门禁顺序**（未清理不得回归），不是清理本身。

## 5. 本机特有的限制

| 限制 | 影响 |
|---|---|
| 无 EBS 快照回滚点 | 系统级故障只能重装恢复（W-1） |
| 与 735 GB 生产 MongoDB 同盘 | 磁盘类故障注入受限：`CHA-04` 只能用独立测试卷，**绝不可**填满根卷 |
| SSM 未注册，SSH 是唯一通道 | **禁止**任何网络分区/路由/防火墙类故障注入（§11.1 硬性禁令） |
| 共用开发机，他人会话活跃 | 资源争抢影响时序类测试；`P95 < 120s` 的 SLO 需记录当时 load |
| 8 vCPU / 14 GiB | 定档 STANDARD；`Loki` 不部署，监控保留 3 天 |

### 5.1 登录/用户体系的当前边界（2026-09-07 更新）

账号、身份版本和登出撤销已持久化到 retained PVC 上的 SQLite；正常重启保留状态，
单副本 Recreate 与文件锁限制单写入者。网关已有密码长度规则、IP/账号双维度登录限流、
密码变更撤销会话，以及认证操作日志；不再以“将来交给 IdP”为由省略这些功能。

| 项目 | 当前状态与边界 |
|---|---|
| 持久化与恢复 | 已实现一致备份、SIGKILL 恢复与恢复后轮换签名密钥的测试；异机恢复仍需生产验收，见 [账号恢复](auth-recovery.md) |
| 认证并发 | PBKDF2 在身份锁外运行，签发/修改前重新校验身份版本，避免阻塞现有会话或操作被重建的账号 |
| SSO / MFA | 尚未接入，当前按管理员开户运营 |
| 高可用 | SQLite 网关明确是单写入者，不能直接把 replicas 调大作为 HA |
| TLS | lab 使用 loopback port-forward；生产 Caddy 配置已备好，仍需真实域名、证书和访问验证，见 [公网入口](public-edge.md) |

## 6. Phase A 退出门当前状态

§10.6 要求"所有 P0、P1 用例 PASS；BLOCKED/INVALID/NOT-RUN 数量为 0"。

**当前不可能满足**：第 2 节的 9 项在拿到 AWS 输出前恒为 BLOCKED。

因此 Phase A 退出签署有两种走法，需要项目负责人选择：
- **(a)** 先补齐 AWS 权限与快照，把 9 项 BLOCKED 关掉，再走完整退出门；
- **(b)** 以"Phase A（受限范围）"签署，明确把这 9 项 + 第 1 节两行契约
  列为**遗留项**，带 owner 与截止日期进入 Phase B/到货清单。

无论哪种，都**不得**把 BLOCKED 改写成 PASS。

## 7. 跨队列 reclaim 调查（2026-08-12 历史记录）

> 当前产品使用方案 B 的整节点预留，SCH-10 原场景已过时；详见文末“§7 终章”。以下保留调查过程，不代表当前客户保障依赖 reclaim。

### 状态

`SCH-10` 判定 **FAIL**。这不是测试写错，也不是环境问题 —— 是一项我们想要的能力
在 Volcano v1.15.1 + 本配置下**没有交付**。按 §10.2，P1 FAIL 需要负责人书面接受
或修复，不得改写成 PASS，也不得靠放宽断言变绿。

### 期望行为

`arise-internal`（weight 2，reclaimable=true）占满算力后，
`direct-customer`（weight 8，reclaimable=false）提交合同工作，
调度器应回收内部作业的容量交给客户作业。

### 实测

内部作业保持 4/4 运行，客户作业永久 Pending。

### 已确证的事实（来自 `-v=4` 调度器日志）

1. **队列数学是正确的**：`Queue <arise-internal> is overused, ignore it`
   —— Volcano 确实识别出内部队列超额。
2. **第一道阻塞（已修复，真实缺陷）**：
   ```
   Task tenant-direct/claimer-p cannot preempt (policy Never)
   No preemptors in Queue <direct-customer>, break.
   ```
   `arise-contract-bound` 原本设了 `preemptionPolicy: Never`。该字段管的是
   **本作业能否去拿别人的**，不是**能否被别人拿走**。合同作业因此无法回收
   自己应得的容量。已改为 `PreemptLowerPriority`；不被抢占的保护来自
   priority 值 1000000 高于一切，与该 policy 无关。
3. **第二道阻塞（未解决）**：修复 1 之后日志变为
   ```
   No validated victims on Node <worker3>: not enough resources:
     requested   <cpu 4000, arise.dev/fake-gpu 8000>
     future idle <cpu 3550, arise.dev/fake-gpu 0.00>
   ```
   victim 的资源**从未被计入可回收量**（`future idle fake-gpu 0.00`）。
   即没有任何 pod 被插件链接受为合法 victim。

### 已排除的假设

| 假设 | 验证方式 | 结论 |
|---|---|---|
| gang 保护导致（minMember 不可跌破） | 换成 `minMember=1` 单 pod victim | 仍不回收。**但此实验无效**：Volcano 中每个 pod 都属于某个 PodGroup，`minMember=1` 同样构成 gang 约束，因此并未真正排除 gang |
| `enableReclaimable: true` 可解 | 在修复阻塞 1 之后重试 | 无变化，已从配置中撤回 |
| 扩展资源不参与 proportion 份额计算 | 让 CPU 请求与 GPU 成比例（500m/GPU） | 无变化。但该改动本身更贴近真实训练作业，予以保留 |

### 仍然未知

哪个插件否决了 victim。需要读 Volcano v1.15.1 的 `ReclaimableFn` 实现，
或用更高日志级别定位。**不要在未定位根因前继续改配置** ——
本次调查中我连续基于两个未经验证的假设改了配置，都是无效改动。

### 对机群设计的实际影响（需要项目负责人决策）

给客户的算力保障**目前不能依赖跨队列 reclaim**。可用的替代机制：

| 方案 | 状态 | 说明 |
|---|---|---|
| **A. 同队列内优先级抢占** | ✅ 已验证（SCH-05 PASS） | 内部与客户作业放同一队列，靠 PriorityClass 分级。抢占确实生效。代价：放弃队列级配额隔离，且需修改 SCH-12 的队列绑定策略 |
| **B. 由 Capacity Controller 预留节点** | ✅ 机制已具备 | 把节点 owner 切成 DIRECT，内部作业因 `arise.ai/owner=ARISE` 选择器天然排除。不依赖调度器公平性，且与合同门是同一套状态机 |
| **C. 静态分区** | 未实现 | 按 pair 硬分给客户/内部。简单但利用率低 |

**倾向 B**：它复用已经通过 P0 验证的所有权状态机，保障来自准入策略而非调度器
启发式，且与合同门天然一致 —— 客户拿到的是整个节点，不是一个可能被抢回的份额。

### 附带发现：抢占会留下 gang 孤儿

`SCH-05` 记录：Volcano 只驱逐满足需求的最少 victim。一个 `minMember=2` 的
gang 被抢走 1 个成员后，**剩下的成员永远凑不齐 gang，不产出任何结果，却继续占着 GPU**。
裸 PodGroup 没有任何东西会对驱逐作出反应。
缓解措施是 vcjob 的 `PodEvicted -> RestartJob` 策略（SCH-08 已验证）。
**真实机群上不要提交裸 PodGroup 跑 gang 作业。**

### 决策（2026-08-12）：采用方案 B，DIRECT 节点预留

项目负责人选定 **方案 B**。已实施并端到端验证（`DIR-01` PASS）。

**客户算力保障不再依赖调度器公平性**，改由 Capacity Controller 的所有权状态机提供：

| 步骤 | 机制 |
|---|---|
| 预留 | `NodeOwnership.spec.desiredOwner=DIRECT` → cordon → 仅驱逐 `tenant-arise` 工作负载 → 打 `arise.ai/direct-owned` 污点 → **uncordon** |
| 隔离 | 准入策略：`tenant-arise` 只能选 `owner=ARISE`，`tenant-direct` 只能选 `owner=DIRECT`，任何租户都不得选 `owner=VAST` |
| 可用 | 节点**保持可调度**，客户作业带 `arise.ai/direct-owned` 容忍度即可落上去 |
| 漂移纠正 | 稳态每周期校验：污点缺失或节点被误 cordon 都会自动修复并告警 |
| 释放 | `desiredOwner=ARISE` → SANITIZING → 健康门 → 清除污点 → 回归内部池 |

**为什么这比调度器份额更强**：客户拿到的是**整个节点**，保障来自准入拒绝（API server 层），
而不是一个在负载下可被重新计算的公平份额。而且它复用了已通过 P0 验证的同一套状态机 ——
`VST-03` 的合同门、`OWN-06` 的漂移纠正、`E2E-04` 的清理闭环全部原样适用。

**一个刻意的设计细节**：DIRECT 节点**不 cordon**。一个被 cordon 的预留节点等于
拒绝给客户他付钱买的算力 —— 这和让别人跑上去一样是违约。隔离靠污点 + 准入，不靠 cordon。

**SCH-10 处置**：从默认门禁集合中移除（降为 P2），保留为可按需执行
（`./tests/run.sh SCH-10`），以便 Volcano 升级后复查上游行为。
不删除、不改绿 —— 平台已不依赖该能力，因此它不再是门禁；但它记录的上游限制依然为真。


## 8. E2E-01 从零重建 —— 已通过（2026-08-13）

`scripts/rebuild-verify.sh` 销毁 kind 集群并仅从仓库重建，然后逐项比对
「平台**是什么**」而非「这个实例碰巧编号成什么」：节点映射与容量、CRD、
准入策略、队列、优先级类、namespace 与 PSA 等级、镜像 digest、配额、
Grafana UID。`resourceVersion`/`uid`/时间戳/Pod 名/ClusterIP 按构造排除。

**结果：连续三次重建，第三次 9/9 全部一致，平台重建耗时 85 秒，
重建后完整矩阵 26/26 PASS。**

### 重建暴露了两个真实漂移（这正是该用例的价值）

长时间存活的集群积累了手工修复、重启和补丁。如果其中任何一项是承重的，
其他所有绿色结果就都部分是历史的偶然。只有把集群毁掉重建才能知道。

| # | 漂移 | 根因 | 修复 |
|---|---|---|---|
| 1 | `monitoring`/`platform-system`/`vast-mock` 三个 namespace **缺少 project 标签**，与仓库定义不一致 | `make code` 用裸 `kubectl create namespace` 创建它们，而 overlay 里是带标签定义的 | namespace 改为**只由 `make platform` 拥有**；`deploy` 顺序改为 `platform code volcano`；`make code` 不再创建 namespace，缺失时直接报错 |
| 2 | Grafana **文件夹 UID 每次重建都变** | provisioning provider 未设 `folderUid`，Grafana 自动生成 | 固定 `folderUid: arise-b300`。dashboard UID 本来就是稳定的，但指向文件夹的链接与告警注解会在每次重建后失效 |

### 仍需注意

本用例证明的是**平台结构与功能可从 Git 重建**，不包括：
- 主机层（Docker、sysctl、工具链）—— 那些仍是手工步骤，Ansible role 未实现
- 证据包与 EBS 快照 —— 按 §11.7 不随集群删除

### §7 终章（2026-08-13）：根因闭环，场景本身已被方案 B 判定过时

结合 Volcano 源码（`../volcano`，reclaim 主链路 v1.15.1 与 master 逐 diff 确认一致）
与两组受控实验，最终事实链：

1. **SCH-10 原场景在方案 B 下已非法**（今日实测）：`arise-tenant-owner-gate` 准入
   策略禁止 `tenant-direct` 的 pod 落在 `owner=ARISE` 节点 —— claimer pod 根本
   **不会被创建**，PodGroup 空转 Pending。DIR-01 上线后所有 SCH-10 复跑（包括
   guarantee A/B）都是无效实验：没有 pod，何谈回收。这不是 bug —— 整节点预留与
   调度器回收是两种互斥的客户保障机制，我们选了前者，测试没跟上。
2. **合法同层场景下（test-system，准入放行），reclaim 确实走通了 claimer 侧全部
   门禁**：`reclaim.go:211` 出现（进入了逐节点 victim 评估），无 `cannot reclaim`
   （Preemptive 门通过）、无 `No reclaimees`（候选存在，`Preemptable` 默认 true
   —— 源码确认 v1.15.1 `pod_info.go` 默认即 true，与早前"需要 annotation"的
   猜测相反）。卡点在 `ssn.Reclaimable` 投票返回空。
3. **victim 投票为何空**（源码 + 实验）：tier1 gang 对"正好处于 minMember 的
   gang"必然弃权全部成员（`gang.go`：`ReadyTaskNum > MinAvailable` 才放行），
   落到 tier2 由 proportion 独裁；proportion 的 deserved 是**按需求水位填充**的
   集群级份额 —— 当集群维度总需求 < 总容量时（我们 24 < 32），每个队列
   deserved = 其全部请求，**没有队列"超额"，永远没有 victim**。而我们的稀缺
   是**拓扑性的**（gang 钉在 pair 的 16 卡里），队列份额数学对拓扑不可见。
   推高全局需求（40 > 32）的验证实验因新增负载自身也被队列门挡住而未能闭环 ——
   这本身再次证明：**proportion 份额 + 拓扑钉死的 gang，二者交互不可依赖**。
4. **处置**：
   - 客户算力保障维持方案 B（DIR-01 绿），reclaim 在产品中无承重角色；
   - 追假设加的 `guarantee`/`deserved` 已从队列配置撤销（未证实有效的投机配置
     不留在产品 spec 里）；
   - SCH-10 标记 **OBSOLETE-BY-DESIGN**，保留可手工执行；
   - 若未来 reclaim 重新成为需求（例如内部多级租户）：上游支持的确定性路径是
     **capacity 插件 + 显式 `spec.deserved`**（去掉水位填充），列入 Phase B
     评估项，且需连同「拓扑感知份额」一起评估 —— 全互联 XDR 拓扑（见
     product-architecture.md §4）会削弱 pair 钉死，反而使集群级份额数学重新可用。

## 8. 2026-09-08 复核后仍开放（不因"没有真 GPU"而开放的部分）

| 项目 | 现状 | 为什么还没关 |
|---|---|---|
| GPU/Network Operator 镜像 | 无 digest、不在 mirror | 需要 NGC 侧 digest；是到货前最大的供应链缺口，见 `docs/production-readiness.md` |
| 14 个用例的证据只有 verdict | `results.json` 的 `verdict_without_observation`，9 个是 P0 | 每个用例要单独补抓取，未做 |
| 开发机 `terminationGracePeriodSeconds: 5` | 门户写死 | 低影响：/home 是 emptyDir，本来就随 pod 消失；但客户没被告知 |
| `access-system` / `edge-system` PSA 为 `privileged` | hostPort / hostNetwork 需要 | pod 自身已最小化（非 root、drop ALL、只加 NET_BIND_SERVICE）；风险在"谁能在这两个命名空间建 pod"，需确认只有部署流程有权 |
| 单个文件归档失败 | 已改为计数并让 Job 失败 | 已关（本轮） |
