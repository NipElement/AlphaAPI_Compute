# 硬件验收(HW acceptance)— Day-0 步骤 13 的可执行部分

offer 页**书面承诺** NVLink 全互联与双节点 XDR(16-GPU 任务)。这些承诺在 kind 上零验证
(`runbooks/gaps.md` §4),到货后必须用**真实数据**关闭。这里预建的是能立刻跑的验收任务与阈值,
不是又一份清单。

| ID | 验证什么 | 任务 | 通过标准(`hw-thresholds.env`) |
|---|---|---|---|
| HW-03 | 驱动/CUDA/8 卡可见 | `nccl-1node.yaml` | `torch.cuda.device_count()==8`,每卡有 HBM 容量 |
| HW-04/05 | 单节点 NVLink 全互联 all-reduce 带宽与正确性 | `nccl-1node.yaml` | busbw ≥ `NVLINK_BUSBW_MIN_GBPS`,结果逐元素正确 |
| HW-06 | 双节点 XDR all-reduce(16 GPU) | `nccl-2node.yaml` | busbw ≥ `IB_2NODE_BUSBW_MIN_GBPS`,且 NCCL 走 IB(`NCCL_NET=IB` 出现在日志) |
| HW-07 | 双节点正确性 | `nccl-2node.yaml` | 结果逐元素正确 |

```bash
make dgx-hw-accept DGX_KCTX=<ctx>                 # 1node 然后 2node;证据写到 evidence/RUN-dgx/hw-accept-*.json
KUBE_CONTEXT=<ctx> scripts/hw-accept.sh 1node    # 单独跑
```

前置:GPU Operator 已把 `nvidia.com/gpu` 广播到 4 节点(DGX-02..05),Volcano 已装(`make dgx-volcano`),
双节点任务还要 Network Operator 的 RDMA 资源(⟪DECIDE-D8⟫,`nccl-2node.yaml` 里的 `RDMA_RESOURCE`)。

**阈值是保守起点,标 ⟪DECIDE-WITH-VENDOR⟫**:NVLink 5 单卡 1.8 TB/s、8 卡 all-reduce 的 busbw 在 B200 上
公开数据约 450–500 GB/s,这里先要求 ≥ 300;双节点 XDR 8 rail × 800 Gb/s,ring busbw 受节点间带宽约束,
先要求 ≥ 150。**低于阈值不是"再跑一次",是先查 `nvidia-smi nvlink -s` / `ibstat` 链路速率。**
验收通过的数字连同 `nvidia-smi -q` 一起进证据目录,offer 页引用的就是这份。

镜像:`nvcr.io/nvidia/pytorch:25.06-py3@sha256:025d9b102b5436d4af8af58f12c6a46b7e5d16f19543b1d2cc4446bf2650b4f1`(versions.env `ACCEPTANCE_IMAGE`,由 registry-mirror.sh 进私有仓库)。
测试脚本是 torch.distributed 的 NCCL all-reduce(ConfigMap `hw-allreduce`),不依赖额外镜像。
命名空间 `hw-acceptance` 是 **PSA privileged**(双节点需要 IPC_LOCK + /dev/infiniband),只允许运维在验收窗口使用,
验收完 `kubectl delete ns hw-acceptance`——它不是租户,不受租户围栏保护。
