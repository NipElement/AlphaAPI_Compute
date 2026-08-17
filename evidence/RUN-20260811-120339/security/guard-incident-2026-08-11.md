# Guard 误报事件记录 — 2026-08-11

| 字段 | 内容 |
|---|---|
| 时间 (UTC) | 2026-08-11 ~12:15 |
| run_id | RUN-20260811-120339 |
| 触发 | `scripts/guard.sh check` 退出码 3「PROTECTED ASSET CHANGED」 |
| 结论 | **误报（false positive）。无任何受保护资产被改动。** |
| 处置 | 修正 guard 指纹算法并重新基线；本记录随证据包留存 |

## 现象

六个受保护目录指纹同时变化：
`final_release`、`yuansheng`、`hengxin`、`xueguang`、`data_online`、`mongo_debug_data`。

`/var/lib/mongodb`、`/var/log/mongodb`、`/mnt/backup`、`/mnt/backup2` **未**变化。

## 根因

`fingerprint_dir()` 原用 `ls -la`。`-a` 会输出 `..` 条目，
而 `..` 行携带的是**父目录**的 mtime。

上述六个目录的父目录都是 `/home/ubuntu`；未变化的四个父目录是 `/var/lib` 与 `/mnt`。

实测：

```
/home/ubuntu   mtime = 2026-08-11 08:12:34 EDT   <-- 会话中被改动
/var/lib       mtime = 2026-05-27 17:13:30 EDT
/mnt           mtime = 2026-05-04 02:25:03 EDT
```

`/home/ubuntu` 的 mtime 在会话期间变化，是因为活跃的 VS Code Remote /
Claude Code 进程持续在其下写入（`.claude/`、`.cache/`、`.local/`、
shell snapshot 等）。这与 B300 预演无关，也与受保护数据无关。

## 受保护目录自身 mtime（证明内容未变）

```
/home/ubuntu/final_release       2026-02-21 07:39:40
/home/ubuntu/hengxin             2026-04-07 15:49:33
/home/ubuntu/xueguang            2026-03-15 20:31:58
/home/ubuntu/data_online         2026-03-25 00:19:44
/home/ubuntu/mongo_debug_data    2026-04-09 23:35:00
/home/ubuntu/yuansheng           2026-08-11 07:43:37   (早于 08:03 基线捕获)
/var/lib/mongodb                 2026-07-27 02:26:01   (mongod 干净退出时刻)
```

全部早于本次基线捕获时刻，`/var/lib/mongodb` 仍停在 mongod 关闭的那一刻。

## 修正

`ls -la` → `ls -lA`（almost-all，排除 `.` 与 `..`），指纹只覆盖目录**内容**。

## 为什么值得单独记一笔

一个会误报的守卫会被忽略，比没有守卫更危险 —— 尤其在本机
**没有 EBS 快照回滚点**、且同盘存放 735 GB 生产数据的情况下，
guard 是唯一的自动防线。误报必须当作缺陷修掉，而不是靠人记住"这条可以忽略"。

同时保留本记录，是为了让证据包如实反映"guard 曾经报警过、原因是什么、如何处置"，
而不是让基线看起来从未出过声。

---

# 第二次触发 — 同日 12:32 UTC

| 字段 | 内容 |
|---|---|
| 触发 | `install-docker.sh` step 0 的 guard check，exit 3 |
| 变更路径 | `/home/ubuntu/yuansheng` |
| 结论 | **真实变更，但非本预演所致，且不应阻塞** |

## 根因

`/home/ubuntu/yuansheng/AlphaAPI` 的 mtime 为 `2026-08-11 08:31:28 EDT`，
晚于基线捕获时刻 `08:29:40 EDT`。即有人/某进程正在该目录下正常工作。

`B300`(08:03:39) 与 `b300-prelab`(08:27:13) 均早于基线，**不是本仓库写入造成的**。

## 设计缺陷

把**活跃的人类工作目录**与**不可替代的生产数据**放进同一个硬停层，是原设计的错误。

后果：同事编辑无关项目 → 预演被拦停 → 操作者学会绕过 guard → guard 形同虚设。
在本机（无快照、735 GB 生产数据同盘）这是不可接受的失效模式。

## 修正：两层结构

| 层 | 路径 | 行为 |
|---|---|---|
| **CRITICAL** | `/var/lib/mongodb`、`/var/log/mongodb`、`/mnt/backup`、`/mnt/backup2`、mongod active/enabled 状态、27017 监听、用户 crontab | 任何差异 **硬停 exit 3** |
| **OBSERVED** | `final_release`、`yuansheng`、`hengxin`、`xueguang`、`data_online`、`mongo_debug_data` | 写入 `observed-activity.log`，**不阻塞** |

另：本仓库自身子树从指纹中排除 —— guard 对自己的写入做出反应是没有意义的度量。

真正要保的安全属性「预演不能悄悄损坏数据库或备份」由 CRITICAL 层完整保留。

## 修正后验证

```
无改动                  exit 0
CRITICAL 被篡改         exit 3   <-- 仍然硬停
CRITICAL 恢复           exit 0
OBSERVED 被改动         exit 0   <-- 不阻塞
OBSERVED 变更被记录     是
```
