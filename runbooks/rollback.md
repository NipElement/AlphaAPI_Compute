# 回滚 Runbook（无快照场景）

> **前提**：WAIVER-2026-08-11-001 W-1 —— 本机**没有 EBS 快照回滚点**。
> 因此回滚是「逐项撤销」而非「卷级还原」。本文档按变更发生的逆序组织。
>
> **绝对禁止**（plan §11.7 + waiver C-3）：
> - `docker system prune -a`
> - 任何无范围限定的 `docker rm/rmi/volume rm`
> - `apt remove containerd`（本机 containerd 由 docker 引入，purge 时一并处理）
> - 触碰 `/var/lib/mongodb`、用户 crontab、或 C-5 清单中任何路径

每一步执行前后都应运行 `./scripts/guard.sh check`。

---

## 0. 快速判断该回滚到哪一层

| 症状 | 回滚层级 |
|---|---|
| kind 集群异常、平台组件跑不起来 | §2 仅重建集群 |
| Docker 网络/网段出问题、SSH 受影响 | §1.3 立即修网段 |
| 要彻底清退本次预演 | §2 → §1 → §3 |
| 现有业务（MongoDB/项目目录）受影响 | **立即停止一切操作**，见 §4 |

---

## 1. 撤销 Docker（对应 docker-install-review.md 五项改动）

### 1.1 先确认没有非本项目的容器
```bash
docker ps -a --format '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Labels}}'
```
若出现**不属于** `kind-b300-prelab` 或 `arise.project=b300-prelab` 的容器，
说明本机已有其他人在用 Docker —— **停止回滚并先沟通**。

### 1.2 删除本项目对象（仅按标签定向删除）
```bash
kind delete cluster --name b300-prelab
docker ps -a  --filter label=io.x-k8s.kind.cluster=b300-prelab -q | xargs -r docker rm -f
docker volume ls --filter label=arise.project=b300-prelab -q     | xargs -r docker volume rm
docker network ls --filter label=arise.project=b300-prelab -q    | xargs -r docker network rm
```

### 1.3 网段应急（若 docker 网络落到 172.31.x 导致 SSH 风险）
```bash
# 立刻停守护进程，恢复路由
sudo systemctl stop docker
ip route                       # 确认 172.31.x 冲突路由已消失
# 修正 /etc/docker/daemon.json 的 bip / default-address-pools 后再启动
sudo systemctl start docker
```
若 SSH 已断且无法登录：这是本豁免下的最坏情形，只能走
**AWS 控制台 → EC2 Serial Console / 或停机换卷**。这也是当初建议不要在本机做的原因。

### 1.4 卸载包（纯撤销上面第 2 项）
```bash
sudo apt-mark unhold docker-ce docker-ce-cli
sudo apt-get purge -y docker-ce docker-ce-cli containerd.io \
                      docker-buildx-plugin docker-compose-plugin
sudo apt-get autoremove -y
```

### 1.5 删除配置与数据目录
```bash
sudo rm -f  /etc/docker/daemon.json
sudo rm -f  /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc
sudo rm -rf /var/lib/docker /var/lib/containerd     # 仅本次预演产生
sudo gpasswd -d ubuntu docker || true
sudo apt-get update -qq
```

> `/var/lib/docker` 与 `/var/lib/containerd` 在本次预演前**不存在**
> （见 `evidence/<run_id>/preflight/docker-before.txt`），故整目录删除是安全的。

---

## 2. 只重建 kind 集群（不碰 Docker）

```bash
kubectl get nodeownership -A -o yaml \
  > evidence/$(cat .run_id)/inventory-after/nodeownership.yaml   # 先存证
kind delete cluster --name b300-prelab
./scripts/guard.sh check
make cluster                                                     # 重建
```

---

## 2.5 撤销 inotify sysctl 变更

```bash
sudo rm -f /etc/sysctl.d/99-arise-b300-prelab-inotify.conf
sudo sysctl -w fs.inotify.max_user_instances=128     # 原值
sysctl -n fs.inotify.max_user_instances              # 确认
```

> 该变更只是抬高上限、不预留资源，撤销前请确认没有其他工作负载已经依赖新上限。

---

## 3. 撤销用户级工具链

```bash
rm -f /home/ubuntu/.local/bin/{kind,kubectl,helm}
rm -rf /home/ubuntu/.kube                      # 仅当确认无其他集群配置
```

---

## 4. 现有业务受影响时的应急（P0 事故）

**立即停止所有变更**，然后：

```bash
./scripts/guard.sh check > evidence/$(cat .run_id)/security/incident-guard.txt 2>&1
systemctl is-active mongod; systemctl is-enabled mongod
sudo ls -la /var/lib/mongodb | head
crontab -l
df -hT /
```

- 对照 `evidence/<run_id>/security/guard-baseline.txt` 判断偏离项
- **不要**尝试自行"修复"MongoDB 数据；保全现场并上报
- 记录事故时间线到 `evidence/<run_id>/faults/`
- 依 plan §11.2，此类情况属「出现未知目标 / 活动数据被误动」→ 冻结并转人工

---

## 5. 回滚验证清单

| 项 | 命令 | 期望 |
|---|---|---|
| docker 已移除 | `command -v docker` | 无输出 |
| 无残留网络 | `ip route \| grep -E 'docker\|br-'` | 无输出 |
| 无残留目录 | `ls /var/lib/docker` | 不存在 |
| mongod 状态未变 | `systemctl is-active mongod; systemctl is-enabled mongod` | `inactive` / `disabled` |
| crontab 未变 | `crontab -l \| sha256sum` | 与 baseline 一致 |
| 保护路径未变 | `./scripts/guard.sh check` | exit 0 |
| 磁盘已释放 | `df -h /` | 回到 ~832G used |
