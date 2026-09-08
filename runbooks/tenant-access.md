# 客户 SSH 与私有服务接入

客户使用内置 SSH 堡垒入口 `ssh.<域名>:2222`，凭自己的密钥连接开发机或在线服务，无需 Kubernetes 凭据。入口运行在独立 `access-system` 命名空间，用组织公钥绑定固定租户；开发机再用客户提供的工作负载公钥进行第二次认证。

支持交互 SSH、scp/sftp/rsync、IDE Remote SSH，以及本机 HTTP 服务隧道。在线服务通过隧道提供给已授权客户；这不是匿名公网推理 API。

## 运维启用

仓库中的 `platform/access/keys.yaml` 是可审查的公钥配置，默认空表。若部署信息不应随 Git 分发，先复制为 `platform/access/keys.local.yaml`（已忽略），以下所有命令通过 `ACCESS_KEYS` 指定同一个文件，独立验证命令使用 `--keys` 指定。私钥不得写入任一注册表。

1. 将 `services/devbox` 构建产物推送到生产仓库，以 `仓库/镜像@sha256:...` 写入 `platform/access/keys.yaml` 的 `image`。与已审查的开发机镜像相同，不在运行时安装软件。
2. 将 `hostname` 设置为真实 SSH 域名，DNS 指向头节点。在防火墙允许客户访问 TCP **2222**；管理 SSH 仍使用原有管理网络和端口 22。此入口仅监听 IPv4，域名不要发布不可达的 AAAA 记录。
3. 每个公钥登记 `tenant` 和 `publicKey`，租户必须存在于 `platform/tenants.yaml`。堡垒组织公钥仅接受 Ed25519，同一公钥不能跨组织复用。`expiresAt` 可设置 UTC 到期时间。私钥不进仓库，不交给平台。
4. 审查后部署：

```bash
make access-render ACCESS_KEYS=platform/access/keys.yaml
make dgx-access DGX_KCTX=arise-dgx ACCESS_KEYS=platform/access/keys.yaml
python3 scripts/verify-access.py --context arise-dgx --public
```

`--public` 将外部 SSH 端口提供的主机公钥与 Kubernetes 可信管理通道读到的公钥比较，不接受盲目信任扫描结果。完整上线检查 `make dgx-launch-verify` 也执行此门。自定义注册表路径时，给它传同一个 `ACCESS_KEYS=...`。

默认注册表没有客户公钥，渲染结果为 **0 副本**。移除最后一个公钥再部署会关闭监听，并等待旧 Pod 和连接退出。非空注册表在真实域名和 digest 未配置时拒绝部署。普通 `make dgx-platform` / `make dgx-code` 不启用或重写此入口。

Lab 使用 `make access ACCESS_KEYS=/安全临时路径/keys.yaml`，只创建 ClusterIP，不暴露主机端口；管理员可在 loopback 上临时 port-forward 该 Service 进行验证。生产使用 `hostPort: 2222`，不用 hostNetwork，保留 Calico 对 Pod 的网络策略。

## 交付主机身份

`access-system` 的 PSA 虽为 privileged（hostPort 2222 需要），但受 `arise-privileged-namespace-envelope`（`platform/base/privileged-namespaces-policy.yaml`）约束：只有堡垒 Deployment 的 ReplicaSet 控制器能在这里建 pod，管理员手工 `kubectl run` 也会被拒；pod 形状被钉死为非 root、无 privileged/hostPath/hostNetwork、hostPort 只有 2222。要在这里 `kubectl debug` 用 `--profile=restricted`；破窗只有删 binding 一条路（cluster-admin 操作，进审计日志）。

堡垒主机密钥保存在 retained PVC `access-system/tenant-bastion-host-keys`，重启和代码更新不会更换。运维通过可信管理通道读取**公钥**，交付给客户：

```bash
kubectl --context arise-dgx -n access-system exec deploy/tenant-bastion -- \
  ssh-keygen -y -f /keys/ssh_host_ed25519_key
kubectl --context arise-dgx -n tenant-acme exec box -- \
  ssh-keygen -y -f /keys/ssh_host_ed25519_key
```

客户将两条已核验的公钥分别写入 `~/.ssh/alphaapi_known_hosts`，格式如下，替换占位符：

```text
[ssh.example.com]:2222 ssh-ed25519 BASTION_PUBLIC_KEY
box.tenant-acme ssh-ed25519 MACHINE_PUBLIC_KEY
```

开发机的 `/keys` 当前为临时卷：**删除并重建开发机后，其主机密钥会变更**。平台需重新交付并核验该开发机公钥。不要让客户关闭 `StrictHostKeyChecking`，也不要把未经核验的 `ssh-keyscan` 输出直接当作可信身份。

## 客户连接开发机和传文件

把独立客户端 `services/ssh-bastion/client.py` 交付给客户，例如保存到 `~/bin/alphaapi-access.py`。只需要 Python 3 和 OpenSSH，不需要 Python 第三方依赖、平台管理员账号或 kubeconfig。工作负载公钥在「开发机」创建表单中填写；组织入口公钥由运维登记。两者可以使用不同密钥。

在客户的 `~/.ssh/config` 加入以下配置，替换域名、租户、机器名和绝对路径：

```sshconfig
Host alphaapi-box
    HostName box
    User dev
    IdentityFile /home/customer/.ssh/workload_ed25519
    IdentitiesOnly yes
    HostKeyAlias box.tenant-acme
    UserKnownHostsFile /home/customer/.ssh/alphaapi_known_hosts
    StrictHostKeyChecking yes
    ProxyCommand python3 /home/customer/bin/alphaapi-access.py proxy %h --host ssh.example.com --key /home/customer/.ssh/access_ed25519 --known-hosts /home/customer/.ssh/alphaapi_known_hosts
```

```bash
ssh alphaapi-box
scp model.bin alphaapi-box:/data/
rsync -av ./dataset/ alphaapi-box:/data/dataset/
```

`/data` 需创建开发机时申请数据卷；`/home/dev` 和 `/tmp` 是临时空间，删除开发机会丢失。IDE 可直接使用同一个 SSH Host 配置。

## 客户访问在线服务

在控制台创建服务 `model`，脚本在容器端口 8080 监听。客户端建立仅绑定本机 loopback 的隧道：

```bash
python3 ~/bin/alphaapi-access.py service model \
  --host ssh.example.com --key ~/.ssh/access_ed25519 \
  --known-hosts ~/.ssh/alphaapi_known_hosts --local-port 8080
curl --fail http://127.0.0.1:8080/
```

组织密钥只能访问该租户的服务名；不能提供 IP、命名空间、端口或 shell 命令。堡垒禁用 OpenSSH 的普通 TCP/X11/agent 转发、PTY 和用户 rc；只允许受限命令到固定 SSH/HTTP Service 端口。网络策略另行限制堡垒仅能访问已登记租户的开发机 2222、服务 8080 及集群 DNS，不能访问平台管理 API。

## 撤销、故障和容量

- **网页账号与 SSH 组织公钥独立管理**。删除网页账号、改网页密码不会撤销 SSH 组织密钥。人员离职需同时处理网页账号、堡垒密钥和开发机密钥。
- 修改入口公钥注册表后运行同一个部署命令。哈希变化触发 Recreate，切断所有旧堡垒连接，然后加载新配置；未撤销的客户可重新连接。有短暂中断。不要只 patch ConfigMap：授权文件采用 subPath 挂载，必须重建 Pod 才生效。
- `expiresAt` 限制新认证；已经建立的连接不会在该时刻自动退出。紧急撤销应部署更新或关闭入口。
- 全部紧急断开：`make dgx-access-off DGX_KCTX=...`。保留主机密钥 PVC。恢复前先核对注册表，避免重新启用已撤销密钥。
- 查看 `kubectl -n access-system logs deploy/tenant-bastion`。OpenSSH 输出认证公钥指纹，受限转发日志包含租户、目标、来源及连接/拒绝事件，不记录业务负载。应接入现有集中日志归档。
- 默认最多 32 个活动隧道，空闲 15 分钟、单连接最长 12 小时；本地服务客户端最多同时接入 16 个连接。到达上限明确拒绝，客户重连即可。该单实例入口没有高可用承诺，扩大规模前需做负载与故障演练。
- 主机密钥 PVC 纳入加密离机备份；丢失后不能假装还是旧主机。恢复备份或重新交付可信公钥。Retain 不代表异机副本。

验证命令：`python3 tests/test_bastion.py`、`python3 tests/live_bastion.py`（隔离容器）、`python3 tests/live_bastion_cluster.py`（仅 kind，拒绝覆盖已启用入口，结束后清除测试密钥并关闭入口）。真实公网 DNS、主机防火墙和公网可达性需要在生产部署时再验收。
