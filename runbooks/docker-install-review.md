# Docker 安装审阅单（唯一需要你亲自执行的 sudo 步骤）

> 依据 WAIVER-2026-08-11-001 控制项 C-2：本机无快照回滚点，
> 唯一的系统级变更必须由你本人审阅后执行，不由自动化代理执行。

## 一、这一步会改动什么（全部 5 项，无其他）

| # | 改动 | 路径 | 可逆性 |
|---|---|---|---|
| 1 | 新增 Docker 官方 apt 源和 GPG key | `/etc/apt/keyrings/docker.asc`、`/etc/apt/sources.list.d/docker.list` | 删文件即可 |
| 2 | 安装 5 个包（**纯新增，无卸载**） | `docker-ce`、`docker-ce-cli`、`containerd.io`、`docker-buildx-plugin`、`docker-compose-plugin` | `apt-get purge` |
| 3 | 写 daemon 配置 | `/etc/docker/daemon.json` | 删文件即可 |
| 4 | 启用并启动 docker 服务 | systemd unit + `/var/lib/docker` | `systemctl disable --now docker` |
| 5 | 把 `ubuntu` 加入 `docker` 组 | `/etc/group` | `gpasswd -d ubuntu docker` |

**不会做的事**：不卸载任何既有包、不动防火墙规则（除 docker 包自带的 DOCKER-USER 链）、
不发布任何端口、不碰 `/var/lib/mongodb`、不碰 crontab、不碰任何现有项目目录。

## 二、已完成的前置核实

```
冲突包检查      docker.io / containerd / runc / podman-docker ... 全部 not-installed
                → 安装是纯新增，不需要任何卸载动作
版本锁          5:29.6.1-1~ubuntu.24.04~noble  已确认存在于官方 noble stable
                （仓库现有 29.7.2，按 plan §4.1「不追 latest」保持锁定）
passwordless    sudo 免密可用
磁盘            free 158.9 GiB，远高于 40 GiB 停止线
```

## 三、最关键的一项：CIDR 冲突规避

Docker 内置默认地址池是 **172.17.0.0/16 … 172.31.0.0/16**。
本机 VPC 子网是 **172.31.16.0/20** —— **落在这个池子里面**。

后果链条：docker 网络分配到 172.31.x → 与 VPC 路由冲突 → SSH 断开 →
**SSM 无法注册所以没有备用通道** → **没有 EBS 快照所以无法回滚** → 需要控制台救援。

因此 `daemon.json` 显式钉死地址池：

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "live-restore": false,
  "bip": "172.20.0.1/16",
  "default-address-pools": [ { "base": "172.21.0.0/16", "size": 24 } ]
}
```

- `bip` → docker0 固定在 172.20.0.1/16
- `default-address-pools` → 用户自定义网络（含 kind 的网络）只从 172.21.0.0/16 分配，256 个 /24
- 两者都远离 172.31.0.0/16，也不碰 pod 10.244.0.0/16 与 service 10.96.0.0/12

配置在**守护进程首次启动之前**写入，所以不存在"先用错网段再改"的窗口。
脚本在安装后会强制断言 `ip route` 中不存在落在 172.31.x 的 docker/br- 网络，
不满足即以非零码退出。

## 四、执行

```bash
cd /home/ubuntu/yuansheng/B300/alphaapi-compute

# 1) 先看一遍脚本本身
less scripts/install-docker.sh

# 2) 干跑，不改任何东西
./scripts/install-docker.sh plan

# 3) 确认无误后执行
./scripts/install-docker.sh apply

# 4) 使 docker 组生效（二选一）
newgrp docker          # 当前 shell 立即生效
# 或者退出重新登录
```

`apply` 会自动在开头和结尾各跑一次 `scripts/guard.sh check`，
任一保护路径被改动或磁盘跌破 40 GiB 即中止。

## 五、执行后请回报

```bash
docker version --format '{{.Server.Version}}'
ip -4 addr show docker0 | grep inet
ip route | grep -E 'docker|br-'
```

我需要确认 docker0 是 172.20.x 且没有任何 172.31.x 网络，然后才继续 Stage 2 建 kind 集群。

## 六、如果要撤销

见 `runbooks/rollback.md` 第 1 节，逐条对应上表 5 项改动。
