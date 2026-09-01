# Day-0 切流:从 port-forward 到公网(步骤 12)

> 前置:D4 已拍板(域名、边缘形态、DDoS 姿态)。本 runbook 把拍板结果变成
> 一串**有序且可回退**的动作。每一步都有"验证"与"回退";没验证过不进下一步。

## 0. 前置状态(切流前必须为真)

- `make dgx-verify` 全 PASS,WARN 只允许 DGX-20(DCGM)与 DGX-22(空接收端——
  切流前必须先 `make dgx-alert-receiver`,让它变 PASS)
- 网关凭据已由 `make dgx-gateway-secret` 生成并记录在密码库
- 边缘清单里的 ⟪DECIDE-D4⟫ 已全部填入真实值:
  - `platform/overlays/dgx/edge/issuer-and-ingress.yaml`:FQDN、运维邮箱
  - `platform/overlays/dgx/edge/ingress-nginx.yaml`:hostNetwork 头节点 or LB 形态
- DNS:FQDN 的 A 记录已指向头节点(或 LB)公网地址,TTL 先调低到 300s

## 1. cert-manager(证书自动化)

```bash
helm repo add jetstack https://charts.jetstack.io && helm repo update
helm install cert-manager jetstack/cert-manager --version $(. versions.env; echo $CERT_MANAGER_VERSION) \
  -n cert-manager --create-namespace -f infra/dgx/operators/cert-manager-values.yaml
kubectl --context $DGX_KCTX -n cert-manager rollout status deploy/cert-manager --timeout=180s
```
验证:三个 Pod Running;`kubectl get crd certificates.cert-manager.io` 存在。
回退:`helm uninstall cert-manager -n cert-manager`。

## 2. 边缘 + 网关开关(一个命令,不可拆)

```bash
make dgx-edge DGX_KCTX=$DGX_KCTX
# = kubectl apply -k platform/overlays/dgx/edge
#   + kubectl set env deploy/platform-gateway GW_TRUST_PROXY=true GW_COOKIE_SECURE=true
#   + rollout status;edge/ 里还有 ⟪DECIDE⟫ 占位时拒绝执行
kubectl --context $DGX_KCTX -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=180s
# Let's Encrypt HTTP-01 需要 80 端口从公网可达 —— 防火墙先放行 80/443 到头节点
kubectl --context $DGX_KCTX -n platform-system get certificate platform-gateway-tls -w
```
验证:Certificate `Ready=True`;`curl -I https://<FQDN>/healthz` 返回 200 且证书链有效;
`make dgx-verify` 的 **DGX-26** 断言"edge 存在 ⇔ 两个开关为 true"(混合态 FAIL)。
回退:`make dgx-edge-off`(同样成对:删 edge/ 并把两个开关拨回 false;证书 Secret 保留,重来不重签)。

2026-08-27 前这是两步(先 apply edge,再改 gateway.yaml 重新 apply);审查指出两步之间的窗口
里所有客户共享头节点 IP,8 次错密码即可把整个平台锁死 15 分钟——所以改成一个 target。
gateway.yaml 里的两个值保持 `"false"`(bring-up 态),运行态由 `set env` 翻转;
以后重跑 `make dgx-platform` 会把它们拨回 false —— DGX-26 会立刻变红,再跑一次 `make dgx-edge` 即可。

为什么必须同时:只翻 Secure 而边缘未就绪 → 浏览器不发 cookie,谁也登不进;
只翻 TRUST_PROXY 而前面没有边缘 → 任何客户端可伪造源 IP 绕过登录限速。
验证:浏览器登录一次;`Set-Cookie` 含 `__Host-arise_session` 与 `Secure`;
限速日志里的 `ip` 是真实客户端 IP 而非边缘 IP。
回退:两个开关同时改回 `"false"`,`make dgx-platform`。

## 3. 防火墙收口

- 头节点公网入向:只放 80(HTTP-01 与重定向)、443
- 6443(API server)、22(SSH)、BMC 网段:**只允许运维网段/VPN**
- 租户 SSH 开发机的公网接入:按 D4(bastion 或 LB 端口映射)另行开放,
  **不要**直接把节点 IP:端口暴露出去
验证:从外网 `nmap -p 22,6443,80,443 <公网IP>` 只见 80/443。

## 4. 最后一遍门

```bash
# LAUNCH=1:把「Day-0 期间正常、收钱之后致命」的几项从 WARN 提为 FAIL ——
# 告警打本地空接收端、devbox/SPA 还是 day0 哨兵镜像、GPU 健康无人观测。
# 不带这个变量的 dgx-verify 是「Day-0 进行中」的门,不是「可以卖了」的门。
LAUNCH=1 make dgx-verify   # 必须 failed=0
make dgx-test              # 全量矩阵打真机(每次全量跑都会开一个新的证据 campaign)
make evidence-seal         # 冻结这一轮:没冻结就没有验收记录
make evidence-verify       # 冻结完当场验;这一步不过 = 包被覆盖过,按 §9.4 那些用例是 INVALID 不是 PASS
```

> 证据包会被**同一个 campaign 里的重跑覆盖**。全量跑(`MODE=all`)会自动换新的
> `.run_id`,单条用例重跑则并入当前 campaign —— 这样「跑完全量、再单独重跑一条看
> 细节」还是原来的用法,而每一轮全量的包各自独立、封完即不可变。
> 2026-09-01 之前 `.run_id` 写一次就再也不换:盘上那个包标着 2026-08-11、装着
> 2026-09-01 的结果、封条是 08-17 封的,190 个文件里 85 个已经对不上。

## 5. DNS TTL 恢复、通知客户

TTL 回 3600;按 `docs/customer/quickstart.md` 把接入地址给客户。

## 回退总原则

任何一步失败:先回退该步,**不要**"顺手"回退前面的步骤;证书 Secret 与
`platform-gateway-auth` 永远不删——它们是重来的起点,不是问题的来源。
