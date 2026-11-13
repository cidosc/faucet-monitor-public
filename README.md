# Faucet Monitor · 水龙头与空投线索监控

一个自托管的 Python / FastAPI 实验项目：收集测试网水龙头与空投线索、记录可访问性、检查公开地址的部分资格条件、展示领取准备状态及入账记录。使用 PostgreSQL 保存结果，提供网页面板和命令行工具。

**这是监控与人工复核工具，不保证项目可信、资格成立、领取成功或收益。不要向它导入有价值的钱包私钥。**

## 功能与边界

- 水龙头页面探测、CAPTCHA / 访问限制识别、超时重试及每主机限速。
- 手工来源、RSS、可选的结构化空投列表及公告 RSS。
- EVM 链配置与 chain ID 校验、公开地址资格检查、合约只读模拟、入账检测。
- FastAPI 面板、CLI、数据库迁移、可选飞书与 Telegram 通知。
- 默认关闭调度、通知和自动领取；钱包列表为空。初始面板没有扫描数据是正常的。
- HTTP 探测不会绕过 CAPTCHA、WAF 或访问限制。第三方列表只作为待核实线索。

### 关于签名代码

源码**包含** `app/autoclaim/executor.py` 的交易构造、签名、广播及确认框架，并依赖 `eth-account`，不能称为“完全没有签名代码”。当前 `ADAPTERS` 注册表刻意为空，`GLOBAL_AUTO_CLAIM=false`、`AUTO_CLAIM_DRY_RUN=true`，标准 Compose 配置不挂载签名密钥。

仅切换环境变量并不能提供可用的领取适配器。新增适配器必须单独审查项目条款、权限、测试钱包及策略门槛。**dry-run 执行器在满足前置条件后仍可能加载密钥并签名，只是不广播；它不是无密钥模式。** `signer --selftest` 也不是普通的监控自检。常规使用不需要运行这些命令。

## Docker 安装

需要 Docker Engine、Compose v2，以及下载基础镜像和 Python 依赖的网络。以下命令在项目根目录执行：

```sh
cp .env.example .env
chmod 600 .env
```

编辑 `.env`：

1. 分别设置随机的 `SECRET_KEY`、`POSTGRES_PASSWORD` 和 `DASHBOARD_PASSWORD`。例如每次运行 `openssl rand -hex 32` 生成一个不同值；不要使用示例口令。
2. 将 `DATABASE_URL` 中的 `CHANGE_ME` 替换为同一个数据库密码；用户名、库名与 `POSTGRES_*` 保持一致。使用十六进制密码可避免 URL 转义问题。
3. 首次启动保留 `RUN_SCHEDULER=false`、`NOTIFY_ENABLED=false`、`DASHBOARD_ENABLED=true` 和自动领取的保守默认值。
4. 如需监控，先检查 `config/chains.yaml`、`config/sources.yaml` 和 RPC 配置。样例公共端点不代表当前仍可用或允许任意请求。

```sh
docker compose config --quiet
docker compose up -d --build
docker compose logs --tail=100 app
```

打开 <http://127.0.0.1:8000>，使用 `.env` 中的面板用户名和密码。入口会等待数据库并应用 Alembic 迁移。数据库不发布到宿主机端口，面板仅发布到宿主机回环地址。`DASHBOARD_PORT` 修改的是宿主端口，容器内始终为 8000。

远程访问请使用 SSH 隧道，或自行配置经过认证的 HTTPS 反向代理。HTTP Basic 本身不加密传输。不要直接将端口暴露到公网。日志可含地址、查询结果及端点错误，应视为私有运行数据。

停止服务可运行 `docker compose down`，数据库卷会保留。**不要随意使用 `down -v`，它会删除数据卷。** 首次初始化后的数据库密码变更需要同步修改 PostgreSQL 内的角色密码，仅编辑 `.env` 不会更新已有数据库。

## 配置与开始监控

所有主动扫描都会访问外部网站或 RPC 服务。先确认服务条款、请求额度以及你愿意将哪些公开地址发送给提供商。

- `config/chains.yaml`：链定义。RPC 地址由 `.env` 的 `RPC_<CHAIN_KEY>` 提供；横线转为下划线。
- `config/sources.yaml`：种子项目和候选 URL。默认来源是 `manual,rss`；可通过 `DISCOVERY_ENABLED_SOURCES` 选择 `airdrop_api` 或 `airdrops`。GitHub 来源尚未实现。
- `config/wallets.yaml`：只填用于只读监控的公开地址，保留 `auto_faucet: false`。不要提交个人配置、标签或地址清单到公开仓库。
- `DETECT_CHAINS`：入账扫描链清单；`SCAN_CHUNK_BLOCKS` / `SCAN_MAX_CHUNKS` 控制扫描窗口，结果不一定涵盖完整历史。
- `HTTP_PROXY_URL` 和 `TELEGRAM_PROXY`：可选的、由你自行管理并有权使用的外部代理；默认留空。此项目不附带代理服务、订阅或地理数据库。容器内的 `127.0.0.1` 不是宿主机。
- 通知：仅实现 `feishu` 与 `telegram`。其它渠道的预留环境变量不代表已有实现。启用时配置相应凭据及 `NOTIFY_CHANNELS`，再设 `NOTIFY_ENABLED=true`。不要提交 webhook、机器人 token 或聊天 ID。

完成配置后可将 `RUN_SCHEDULER=true`，运行 `docker compose up -d app` 重新创建应用容器。保留 `DASHBOARD_ENABLED=true`；关闭面板会进入专用 worker 模式，**该模式会启动调度器，不以 `RUN_SCHEDULER=false` 作为离线开关**。需要完全停止任务时停止应用容器。

可查看命令帮助（不执行扫描）：

```sh
docker compose exec app python -m app.cli --help
docker compose exec app python -m app.cli job list
```

手动执行任务、链校验、领取准备检查或通知测试都可能产生外部请求，不属于离线测试。

## 本地开发与离线测试

需要 Python 3.12 或以上版本。从项目根目录执行：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check .
```

测试使用固定数据和替身，不需要真实钱包、RPC 密钥或运行中的 PostgreSQL。建议在没有 `.env`、没有真实凭据的独立工作副本内测试。测试覆盖 ABI、页面分类、策略门槛、准备状态、资格汇总、路由及通知模板，不等于端到端部署、安全审计或真实领取验证。

本地启动应用仍需要自行配置 PostgreSQL、执行 `alembic upgrade head` 并提供环境变量。项目依赖版本设有下限，但未锁定完整解析结果；升级后应重新测试。

## 重要限制

- 这是实验代码，未做安全审计，不适合保管资产或无人值守处理有价值的钱包。
- URL、链配置、服务接口及规则会变化；本仓库不提供实时可用性保证。
- 入账记录可能包含垃圾代币；不要仅凭收到代币就与未知合约交互。
- `UNKNOWN` 表示证据不足，不应被当作满足条件。价格默认静态且可为零，不能用作真实费用或收益保证。
- 默认只部署一个 app 实例。数据库迁移、并发调度、重启恢复与通知交付仍需针对实际环境验证。
- 发布内容不包含运行数据库、日志、个人凭据、部署记录、代理节点配置或第三方二进制数据库。

## 许可

当前未指定开源许可证。公开可见不等于获得开源许可；使用、修改或再分发前请与权利人确认授权。此副本没有附加任何许可证。
