# AWS 只读盘点与快照 Runbook

> **在授权运维终端执行**（不是在这台 EC2 上 —— 本机无 IAM 角色、无凭据、无 aws CLI）。
> 执行完把 JSON 输出回传，用于关闭 PRE-02/03/04 与 AWS-01..05 这批 **BLOCKED** 门。
>
> 目标参数已填好，**不要改**：

```bash
export REGION=us-east-2
export INSTANCE_ID=i-REDACTED-PRELAB-HOST
export ACCOUNT_ID=REDACTED-ACCOUNT-ID
export RUN_ID=RUN-20260811-120339
export CHANGE_ID=CHG-20260811-001
mkdir -p ./preflight-aws
```

> 先确认身份，避免打到别的实例（plan §11.2 停止条件）：
> ```bash
> aws sts get-caller-identity --query Account --output text   # 必须等于 REDACTED-ACCOUNT-ID
> ```

---

## 1. 只读盘点（plan §6.3）

```bash
aws ec2 describe-instances --region $REGION --instance-ids $INSTANCE_ID \
  > ./preflight-aws/aws-instance.json

aws ec2 describe-volumes --region $REGION \
  --filters Name=attachment.instance-id,Values=$INSTANCE_ID \
  > ./preflight-aws/aws-volumes.json

aws ec2 describe-instance-status --region $REGION --include-all-instances \
  --instance-ids $INSTANCE_ID \
  > ./preflight-aws/aws-instance-status.json

aws ssm describe-instance-information --region $REGION \
  --filters Key=InstanceIds,Values=$INSTANCE_ID \
  > ./preflight-aws/aws-ssm-info.json
```

### 1.1 定向提取各门禁的判定字段

```bash
# AWS-01  IMDSv2 + hop limit（主机内已确认 HttpTokens=required；hop limit 只能从这里读）
aws ec2 describe-instances --region $REGION --instance-ids $INSTANCE_ID \
  --query 'Reservations[].Instances[].MetadataOptions' --output json
# 期望: HttpTokens=required, HttpPutResponseHopLimit=2 (容器场景), InstanceMetadataTags 按需

# PRE-03 / AWS-03  卷、加密、KMS、delete-on-termination
aws ec2 describe-volumes --region $REGION \
  --filters Name=attachment.instance-id,Values=$INSTANCE_ID \
  --query 'Volumes[].{Id:VolumeId,Size:Size,Type:VolumeType,Enc:Encrypted,Kms:KmsKeyId,
           Iops:Iops,Tp:Throughput,Dev:Attachments[0].Device,
           DoT:Attachments[0].DeleteOnTermination}' --output table
# 关注: Enc 是否为 true；Type 是否 gp3；DoT 对根卷通常为 true

# AWS-04  安全组实际入站规则（主机内看不到）
aws ec2 describe-security-groups --region $REGION \
  --group-ids $(aws ec2 describe-instances --region $REGION --instance-ids $INSTANCE_ID \
      --query 'Reservations[].Instances[].SecurityGroups[].GroupId' --output text) \
  --query 'SecurityGroups[].{Name:GroupName,Id:GroupId,In:IpPermissions}' --output json
# 关键判定: 是否存在 0.0.0.0/0 到 22/80/443/3000/6443/9090 的入站

# AWS-04  路由表 —— 关闭 PRE-05 的 CIDR 残留项
aws ec2 describe-route-tables --region $REGION \
  --filters Name=vpc-id,Values=$(aws ec2 describe-instances --region $REGION \
      --instance-ids $INSTANCE_ID --query 'Reservations[].Instances[].VpcId' --output text) \
  --query 'RouteTables[].Routes[].{Cidr:DestinationCidrBlock,Gw:GatewayId,Pcx:VpcPeeringConnectionId,Tgw:TransitGatewayId}' \
  --output table
# 判定: 是否存在覆盖 10.244.0.0/16 或 10.96.0.0/12 的路由（peering/TGW/VPN）

# PRE-02  SSM 在线状态
aws ssm describe-instance-information --region $REGION \
  --filters Key=InstanceIds,Values=$INSTANCE_ID \
  --query 'InstanceInformationList[].{Id:InstanceId,Ping:PingStatus,Agent:AgentVersion}' --output table
# 现状预期: 返回空列表（未注册）
```

---

## 2. 修复 SSM（可选但强烈建议）

当前 agent 日志：
```
EC2RoleProvider: no EC2 instance role found
AccessDeniedException: Systems Manager's instance management role is not
configured for account: REDACTED-ACCOUNT-ID
```

两条路，任选其一：

**A. 挂实例角色（标准做法）**
```bash
aws iam create-role --role-name AriseB300PrelabSSM \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam attach-role-policy --role-name AriseB300PrelabSSM \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam create-instance-profile --instance-profile-name AriseB300PrelabSSM
aws iam add-role-to-instance-profile --instance-profile-name AriseB300PrelabSSM \
  --role-name AriseB300PrelabSSM

aws ec2 associate-iam-instance-profile --region $REGION \
  --instance-id $INSTANCE_ID \
  --iam-instance-profile Name=AriseB300PrelabSSM
```
> `AmazonSSMManagedInstanceCore` 是 SSM 的最小托管策略，满足 plan §5.2「仅含 SSM」。
> 挂载后 agent 通常 1–5 分钟内注册；再跑一次上面的 `describe-instance-information` 确认 `Ping=Online`。

**B. 配置账号级 Default Host Management Configuration**
（对应日志里第二条报错，适合批量场景）——需在 SSM 控制台 Fleet Manager 启用，
或用 `aws ssm update-service-setting`。若你们后续 4 台 DGX 也要纳管，这条更省事。

> **修好 SSM 的价值**：本机目前唯一管理通道是 SSH，Docker 网段一旦出错就没有退路。
> 有了 SSM，`runbooks/rollback.md` §1.3 的最坏情形就有了第二条命。

---

## 3. 快照（关闭 PRE-04，建立真正的回滚点）

> ⚠️ 根卷 1 TiB / 已用 832 GiB。首次快照是全量，**耗时可能数十分钟到数小时**，
> 并产生存储费用。建议在业务低峰执行。快照是增量存储、崩溃一致性的。

```bash
VOLUME_ID=$(aws ec2 describe-volumes --region $REGION \
  --filters Name=attachment.instance-id,Values=$INSTANCE_ID \
  --query 'Volumes[0].VolumeId' --output text)
echo "VOLUME_ID=$VOLUME_ID"     # 预期只有一个根卷 (xvda)

aws ec2 create-snapshot --region $REGION \
  --volume-id $VOLUME_ID \
  --description "ARISE B300 prelab rollback point $CHANGE_ID" \
  --tag-specifications "ResourceType=snapshot,Tags=[
      {Key=project,Value=arise-b300-prelab},
      {Key=change_id,Value=$CHANGE_ID},
      {Key=run_id,Value=$RUN_ID},
      {Key=document_id,Value=ARISE-B300-PRELAB-DEPLOY-TEST-001},
      {Key=retention_days,Value=30},
      {Key=owner,Value=yuansheng@ariselabs.ai}]" \
  > ./preflight-aws/snapshot.json

SNAP_ID=$(jq -r .SnapshotId ./preflight-aws/snapshot.json)

# 等到 completed —— plan §6.4 要求必须等到 completed 才算数
aws ec2 wait snapshot-completed --region $REGION --snapshot-ids $SNAP_ID

aws ec2 describe-snapshots --region $REGION --snapshot-ids $SNAP_ID \
  --query 'Snapshots[].{Id:SnapshotId,State:State,Enc:Encrypted,Prog:Progress,Start:StartTime}' \
  --output table | tee -a ./preflight-aws/snapshot.json
```

> **MongoDB 一致性提示**：`mongod` 当前是 stopped 且 disabled，
> 最后一次为干净退出（exitCode 0），因此**此刻的快照对 MongoDB 数据是一致的**。
> 这是做快照的理想窗口 —— 如果之后有人启动了 mongod，就需要先做
> `db.fsyncLock()` 或应用级备份再快照（plan §6.4）。

### 3.1 恢复步骤（写入证据，未来真要用时照做）
1. 由快照创建卷：`aws ec2 create-volume --snapshot-id $SNAP_ID --availability-zone us-east-2b --volume-type gp3`
2. 停止实例：`aws ec2 stop-instances --instance-ids $INSTANCE_ID`
3. 卸载原卷：`aws ec2 detach-volume --volume-id $VOLUME_ID`
4. 挂新卷到 `/dev/xvda`：`aws ec2 attach-volume --volume-id <new> --instance-id $INSTANCE_ID --device /dev/xvda`
5. 启动实例并验证：`aws ec2 start-instances`，然后校验 mongod 数据与项目目录
6. 注意：stop/start 会更换公网 IPv4（当前 3.19.59.163 会变）

---

## 4. 回传给我

```bash
tar czf preflight-aws-$RUN_ID.tar.gz ./preflight-aws
```
把 `aws-instance.json`、`aws-volumes.json`、`aws-ssm-info.json`、`snapshot.json`
放到 `alphaapi-compute/evidence/$RUN_ID/preflight/` 下，我会据此把
PRE-02/03/04、AWS-01..05 从 **BLOCKED** 改判为 PASS/FAIL 并更新 `preflight-result.json`。

> 按 plan §10.2，在拿到这些输出之前，这些用例**必须保持 BLOCKED，不得记为 PASS**。
