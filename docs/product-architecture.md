# ARISE B300 平台产品架构

> 目标产品形态：对标火山引擎机器学习平台（Volcengine MLP）的多租户 AI 算力平台，
> 调度层基于 Volcano 开源引擎（源码在 `../volcano`，master ≈ v1.16.0-alpha；
> 线上运行 v1.15.1）魔改。本文档是"截图那张架构图"到我们代码的映射，
> 以及 4 节点 → 8 节点/64 卡的扩展设计。
>
> 参考来源（2026-08-13 抓取）：
> - [产品文档首页](https://docs.volcengine.com/docs/6459/?lang=zh) — 产品分层与能力清单
> - [实例规格及定价](https://docs.volcengine.com/docs/6459/72363?lang=zh) — 规格族与配比
> - [资源组与队列操作](https://docs.volcengine.com/docs/6459/80586?lang=zh) — 资源组/队列模型（页面 JS 渲染，内容经搜索摘要获取）
> - 注：72379 等部分子页为空渲染无法直接抓取，结论以能读到的页面为准。

---

## 1. 分层映射：截图 → 我们的组件

| 截图层 | 火山引擎 | 我们（现状） | 我们（规划） |
|---|---|---|---|
| **User Interface** | Web 页面 / openAPI / PythonSDK / CLI | **统一控制台** `services/gateway/gateway.py`：单端口，登录门 + admin/user 角色体系（会话 cookie、PBKDF2 口令、用户管理模块），角色在代理层强制（user 仅达 papi/prom 且命名空间锁定本租户）；左导航对齐火山引擎 IA（概览/开发机/自定义任务/在线服务/存储卷/镜像/资源队列 + 运维分组，按角色裁剪），监控内嵌；后端为权限分离的 portal/console，gateway 零凭据（UI-03） | PythonSDK/CLI 包一层 REST；实机阶段登录层换 OIDC/SSO（企业 IdP），角色模型不变；用户/会话落库（预演为内存态） |
| **ML Service** | 开发机 / 作业式训练 / 在线推理 / 离线批量 / Pipeline / AI 资产中心 | 作业式训练 = vcjob（SCH-08 已验证多角色 gang）；开发机 = 长驻 pod + PVC（FLV-03 形态） | 开发机加 SSH/IDE 接入；在线推理 = Deployment+Service+HPA；资产中心 = 镜像仓库 + 模型仓库（对象存储）+ 实验元数据 |
| **ML System** | k8s Scheduler + Operator + DevicePlugin | Volcano v1.15.1（gang/queue/priority/preempt）+ Capacity Controller（NodeOwnership 状态机）+ **fake-gpu device plugin**（真 kubelet v1beta1 契约,Go/gRPC,Allocate 注入设备 ID;advertiser 只剩 sim 尺寸资源 + 故障路由） | 真 GPU Operator + NVIDIA device plugin(同一 kubelet 契约,lab 已预演);Volcano 魔改点见 §5 |
| **IaaS** | K8s 容器服务 + 计算/网络/存储 | kind 8 节点模拟（4 GPU + 2 CPU + 1 存储 + cp） | 8×DGX B300 + Quantum XDR 全互联 + 存储池 + CPU 冗余节点，见 §4 |
| **右侧栏** | 监控/告警/日志/Terminal | 监控与告警**内嵌**统一控制台（Prometheus/AM 经 gateway 白名单代理）；Grafana 降级为运维内部工具；日志 = kubectl logs | Loki 或等价（实机阶段）；Terminal = 开发机 SSH |

---

## 2. 资源模型：量化自定义（对火山引擎规格目录的刻意偏离）

**火山引擎的做法**：封闭规格目录 `ml.[族名].[规格]`，族固定 CPU:内存配比
（通用 g* 1:4、计算 c* 1:2、内存 r* 1:8；GPU 族按卡配套，如 `ml.g1ve.2xlarge` =
8 vCPU/32 GiB/1 卡；最小通用规格 8xlarge = 32 vCPU/128 GiB）。
碎片问题靠"只能买这几种"解决。

**我们的需求**（产品要求原文）："所有的 cpu core，mem，disk，gpu 都是独立
separate 可以申请的（当然有最小单位，不然碎片太严重了）……自定义内存，自定义存储"。
封闭目录满足不了"自定义"，于是：

### 量化网格（v1，`flavor-policy.yaml` 强制）

| 资源 | 最小单位 | 规则 | 强制点 |
|---|---|---|---|
| CPU | **500m**（半核） | ≥500m 且为 500m 整数倍 | VAP（API server 准入） |
| 内存 | **512Mi** | ≥512Mi 且为 512Mi 整数倍 | VAP |
| GPU | **1 卡** | 整数 0..8/节点 | K8s 扩展资源原生 |
| 存储（PVC） | **10Gi** | ≥10Gi 且为 10Gi 整数倍，class 限 `arise-shared`/`arise-longterm` | VAP |

- 任意组合合法：1.5 核 + 3.5 GiB + 0 卡 + 20 Gi 是合法请求（FLV-01 验证）。
- 规格目录退化为 **UI 预设**（small=1c2Gi、large=8c16Gi、gpu.1=8c32Gi1卡…），
  不是准入规则 —— 目录方便新手，网格保住碎片底线。
- 平台命名空间豁免：25m 的 metrics sidecar 不该被迫涨到 500m。
- LimitRange 默认值必须落在网格上（500m/512Mi），因为 LimitRanger 先于 VAP
  注入默认值 —— 否则平台会拒绝自己的默认值（E2E 实测过这个坑）。
- 网格参数是**产品决策**，上机前可调（比如 CPU 最小 1 核、内存 1Gi）；
  改一处 VAP 即全局生效。

### 队列 = 商业权益（不是硬件拓扑）

对应火山引擎"资源组→队列"两级：我们的**资源组 = owner 池**
（ARISE 内部池 / DIRECT 客户预留 / VAST 出租，由 NodeOwnership 状态机管理，
互斥已过 P0 验证），**队列 = 池内的配额与让渡关系**：

| 队列 | weight | reclaimable | 用途 |
|---|---|---|---|
| `direct-customer` | 8 | false | 合同工作，永不被回收 |
| `arise-internal` | 2 | true | 内部训练，让渡给客户 |
| `system` | 1 | false | канary/探针，硬上限 4 卡 |

命名空间↔队列绑定由 VAP 强制（SCH-12：权益不可借用）。
客户硬保障用 **DIRECT 整节点预留**（DIR-01 已验证），不依赖调度器公平性。
跨队列 reclaim 已根因闭环并判定为非承重能力：方案 B 的准入门本身就禁止
"客户作业落 ARISE 节点"这一 reclaim 前提，且 proportion 份额数学对拓扑稀缺
不可见（gaps.md §7 终章）。

---

## 3. 存储模型

| Class | 语义 | 火山引擎对应 | 生命周期 |
|---|---|---|---|
| `arise-shared` | 工作数据：数据集、checkpoint | vePFS 工作区 | 随 PVC 删除 |
| `arise-longterm` | 长期数据：模型产物、归档 | CloudFS/托管存储 | **Retain**，删 PVC 也留卷，需运维显式清理 |

- 租户配额三维：总量 `requests.storage` 200Gi + 每 class 上限（shared 150Gi / longterm 100Gi）——
  防止租户把全部额度换成永不释放的长期卷。
- "长期分配 long term 存储的 cpu node"产品形态 = CPU 池 pod + longterm PVC（FLV-03 验证了
  写入→pod 消亡→新 pod 读回的持久链路）。
- **诚实边界**：lab 里两个 class 都是 kind local-path 后端，验证的是**控制面**
  （分类/配额/量化/生命周期），不是数据面。真实存储池（vePFS 等价物）的吞吐、
  多写、容灾是到货后的事。

---

## 4. 拓扑：现状与两个月后的目标

### 现状（模拟，8 kind 节点）

```
control-plane
dgx01..04    role=gpu     8 fake-gpu each, pair 01-02 / 03-04, owner 状态机管理
cpu01,cpu02  role=cpu     纯 CPU 租售 + 冗余；owner=ARISE，无 node-id（不进状态机，不可出租到 VAST）
stor01       role=storage taint storage-only:NoSchedule，租户永远进不来
```

### 目标（2 个月，实机）

```
8 × DGX B300            = 64 GPU，NVIDIA Quantum(-X800) XDR InfiniBand 全互联
存储池                   独立服务器（vePFS 等价：候选 Lustre/Ceph/Weka，到货评估）
few × CPU node           冗余备份 + CPU 租售 + 控制面副本
```

**全互联对调度模型的关键影响**：现在的 `pair 01-02/03-04` 约束源于"两台一组"的
互联假设。XDR 全互联后**任意节点子集都是等价通信域**，pair 从"硬约束"降级为
"机架/故障域偏好"。迁移路径：

1. 短期：pair 标签保留，gang 作业的 nodeSelector 放宽为 `role=gpu`；
2. 中期：改用 Volcano **HyperNode**（`topology.volcano.sh/hypernodes`，CRD 已在
   v1.15.1 装好）描述网络拓扑层级（leaf=节点、spine=XDR 交换域），
   network-topology-aware 调度替代 pair；
3. 8 节点 gang（64 卡整机群作业）的准入与排队规则沿用现 gang 语义，minMember=8。

**扩展时的资源账**（供容量规划）：64 卡、每卡配套 CPU/内存按 B300 实机规格定；
CPU 池独立售卖不占 GPU 节点配套；`system` 队列上限随之等比放大。

---

## 5. Volcano 魔改清单（fork 前先穷尽不 fork 的路）

源码：`/home/ubuntu/yuansheng/B300/volcano`。原则：**能用配置/外围组件解决的不改内核**，
必须改内核的走独立 patch 分支 + 上游 PR 尝试。

| 需求 | 不 fork 的实现（现状） | 可能需要动源码的点 |
|---|---|---|
| 队列/gang/优先级/抢占 | 上游稳定特性 + 调度器配置（已验证） | — |
| owner 互斥（ARISE/DIRECT/VAST） | 节点标签+污点+VAP，调度器无感知 | — |
| 跨队列份额与拓扑 | proportion 的 deserved 是集群级水位填充：全局无稀缺则无 reclaim，而 pair 钉死的稀缺是拓扑性的，份额数学不可见（gaps §7 终章） | 若需 reclaim：`capacity` 插件 + 显式 `spec.deserved`；XDR 全互联后拓扑钉死弱化，集群级份额重新可用 |
| 拓扑感知 gang | HyperNode CRD（上游已有，未启用） | 启用 + 按 XDR 拓扑建模，可能不需要改码 |
| 碎片控制 | binpack 权重偏向 GPU 资源 + 量化 VAP | — |

魔改工作流：`versions.env` 锁上游 tag → patch 分支只放 diff → 每次升级重放 patch
并跑全量矩阵（26+ 用例就是回归门）。

---

## 6. 工作负载类型 → 产品能力（对齐截图 ML Service 层）

| 产品能力 | 实现 | 状态 |
|---|---|---|
| 作业式训练（分布式） | vcjob：多角色、gang、`PodEvicted→RestartJob` | ✅ SCH-08；孤儿风险已记录（禁裸 PodGroup） |
| 纯 CPU 容器（小/大/自定义） | 量化网格 + CPU 池节点 | ✅ FLV-01/02 |
| 自定义存储 / 长期存储 | 两个 storage class + 三维配额 | ✅ FLV-03（控制面） |
| 开发机 | 门户一键创建：长驻 pod + longterm PVC | ✅ UI-02；SSH 接入未做 |
| 在线推理 | 门户 `/api/services`：Deployment + ClusterIP | ✅ 形态已通（HPA 未做） |
| Pipeline / 资产中心 | KFP 等 | 远期，依赖对象存储 |
| 监控/告警/日志/Terminal | Prom/AM/Grafana as-code | ✅；日志聚合与 Terminal 实机阶段 |

---

## 7. 已知与实机的差距（此文档的诚实条款）

1. GPU/显存/NVLink/IB 一切性能语义 = 模拟（gaps.md §4）；
2. 存储数据面 = local-path，非真实存储池（§3）；
3. ~~CPU/存储节点控制台不显示~~ 已补：infra pool 视图（cpu01/02、stor01）；
4. 跨队列 reclaim 已根因闭环、判定非承重（gaps.md §7 终章），客户保障走 DIRECT 预留；
5. 量化网格参数是占位产品决策，上机前需和定价一起定稿。

---

## 8. 机器注册路径（2026-08-13 落地并测试）

「到货只需注册」的具体形态：`scripts/onboard-node.sh` + `runbooks/machine-registration.md`。
GPU 节点注册即进入 owner 状态机（自动建 NodeOwnership）；deregister 在**合同活跃或
租户 pod 在跑时拒绝执行** —— 注册脚本不是状态机的逃生通道。全流程由 NODE-01 用例
在每次矩阵运行中回归。唯一未自动化段：主机层（OS/容器运行时/kubeadm join），
见 runbook §1，上机前可选补 Ansible。
