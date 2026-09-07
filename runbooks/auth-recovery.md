# 账号持久化、备份和恢复

网关使用 `/var/lib/arise-auth/auth.sqlite3`，挂载 retained PVC `platform-gateway-auth`。账号、身份版本、退出撤销记录均在 SQLite 提交后才返回成功。WAL、进程写锁和单副本 Recreate 共同保证一个写入者。不要水平扩容这套 SQLite 网关；HA 需要外部身份库或 IdP。

种子账号由 Kubernetes Secret 管理，轮换其密码应更新 Secret 后重启网关；控制台新增账号和改密不会因正常重启丢失。备份也包含密码哈希，按敏感凭据处理。

## 备份

`auth-backup` CronJob 每 6 小时通过 SQLite backup API 生成一致快照，校验 integrity_check 和账号结构，输出 SHA-256，默认保留最近 120 份。`platform-auth-backups` PVC 使用 Retain。两块 PVC 在同一头节点上，**不能抵御头节点或整块存储损毁**；上线前必须复制到独立故障域并实际试恢复。离机文件须加密、限制访问、保留独立的哈希记录。

```bash
kubectl --context arise-dgx -n platform-system create job auth-backup-manual-YYYYMMDD --from=cronjob/auth-backup
kubectl --context arise-dgx -n platform-system wait --for=condition=Complete job/auth-backup-manual-YYYYMMDD --timeout=120s
kubectl --context arise-dgx -n platform-system logs job/auth-backup-manual-YYYYMMDD
```

使用唯一 Job 名。不能用 `cp` 复制运行中的 auth.sqlite3：已提交的数据可能还在 WAL 文件中。手工导出的副本应使用 `services/auth-backup/auth_backup.py --source ... --destination ...`。

## 恢复

1. 暂停公网入口，缩容网关至 0，确认旧 Pod 已删除。暂停 auth-backup CronJob，避免恢复时备份半成品。
2. 从独立备份记录核对 SHA-256，在隔离环境执行 `python3 services/auth-backup/auth_backup.py --verify BACKUP.sqlite3`。确认时间点和受影响账号；验证完整性不能证明备份来自可信来源。
3. 通过只在头节点运行的临时维护 Pod 挂载账号 PVC。把现有 `auth.sqlite3`、`auth.sqlite3-wal`、`auth.sqlite3-shm` **一起归档**供调查，然后安装选定备份为 `auth.sqlite3`（uid/gid 65532、0600），恢复目录中不要留下旧 WAL/SHM。禁止在网关运行时进行这些操作。
4. **恢复启动前生成新的 `GW_SESSION_KEY`，更新网关 Secret。** 恢复点之后的登出撤销可能不存在于旧备份；轮换签名密钥使所有旧 cookie 失效，要求全部用户重新登录。不要把密码或密钥放进 shell 历史、工单或 Git。
5. 恢复网关副本数 1，等待 Ready。验证管理员、一个恢复的客户账号、新登录及旧 cookie 失效，核对恢复点之后新增/删除的账号；必要时重新禁用已离职账号并重新发放凭据。
6. 恢复备份 CronJob，手动生成并校验一份新备份，再按 `public-edge.md` 恢复公网入口。

`tests/live_readiness.py` 已覆盖真实 kind Pod 重启、账号与退出撤销持久化、实际 CronJob 备份和读回；`tests/test_production_regressions.py` 覆盖快照恢复后密码校验。它们不能替代真实离机介质、Secret 密钥轮换与整机故障的恢复演练。
