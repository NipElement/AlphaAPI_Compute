# 机器注册 Runbook（实机到货执行）

> 目标：新机器从上电到进入可调度池的完整路径。控制面部分（第 3 步起）
> 已由 NODE-01 用例在每次矩阵运行中验证 —— 到货那天执行的是**测过的**流程，
> 不是文档里的愿望。

## 0. 前置（一次性，整个集群）

- 控制面已按本仓库部署（`make cluster && make deploy` 的实机等价物：
  kubeadm 集群 + `platform/overlays/dgx` + Volcano + 监控）。
- **注意用 dgx overlay，不是 lab overlay**：dgx overlay 没有 fake-gpu，
  且准入策略方向相反（拒绝模拟资源、放行 `nvidia.com/gpu`）。

## 1. 主机层（每台机器，未自动化 —— 唯一的手工段）

| 步骤 | DGX (GPU) 节点 | CPU / 存储节点 |
|---|---|---|
| OS 基线 | DGX OS 出厂镜像，核对固件（HW-02） | Ubuntu 24.04，与 versions.env 同主版本 |
| 容器运行时 | containerd（DGX OS 自带，核对版本矩阵） | containerd |
| GPU 栈 | 按 §13.2 决定 GPU Operator 的 driver/toolkit 开关 | — |
| 加入集群 | `kubeadm join`（token 由控制面签发） | 同 |

> 这一段就是 gaps.md 里的「主机层未自动化」缺口。上机前若补 Ansible role，
> 从这里替换；不补则照此手工执行，每台约 30 分钟。

## 2. 加入后核验

```bash
kubectl get node <node>            # Ready
kubectl describe node <node> | grep -A5 Capacity   # 资源如预期（GPU 节点应有 nvidia.com/gpu=8）
```

## 3. 注册进机群（已测试路径）

```bash
# GPU 节点（进入 owner 状态机，初始 ARISE）
./scripts/onboard-node.sh <node> gpu dgx05 "05-06"

# CPU 池节点
./scripts/onboard-node.sh <node> cpu cpu03

# 存储节点（自动打 tenant 隔离污点）
./scripts/onboard-node.sh <node> storage stor02
```

脚本行为：打标签/污点；GPU 节点同时创建 NodeOwnership（`desiredOwner: ARISE`），
控制器随即接管 —— 从注册那一刻起，这台机器的归属就受状态机与准入策略约束。

## 4. 注册后验收

```bash
./scripts/verify.sh                # 完成门（节点数断言需按实际规模调整）
./tests/run.sh smoke               # P0 冒烟
```

GPU 节点另需过实机矩阵 HW-01..15（基线文档 §13.3）后才可承载客户负载。

## 5. 退役 / 换池

```bash
./scripts/onboard-node.sh <node> deregister
```

脚本会**拒绝**在以下情况执行：NodeOwnership 显示活动合同、节点上仍有租户 pod。
先走状态机（VAST 回收 / DIRECT 释放 / drain）再退役 —— 注册脚本不是逃生通道。

## 6. 8 节点扩展时的注意项

- pair 编号延续 `05-06`、`07-08`；XDR 全互联落地后按
  product-architecture.md §4 迁移到 HyperNode 拓扑，pair 降级为故障域标签。
- `system` 队列 capability 与租户配额按新卡数等比调整（quotas.yaml、volcano-queues.yaml）。
- verify.sh 的节点数断言（当前 8）随实际规模更新。
