# 事故:计量与台账(钱路径)

> 台账是客户欠费的**唯一**记录。这里每一条的处置都以 2026-08-30 的实测行为为准,
> 不是按设计意图猜的。触发者是 `arise-billing` 告警组(platform/overlays/dgx/monitoring.yaml)。

## 先看清事实(任何一条告警都先做这三步)

```bash
K="kubectl --context $DGX_KCTX"
$K -n platform-system get pod -l app.kubernetes.io/name=metering -o wide
$K -n platform-system logs deploy/metering --tail=50 | grep -v '"level": "INFO"'
$K -n platform-system exec deploy/metering -- python3 -c "
import urllib.request,json; d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/ledger'))
print('chain_ok',d['chain_ok'],'broken_at',d['broken_at'],'records',d['total'])"
# 锚点(Prometheus 里的历史 head,计量进程无法改写):
$K -n monitoring exec deploy/prometheus -- wget -qO- \
  'http://127.0.0.1:9090/api/v1/query_range?query=arise_metering_ledger_head_info&start=<24h前 unix>&end=<现在 unix>&step=300' | head -c 2000
```

## MeteringDown —— 计量没在被抓取(critical)

**含义**:停摆期间发生的用量**不在台账里**。运行中的 pod 不丢(恢复后仍按 `status.startTime` 开账,实测确认),
**在停摆窗口内起止的短命 pod 永久丢失**。

1. 先判断是"进程死"还是"没抓到":`up{job="metering"}` 为空 = target 不存在 → **Prometheus 配置没重载**
   (2026-08-30 lab 就是这个:ConfigMap 改了、进程没重载,metering 从未被抓取过)。
   处置:`$K -n monitoring exec deploy/prometheus -- wget --post-data='' -qO- http://127.0.0.1:9090/-/reload`,
   然后 `make dgx-verify` 的 DGX-33 应转绿。
2. 进程真死:看 PVC(`metering-ledger`)是否挂得上、节点是否 `/raid` 缺失(见 RaidMountMissing)。
3. **对账**:窗口内仍在运行的 pod 由计量自动补齐;已结束的用当期
   `kube_pod_status_phase` / 审计日志估算,**按对客户有利的方向取整并在发票上标注**;不要静默补记。

## LedgerChainBroken —— 链断(critical)

**含义**:某条记录不再哈希到前一条。可能是损坏,也可能是篡改。

1. **先快照再动手**:`$K -n platform-system exec deploy/metering -- cat /ledger/allocations.jsonl > ledger-$(date -u +%FT%TZ).jsonl`
   (存到 evidence,双人见证)。
2. `billing/invoice.py` 会以 **exit 3** 拒绝开票(`--allow-broken` 会逐行标注,仅用于取证)。
3. 定位:`/ledger` 返回的 `broken_at` 是行号;该行**之后**的记录都不可信。
4. **判断是损坏还是篡改**:把当前 head 与 Prometheus 里 24h/7d 前的
   `arise_metering_ledger_head_info` 比对——历史 head 由 Prometheus 保存(计量进程改不了它)。
   历史某点的 head 与按当前文件重放到同一 seq 得到的 head 不一致 ⇒ 历史被改写过。
5. **先排除"模式变了"这一种**(最常见且最容易误判成篡改):
   `arise_metering_ledger_chain_mode` 与账本被写入时的模式必须一致。
   典型场景:`metering-chain-key` Secret 被删/被换 → 计量以 `sha256` 模式重启,
   并**继续往 HMAC 账本上追加**(记账不停,否则会丢钱;`chain_ok=0` 会在 1 分钟内告警)。
   处置:**恢复原密钥**(密码库),重启计量,链即自洽;**不要**用新模式重算整条链——
   那正是攻击者要做的事,做完就再也分不清谁改过。若原密钥确实丢失:归档当前文件(连同它的
   `arise_metering_ledger_records` 与最后可信 head),另起新账本,并在发票上注明分段。
6. 恢复:从最近的一致快照 + 之后的 pod 事实重建;记录 `approvedBy` 与差额。

> **链能证明什么、不能证明什么(2026-08-30 实测)**:改字段/删记录/换序/惰性拼接都能定位;
> 尾部撕裂会被容忍截断;但**任何知道算法(算法在仓库里)且能写这个文件的人,重算整条链后无法被文件本身发现**
> ——实测把一条外来记录拼进去并重算链,账单从 $114.84 变成 $51,563.16 而 `chain_ok` 仍为 true。
> 因此:**外部锚点(Prometheus 的 head 历史)是这条链唯一的防篡改依据**,开票前必须比对。

## LedgerShrank —— append-only 的文件变短了(critical)

只可能是:有人删了记录、PVC 被换掉、或从旧快照回滚。**立刻停手**:快照 PVC、停止一切写入
(`$K -n platform-system scale deploy/metering --replicas=0`,计量停摆期间的用量按上面的对账流程补),
用锚点确定被截掉的区段,恢复后再拉起。

## MeteringPollErrors —— 轮询/写入失败(warning)

实测:`tick()` 抛异常 → 主循环记 `arise_metering_poll_errors_total` 并继续。
最常见原因是**台账 PVC 满或只读**:此时运行中的 pod 不丢钱,但起止都落在故障窗口里的 pod 完全无记录。
处置:扩容 PVC 或归档已结账的月份——**归档用复制,绝不原地截断**(截断会触发 LedgerShrank,且破坏 seq 连续性)。

## MeteringSeesNothing —— 计量活着但看不见租户(critical)

租户有 Running 的 pod,计量却零开放区间。查:`platform-tenants` ConfigMap 里的租户列表、
metering 的 RBAC(需要 pods + persistentvolumeclaims 的 get/list/watch)、`GPU_RESOURCE` 环境变量
(dgx 必须是 `nvidia.com/gpu`;若还是 `arise.dev/fake-gpu`,所有 pod 的 footprint 都是 0 → 不记账)。

## LedgerVolumeFilling —— 台账卷快满(warning)

见 MeteringPollErrors 的处置。容量参考:一条记录约 400 字节,一个 pod 两条;10 GiB ≈ 1300 万条。
真正撑满它通常意味着有人在刷 pod,一并查配额。

## 锚点:唯一能发现"历史被改写"的东西

链本身只能证明"这份文件内部自洽"。**能证伪改写的只有一个外部数字:某个时刻的链头**。
它由 Prometheus 保存(计量进程无权写),`arise-billing` 组的告警也盯着它。

```bash
# 取"当前"链头(计量自己报的)
$K -n platform-system exec deploy/metering -- python3 -c "
import urllib.request
print([l for l in urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode().splitlines()
       if l.startswith('arise_metering_ledger_head_info')])"

# 取"历史"链头(Prometheus 留存的,才是锚点;查上个月末那一刻)
$K -n monitoring exec deploy/prometheus -- wget -qO- \
  'http://127.0.0.1:9090/api/v1/query?query=arise_metering_ledger_head_info&time=<月末 unix 秒>'
#   → 返回 {head="<前 16 位>"} <seq>;把完整 head 从当期账本里按 seq 取出并核对
```

**注意**:指标里的 head 是完整 64 位十六进制(2026-08-30 起;此前是前 16 位,足以被蓄意碰撞磨出来);
月结时把 `(seq, 完整 head, 时间)` 写进 evidence——**那一行就是下个月的锚点**。

取出密钥去验账(必须与 pod 里看到的**字节完全一致**,否则会误报"链断"):

```bash
$K -n platform-system get secret metering-chain-key -o jsonpath='{.data.key}' | base64 -d > /secure/chain.key
# 校验:pod 里算出的链头与 invoice.py 用这把钥匙算出的一致
billing/invoice.py --ledger <snapshot> --chain-key-file /secure/chain.key ...
```

链模式:`arise_metering_ledger_chain_mode{mode=...}`。硬件上必须是 `hmac-sha256`
(`make dgx-ledger-key`,DGX-37 会卡);lab 是 `sha256`。
**密钥丢失 = 该密钥期内的账本永远无法验证**,所以创建时就要进密码库。

## 每月开票前的例行核对(不是可选项)

```bash
# 1. 链完整 + 锚点一致
# 2. 期末 head/seq 记进 evidence(下个月的锚点)
billing/invoice.py --ledger <snapshot> --pricebook billing/pricebook.yaml \
  --tenants platform/tenants.yaml --tenant <ns> --from <月初> --to <月末> \
  --chain-key-file <密码库里的密钥文件> \   # 硬件上必带:少了它就是在验一条可伪造的链
  --expect-seq <上月末记录的 seq> \
  --expect-head <上月末记录的完整 head>     # 锚点不符 / 变短 → exit 3,不出账
```

三种拒绝的含义:
- **exit 3 + CHAIN BROKEN@n**:第 n 条起自洽性断了(损坏、惰性篡改,或**用错了链模式/密钥**)。
- **exit 3 + ANCHOR MISMATCH**:文件内部自洽,但与外部锚点不符 ⇒ **历史被改写/替换**。这是最严重的一种。
- **exit 2 + NOT PRICED**:价格本在该时段没有生效费率(DGX-35 在 Day-0 就会卡住这一条)。

```bash
# 出账后:把本期末的 (seq, head) 记进 evidence,作为下期锚点
# 3. 出现 NOT PRICED 行 = 价格本在该时段没有生效费率(exit 2),先修价格本再开票
```
