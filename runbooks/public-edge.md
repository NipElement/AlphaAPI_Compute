# 公网入口与 TLS

平台使用 digest 固定的 Caddy 2.11.4。它负责 HTTPS、ACME 续期、HTTP 跳转、请求大小与超时限制；网关负责认证、授权、CSRF、登录限流和安全响应头。Caddy 不持有 Kubernetes 凭据。入口配置在 `platform/overlays/dgx/edge/`，仅由显式切流命令部署。

## 切流前

1. 填 `edge/caddy.yaml` 的 `CONSOLE_FQDN` 和 `ACME_EMAIL`，域名 A/AAAA 必须指向实际提供服务的头节点。不要保留错误的 AAAA 记录。
2. 确认控制台入口公开 TCP 80/443；启用 [租户堡垒](tenant-access.md) 时另开放 TCP 2222；Kubernetes API、kubelet、网关 8080、数据库和监控端口留在管理网络。80 用于重定向和 ACME HTTP 验证。
3. 创建生产网关 Secret 和台账 HMAC Secret；运行 `make dgx-verify DGX_KCTX=...`。设置真实告警接收端并确认送达。
4. 完成 [生产就绪清单](../docs/production-readiness.md) 中的存储、备份、租户接入及硬件验收条件。一个绿色 Pod 不代表平台已获上线验收。
5. 头节点 `/raid` 与 retained StorageClass 必须可用。`platform-edge-data` 保存 ACME 账号、证书和私钥，应纳入加密的离机备份。

## 切流和验证

```bash
make dgx-edge DGX_KCTX=arise-dgx
```

命令先验证配置，再打开网关 `GW_COOKIE_SECURE` / `GW_TRUST_PROXY` 并等待它 Ready，最后启动 Caddy。`make dgx-platform` 会在渲染阶段保留已开启的安全状态，避免部署过程中出现配置退回窗口。

```bash
kubectl --context arise-dgx -n edge-system logs deploy/platform-edge --tail=80
curl --fail --show-error --silent https://console.YOUR_DOMAIN/readyz
curl --head http://console.YOUR_DOMAIN/
```

使用实际域名替换示例。HTTPS 检查必须正常验证证书，不能用 `-k`。用真实浏览器完成登录、访问自己租户、拒绝跨租户访问、退出后旧 cookie 被拒绝；检查会话 cookie 的 `Secure`、`HttpOnly`、`SameSite=Lax` 和 `__Host-` 前缀。确认 HTTP 跳转到 HTTPS。Caddy 的进程探针只证明进程可服务，**不证明公网 DNS、ACME 签发或防火墙正确**。

Caddy 的信任边界是直连客户端：覆盖客户端传入的 X-Forwarded-For 和 X-Forwarded-Proto。增加 CDN/LB 时，必须同时重新设计可信代理和源 IP 规则，不能直接打开任意 forwarded headers 信任。

## 回退与更新

```bash
make dgx-edge-off DGX_KCTX=arise-dgx
```

先缩容边缘并等待监听进程退出，再关闭网关代理标志。ACME PVC 保留。更新 Caddyfile 后重跑 `make dgx-edge`，哈希 ConfigMap 触发重启；头节点 hostNetwork 使用 Recreate，有短暂入口中断。

从旧 ingress-nginx 安装迁移时，先确认同一头节点的 80/443 由谁占用，安排切流窗口，停止旧 ingress Deployment 后再启动 Caddy。确认新入口及续期成功后再移除旧控制器和 RBAC；不要删除仍被其他应用使用的 cert-manager 或证书。新装平台不需要 cert-manager。

离线集成验证：`python3 tests/live_edge.py`。它使用本地 CA 和仅绑定 loopback 的临时容器，验证证书、非 root / 只读运行、伪造转发头、cookie 透传和 413。它不申请公网证书。
