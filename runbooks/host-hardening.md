# DGX 主机加固基线(Day-0 步骤 2/4 的主机侧)

> `infra/dgx/node-bootstrap.sh` 落地的是**自动化的那一半**(sysctl、swap、containerd、
> RAID 哨兵、sshd key-only)。本基线写全"应当为真"的清单,分**已自动化 / 手工一次 /
> 到货才定**三栏,这样验收时逐行打勾,而不是凭印象。

## 已由 node-bootstrap.sh 落地(可重复执行)

| 项 | 内容 | 验证 |
|---|---|---|
| sshd | 仅公钥;root 禁密码;无 X11;MaxAuthTries 4 | `sshd -T \| grep -E 'passwordauth\|permitroot'` |
| 内核 | br_netfilter / ip_forward / inotify 上限 | `sysctl net.ipv4.ip_forward fs.inotify.max_user_watches` |
| swap | 关闭并注释 fstab | `swapon --show` 为空 |
| containerd | SystemdCgroup=true | `grep SystemdCgroup /etc/containerd/config.toml` |
| GPU 节点存储 | `/raid` 是挂载点 + `.raid-verified` 哨兵 | `mountpoint /raid && ls -la /raid/arise/volumes/.raid-verified` |
| 头节点 | 审计日志目录 0700、etcd 备份目录 0700 | `stat -c %a /var/log/kubernetes/audit /var/lib/arise/etcd-backups` |

## 手工一次(每台,到货当天)

| 项 | 做法 | 为什么 |
|---|---|---|
| 运维账号 | 每人一个账号 + 各自公钥,`sudo` 走 `%arise-ops` 组;**删默认账号密码** | 审计要能指到人 |
| 主机防火墙 | `nftables`/`ufw`:GPU 节点入向只放 运维网段→22、集群网段→kubelet 10250 / NodePort 段(若用)/ pod 网络;头节点另加 6443(仅运维网段)与 80/443(切流后) | 租户 pod 到宿主机的路径靠 NetworkPolicy,宿主机自身靠这层 |
| BMC | 独立管理 VLAN;改默认口令;关闭 BMC 的 IPMI-over-LAN 公网可达 | BMC 是整机的根 |
| 自动更新 | `unattended-upgrades` **只装安全更新且不自动重启**;内核/驱动升级走 `runbooks/upgrade-rollback.md` 层 2(MAINTENANCE) | 自动重启一台正在售的 DGX = 事故 |
| 时间 | chrony 指向内网 NTP;`timedatectl` 确认 UTC | 计量台账与审计日志的时间戳 |
| 日志 | `journald` 持久化(`Storage=persistent`,`SystemMaxUse=4G`) | 事故复盘需要节点侧日志 |
| auditd(头节点) | 记 `/etc/kubernetes`、`/var/lib/etcd` 的写入 | apiserver 审计管 API,这层管文件 |
| 磁盘加密 | ⟪DECIDE-D2/D7⟫ 租户 NVMe 是否 dm-crypt(影响性能与"数据丢失责任"的披露) | 属决策 |

## 到货才定(需要看到实机)

- NVIDIA 驱动归属(DGX OS 自带 vs Operator,`operators/gpu-operator-values.yaml`)
- MOFED/DOCA 归属与 IB 织物(D8)
- 固件基线快照:`nvidia-smi -q \| grep -i firmware`、`ipmitool mc info`、
  `mlxfwmanager --query` → 存入 `/var/lib/arise/evidence/`(bootstrap 已建目录)

## 验收方式

每台节点跑一次 `node-bootstrap.sh <role>`(幂等)并把输出存 evidence;手工项逐行
在变更单打勾;之后 `make dgx-verify` + `make dgx-test`。任何一行不为真,该节点
不进 `onboard-node.sh`(不进池)。
