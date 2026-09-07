# Day-0 公网切流（步骤 12）

当前操作步骤见 [公网入口与 TLS](public-edge.md)。2026-09-05 起，新安装使用 Caddy 自带 ACME，不再安装 ingress-nginx 或依赖 cert-manager。

开始前先检查 [本轮审查及上线条件](../docs/review-2026-09-05.md)，完成 [账号备份恢复](auth-recovery.md) 和实际硬件验收。公网切流命令为 `make dgx-edge DGX_KCTX=...`，回退为 `make dgx-edge-off DGX_KCTX=...`。
