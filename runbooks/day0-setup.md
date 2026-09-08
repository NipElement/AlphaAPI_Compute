# DGX 部署与验收顺序

从仓库根目录运行以下步骤。管理机需具备本仓库要求的工具、镜像仓库访问和目标集群管理权限；节点初始化命令在对应物理主机运行。命令里的 `<...>` 是待填写参数，不能整段原样执行。

先确认 [生产就绪清单](../docs/production-readiness.md) 和 [D1–D8 决策](../docs/decisions-D1-D8.md)，核实头节点 `/raid`、节点地址、仓库及硬件配置。版本由 `versions.env` 与相应清单锁定；修改版本应同时更新配置并跑门禁。真实 GPU、NVLink、NCCL、IB/RDMA 和数据盘必须在目标硬件验收。

```bash
make check                  # 源码与配置检查，包含前端构建
make new-run                # 为本次部署创建新的证据目录
```

以下按依赖顺序进行。`make dgx-test` 会为完整矩阵另开测试 campaign，检查输出的路径；封存该测试 campaign，同时保留前面的部署记录。只重跑单个用例不会替代完整验收。

```bash
# 0. 管理机:构建自有镜像,起私有 registry(D1 决定它落在头节点还是别处),全部 pinned 镜像进 mirror
make web-image && make devbox-image                       # arise/web、arise/devbox(本机 docker)
REGISTRY=<registry:port> ./scripts/registry-mirror.sh     # digest 逐个相等否则失败;打印 arise/* 的新 digest
                       #    也镜像 NVIDIA GPU/Network Operator 的整套镜像(infra/dgx/operators/operator-images.lock,
                       #    make validate 的 16/16 校验它与 versions.env、values、NicClusterPolicy 一致);
                       #    版本升级后先 make operator-images-resolve 再按 check 的提示钉 digest
#    → 把打印的 digest 写进 platform/overlays/dgx/kustomization.yaml(images:)与 tenant-portal.yaml(DEVBOX_IMAGE)
#    → 明文 HTTP registry 需要 docker daemon 的 insecure-registries(rehearsal 用 127.0.0.1:5001 天然豁免)
# 1. 每台节点(root;sudo 会丢环境变量,所以用 env 显式传):
sudo env KUBE_VERSION=v1.36.2 REGISTRY_MIRROR=<registry:port> infra/dgx/node-bootstrap.sh head   # 头节点(/raid 必须已挂载)
sudo env KUBE_VERSION=v1.36.2 REGISTRY_MIRROR=<registry:port> infra/dgx/node-bootstrap.sh gpu    # 4 台 DGX
# 2. 头节点:把 kubeadm-cluster-config.yaml 里两处 REPLACE_WITH_HEAD_NODE_IP 都填成同一个地址。
#    render 门无条件拒绝「只填了一处」和「两处不一致」;两处都还是占位时只 WARN(D1 未定时属正常),
#    填完后用 DGX_KUBEADM_FILLED=1 把它变成硬失败(见第 5 步)。然后
sudo install -m 0644 infra/dgx/audit-policy.yaml /etc/kubernetes/audit-policy.yaml
sudo make dgx-etcd-encryption-key    # 2b. Secret 静态加密的钥匙(只打印一次,记进密码库)。
                                     #     没有它 API server 起不来;有它,被 scp 走的 etcd
                                     #     备份就不再是「一条 strings 就能拿到台账钥匙」
sudo kubeadm init --config infra/dgx/kubeadm-cluster-config.yaml
#    按 kubeadm 输出在可信管理机配置 kubeconfig；不要提交 Git 或交付客户。
export DGX_KCTX=arise-dgx    # 替换为已配置的生产 context 名称
make dgx-cni           # 3. vendored Calico(digest 固定、pod CIDR 预设);此前节点 NotReady
#    4. 4 台 DGX:粘贴 kubeadm join
make dgx-approve-csrs  # 4b. 批准 kubelet serving 证书 CSR(否则 logs/exec 与 DGX-28 报 TLS 错)
DGX_KUBEADM_FILLED=1 make dgx-render   # 5. 静态门:清单本身是否可以安全 apply(含 kubeadm 占位符/一致性/版本/CIDR 交叉检查)
make dgx-platform      # 6. CRD + 命名空间 + 策略(先于凭据:Secret 需要 platform-system 存在)
make dgx-onboard       # 7. onboard-node.sh gpu ×4:node-id/pair/role 标签 + NodeOwnership(DGX-02..05、node_for 都靠它)
                       #    默认 DGX_HOSTS="dgx01 dgx02 dgx03 dgx04";主机名不同时 DGX_HOSTS="h1 h2 h3 h4"
make dgx-gateway-secret # 8. 随机生成网关凭据,只打印一次
make dgx-ledger-key    # 8b. 台账链的 HMAC 钥匙(随机,只打印一次,记进密码库)。
                       #     跳过它 = 台账是无钥匙链,谁能写文件谁就能伪造历史;
                       #     DGX-37 会红,但一个只以「门变红」形式存在的步骤,
                       #     总是在最糟的时候才被发现
make dgx-deploy        # 9. render 门 -> 哨兵镜像检查 -> overlay -> 代码/注册表 ConfigMap -> vendored Volcano(控制面放头节点)
# 10. GPU Operator / Network Operator:helm,values 在 infra/dgx/operators/(填 ⟪DECIDE⟫;driver.enabled 看实机——
#     若改成 true,driver.version 必须填 operator-images.lock 里对应 DGX OS 的 driver digest,check 会拒绝标签或不在锁里的 digest);
#     组件镜像已按 digest 钉在 values / NicClusterPolicy 里,Operator 自身镜像由 mirror 按标签+digest 校验,DGX-31/32 核对运行中的 digest;
#     先 kubectl -n gpu-operator create configmap arise-dcgm-metrics --from-file=dcgm-metrics.csv=infra/dgx/operators/dcgm-metrics.csv;
#     Network Operator 的真实配置是 infra/dgx/operators/nic-cluster-policy.yaml(NicClusterPolicy CR),operator 起来后再 apply
#     直到这一步之前 nvidia.com/gpu=0,dgx-verify 的 DGX-03/04/05 必红——所以 verify 放在它后面
make dgx-verify        # 11. 生产部署完成门(真 GPU 在、模拟资源为零、门禁齐备、CSR 已批、围栏实测、Volcano/Calico 就绪)
make dgx-test          # 11b. OVERLAY=dgx 矩阵；lab 专属用例显式 SKIPPED，逐条复核 results.json
make dgx-hw-accept     # 12/13. 硬件验收:NVLink 单节点 + XDR 双节点 all-reduce,按 infra/dgx/acceptance/hw-thresholds.env 评分
make dgx-alert-receiver WEBHOOK_URL=https://...   # 14. 接真实 pager(DGX-22 从 WARN 变 PASS)
# 14b. 按 runbooks/tenant-access.md 复制并填写 keys.local.yaml，再启用和验收 SSH 入口
make dgx-access DGX_KCTX="$DGX_KCTX" ACCESS_KEYS=platform/access/keys.local.yaml
make dgx-launch-verify ACCESS_KEYS=platform/access/keys.local.yaml # 收钱前的门:LAUNCH=1 把「Day-0 期间正常、上线后致命」的几项(空 pager、
                       #      day0 哨兵镜像、GPU 健康无人观测)从 WARN 提为 FAIL。必须 failed=0
make dgx-restore-drill  #      备份从「有」到「恢复过」：把最新 etcd 快照恢复到临时目录并读回，不停任何东西。只在 etcd-backup 跑过一次(6h 节奏)之后才有快照可演练；见 runbooks/etcd-restore.md
# 15. 切流（填写 Caddy 域名/邮箱并完成 runbooks/public-edge.md 前置项后）:
make dgx-edge          #     edge/ 与网关 GW_TRUST_PROXY/GW_COOKIE_SECURE 一步同翻(DGX-26);回退 make dgx-edge-off
```

## 证据封存

矩阵结果在本次 `evidence/$(cat .run_id)/`；硬件脚本输出到 `evidence/RUN-dgx/hw-accept-*`，完成门默认写 `evidence/RUN-dgx/verify-dgx.json`（后一次会覆盖前一次）。在切流后再次执行 launch 检查，按输出时间与 context 选取本次硬件和最终完成门结果，复制到当前测试 campaign，同时归档此前的部署记录，再封存：

```bash
make evidence-seal
make evidence-verify
```

封存前核对没有遗漏硬件结果或混入旧运行，封存后不再向该 campaign 写入。后续验收另开新 campaign。

## 切流后与变更

按 [公网入口](public-edge.md) 复核真实 HTTPS、cookie、登录退出与跨租户拒绝，按 [客户接入](tenant-access.md) 验证外部 SSH 主机身份和私有服务隧道。再次运行 `make dgx-launch-verify`，自定义公钥注册表必须使用同一个 `ACCESS_KEYS`。检查结果中的 WARN、BLOCKED、SKIPPED，不能按进程退出码替代人工验收。

后续发布和回退见 [升级与回滚](upgrade-rollback.md)，新增客户见 [客户入驻](customer-onboarding.md)。切流后更新平台必须使用 `make dgx-platform` 的渲染路径，以保留网关的公网安全配置。
