# 开发与验证

## 工具与入口

从仓库根目录运行。后端运行时使用 Python 标准库；本机配置检查和渲染还需要 PyYAML。`make check` 需要 Python 3.12、PyYAML、Node.js/npm、Git、Bash 和 kubectl（仅本地渲染）；本次验收使用 Node.js 22。版本与依赖由 `versions.env`、`web/package-lock.json` 和 `services/fake-gpu-plugin/go.sum` 记录。

```bash
make check       # L0 后端/配置检查 + npm ci + TypeScript/Vite 构建
make validate    # 只跑后端/配置；无需 Docker 或活集群
make dgx-render  # 单独检查真实集群清单及占位状态，不连接集群
```

`npm ci` 按锁文件安装，缓存缺失时需要网络；不自动升级依赖。独立 Python 虚拟环境可放 `.venv/`，安装检查所需的 PyYAML 后再运行。不要将宿主机检查依赖误加入标准库后端镜像。

## 前端开发

已部署 lab 的查看方法在 [README](../README.md#查看控制台)。要编辑源码并热更新，先保持网关的 loopback port-forward `127.0.0.1:8888`，另开终端：

```bash
npm --prefix web ci
npm --prefix web run dev -- --host 127.0.0.1
```

本机访问 `http://127.0.0.1:5173`，Vite 将认证和业务 API 代理到真实 lab 网关。远程开发时给现有 SSH 隧道增加 `-L 5173:127.0.0.1:5173`。开发服务器只用于开发，生产交付由 gateway 伺服构建后的 SPA。

```bash
make web         # 生成 web/dist
make web-assets  # 仅更新已部署 lab 的前端，需要 Docker/kind
```

正式镜像交付使用 `make web-image`，生产部署顺序见 [DGX 手册](../runbooks/day0-setup.md)。`web/dist` 和 `services/web/dist` 均为可重建产物；编辑源码后必须重新构建和发布才能更新客户看到的页面。

## 验证层次

| 改动范围 | 相关检查 |
|---|---|
| 文档、忽略规则 | 本地链接、命令路径、`git diff --check`、暂存清单与忽略命中检查 |
| 后端/配置 | `make validate`；生产 overlay 再检查 `make dgx-render` |
| 前端 | `make check`；发布到 lab 后按影响执行浏览器业务/质量验收 |
| 认证持久化与恢复 | `make test-auth-recovery`，临时进程与临时数据库 |
| 集群权限、状态机、计量、工作负载 | `make test`、`make verify`，按需加对应 live 测试 |
| 真实生产环境 | `make dgx-verify`、`make dgx-test`、硬件验收和 `make dgx-launch-verify` |

完整浏览器命令及 Chrome、CJK 字体前置项见 [浏览器验收](../web/e2e/README.md)。修改工作负载的集群套件顺序执行，避免相互污染。筛选用例、注入响应、短时 HTTP 采样和模拟 GPU 均有各自边界，不能代替完整真实环境验收。

## 文件与提交约定

- 提交源码、Kubernetes 模板、依赖锁文件、静态资源及相应许可证。`platform/vendor` 是固定版本部署输入，不能当缓存删除；JSON 词库由前端与检查器共同读取。Inter 字体许可证唯一来源为 `web/public/assets/Inter-LICENSE.txt`，构建时随站点复制交付。
- 根 `.gitignore` 统一管理依赖、构建、缓存与本地配置。不要提交 `.env*` 实值、kubeconfig、私钥、SQLite 数据库或恢复副本；环境示例可使用 `.env.example` / `.env.<name>.example`。忽略规则只是防误加，提交前仍需核对暂存内容。
- 本地运维资料可放 `.local/`；密钥和备份优先保存在受控仓库外部位置。Git 忽略不是加密或访问控制，已经被追踪的文件也不会因新规则自动取消追踪。
- `platform/access/keys.yaml` 保留可审查的**公钥**注册表，默认空表。若组织域名和公钥登记不应进入共享 Git，可复制为 `platform/access/keys.local.yaml` 并通过 `ACCESS_KEYS` 显式指定。无论放哪里都不得包含私钥，复制后先检查文件权限。
- 验收输出统一放 `evidence/RUN-<唯一标识>/`；新一轮用 `make new-run` 或套件自动生成 campaign。封存后不改写，`make evidence-verify` 验证当前包；`--reseal` 是明确记录替代关系的特殊操作。
- 本机 `.guard/` 和 `.run_id` 不提交；不要重建基线来绕过保护失败。豁免文件是审查依据，仍然提交。

提交前运行相应检查，再核对 `git diff --check`、`git diff --cached --stat` 和实际暂存内容。检查未跟踪文件，避免遗漏新模块或把本机数据一起提交。
