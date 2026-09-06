# 跨境智营台——亚马逊美国站智能运营平台

一个可本地运行、可测试、可按单机生产拓扑部署的运营后台：覆盖订单/退货、商品主数据、SKU 贡献利润、选品评分、双语文案、人工审批、模拟发布、FBA 库存预警、采购补货、客服履约和客户反馈整改闭环。

> 证据边界：本项目不连接真实亚马逊店铺；数据与商品主图均为人工智能合成样例，不代表真实商品或店铺经营结果；“发布”只保存本地快照；离线测试结果不代表生产效果或亚马逊合规认证。

## 设计原则

- AI 生成建议，确定性代码负责选品评分、库存阈值、工单优先级和发布门禁。
- 商品文案必须先保存草稿，再由不同账号批准；创建者/编辑者不能批准自己的版本，草稿或拒绝状态不能发布。
- 智能商品文案固定生成中英双语，并按 SKU、品类和使用场景生成差异化标题、五点与描述；英文作为 Amazon US 发布字段，中文作为可编辑运营译稿，两套内容一起进入审批与发布快照。
- 仪表盘按 30 天合成数据估算销售额、贡献利润、广告费、退款损失与缺货影响，明确不作为财务报表。
- 开发环境没有模型密钥时使用确定性演示模型；生产环境默认拒绝静默降级，只有显式设置 `ALLOW_DEMO_AI=true` 才允许演练模式。
- 密码采用 PBKDF2 哈希，会话签名且 HttpOnly；写操作包含 CSRF、角色与细粒度权限校验（商品、客服、文案、渠道、采购、收货、库存纠错）。
- 登录同时按“来源 IP + 账号”和来源 IP 聚合限流，支持 TOTP、临时密码强制修改、服务端会话撤销及会话/MFA 密钥平滑轮换；客户附件与上传商品图均经过登录鉴权。
- 审批、发布和工单状态使用版本号防止并发覆盖；审计记录禁止修改和删除，并记录操作人及请求编号。
- 审计事件使用 SHA-256 前向哈希链，并用独立 `AUDIT_SIGNING_KEY` 把 HMAC 检查点追加到数据库外的 `AUDIT_CHECKPOINT_PATH`；健康检查会识别异常。
- 客户标识在隐私请求表中仅保存独立密钥 HMAC；匿名化会同步删除私有附件并清理元数据。
- 渠道同步进入持久化任务表，由独立 Worker 消费，失败采用有限次数退避重试；每条失败数据可追溯。
- SQLite 单体适用于单店铺、小团队、单机部署；不要把 Web 水平扩容为多实例。多店铺或高并发场景必须先迁移 PostgreSQL。
- 默认 Amazon US 标记为 `demo_sp_api`；切换 `CHANNEL_ADAPTER=http` 后由已授权的企业网关提供标准化数据，应用仍保留幂等、校验、失败隔离与审计。

## 本地启动（Windows PowerShell）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m app.main init-db
.\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --reload --port 8030
```

打开 <http://127.0.0.1:8030>，使用 `.env` 中的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。初始示例密码只用于本地，必须修改。本机裸 `python` 可能指向旧版本，文档统一使用 `py -3.12` 或项目 `.venv`。

登录后先打开 <http://127.0.0.1:8030/guide>。“演示导览”按真实依赖顺序串起 12 步完整流程，每一步都写明操作者动作、系统行为、证据位置和可直接使用的讲解话术，并可在浏览器中勾选进度。

## 5 分钟演示路线

1. 在“商品与选品”导入 `samples/products.csv`，对商品执行“分析”。
2. 打开“经营利润”，按 SKU 查看采购、佣金、FBA、头程、广告、退款和贡献利润拆解。
3. 点击“生成文案”，进入智能商品文案工作台并排查看英文发布内容和完整中文运营译稿。
4. 运营账号保存编辑稿，切换到独立审批账号批准，之后点击“模拟发布”；它只验证发布门禁并固化快照，不会写入真实店铺。
5. 在“库存中心”同步库存，查看历史趋势；在“采购补货”审批计划、转采购单并登记分批收货。
6. 在“客服工单”同步订单事件，生成英文回复建议，上传私有凭证，指定负责人并完成人工售后审批闭环。
7. 在“客户反馈洞察”同步聚合主题，再创建负责人、截止时间和处理结果完整的整改行动。
8. 在“账号与安全”修改临时密码、启用 MFA、撤销会话并按角色维护用户。

开发环境 API 文档位于 <http://127.0.0.1:8030/docs>；生产环境自动关闭文档。存活检查 `/health/live` 可公开使用；生产环境的 `/health/ready` 必须带 `X-Health-Token`，详细 `/health`、运营计数 `/health/metrics` 和全量审计校验只允许管理员访问。

## 配置真实模型（可选）

编辑 `.env` 或设置环境变量：

```text
AI_API_KEY=your-key
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4.1-mini
AI_TIMEOUT_SECONDS=30
```

接口必须兼容 `POST /chat/completions`。模型返回内容会经过结构化数据格式、字段数、标题长度和高风险表述校验；失败不会回退为看似成功的发布结果。请勿提交 `.env`。

## 接入授权渠道网关

应用不直接保存亚马逊刷新令牌。生产部署建议由独立的授权网关负责 LWA、AWS 签名、限流与原始响应留存，本应用只读取标准化的 `rows`：

```text
CHANNEL_ADAPTER=http
AMAZON_ADAPTER_BASE_URL=https://your-internal-gateway.example
AMAZON_ADAPTER_TOKEN=replace-with-secret-manager-value
AMAZON_ADAPTER_TIMEOUT_SECONDS=30
```

网关需提供 `GET /sync/{orders|inventory|tickets|feedback|finance|market}` 并返回 `{ "rows": [...], "next_cursor": "...", "watermark": "..." }`。应用逐页校验和提交，成功后持久化游标；中途失败从最后成功页继续，并拒绝循环游标和超过 1000 页的异常响应。

## Docker

先复制 `.env.example` 为 `.env`，替换 `SESSION_SECRET`、`MFA_ENCRYPTION_KEY`、`AUDIT_SIGNING_KEY`、`PRIVACY_HASH_KEY`、`HEALTH_TOKEN` 和 `ADMIN_PASSWORD`，并配置附件扫描网关、独立备份挂载 `BACKUP_REPLICA_HOST_PATH` 及外部审计挂载 `AUDIT_CHECKPOINT_HOST_PATH`。

```powershell
docker compose -p cross-border-ai up --build
```

访问 <http://127.0.0.1:8030>。停止服务时使用：

```powershell
docker compose -p cross-border-ai down
```

Compose 同时启动一个 Web 和一个持久化同步 Worker，数据库与主备份分别保存在 `cross-border-ai-data` 和 `cross-border-ai-backups`。Web 容器使用非 root 用户并带鉴权就绪检查。SQLite 配置只允许单 Web 实例，不要执行 `--scale web`。

执行一次带完整性校验、校验和清单和 14 份保留策略的容器内备份：

```powershell
docker compose -p cross-border-ai --profile maintenance run --rm backup
```

## 上线与运维

- TLS 必须由 Nginx、Caddy 或云负载均衡终止；生产 Cookie 只通过 HTTPS 发送。
- 用 `python -m scripts.manage_user create <用户名> --role operator` 创建账号；临时密码首次登录必须修改。停用账号会同时撤销全部有效会话。
- Worker 每 30 秒执行通知刷新、Webhook 签名投递与指数退避重试、过期附件/同步失败清理、客户数据匿名化、审计检查点和备份新鲜度维护。Webhook HTTP 请求不占用 SQLite 写事务。
- 每日执行 `python scripts/backup_db.py --retention 14 --replica-dir <独立磁盘目录>`。脚本使用 SQLite 在线备份、完整性检查和 SHA-256 清单，并校验第二副本；恢复前停止 Web 和 Worker，再运行 `python scripts/restore_db.py <备份文件>`，脚本拒绝覆盖现存数据库。
- Windows 单机部署可用 `powershell -ExecutionPolicy Bypass -File scripts/install_backup_task.ps1` 安装每日 02:30 双份备份任务；安装后手动运行一次并实际做恢复演练。容器维护任务同样要求 `BACKUP_REPLICA_PATH` 指向独立挂载，不能把同目录副本当异地备份。
- 数据库使用编号 SQL 迁移，当前正式版本为 21；生产由 Compose `migrate` 任务独占执行，Web/Worker 只校验版本，避免并发迁移。动态 `ALTER` 仅保留给无迁移记录的历史库基线兼容。
- 美国站业务日界默认使用 `America/Los_Angeles`，界面默认使用 `Asia/Shanghai`；金额同时保留兼容展示列和整数美分列，数据库触发器拒绝两者不一致。
- 库存盘点纠错、已完成售后动作冲正和未收货采购单取消都是“申请 → 异人批准 → 执行”，且全程写审计。
- 客户数据默认保留 730 天、同步失败保留 30 天，可通过环境变量调整；管理员提供导出、匿名化与法律保全接口。真正上线前必须让法务确认地区性保留期限和删除依据。
- 金额写入整数美分字段；订单商品成本在同步时冻结，费率更新写历史版本。退款动作不能超过订单剩余可退金额，普通审批账号默认上限为 500 美元。
- HTTP 日志为单行 JSON，含请求编号、用户、状态码和耗时，不记录密钥。生产日志仍需送往外部集中日志平台并配置告警。
- 应用按 `Content-Length` 拒绝超过 `MAX_REQUEST_BYTES` 的请求；生产反向代理仍必须配置相同或更严格的 body/chunked 上限。
- `.github/workflows/ci.yml` 会执行依赖漏洞扫描、编译、覆盖率门槛、真实浏览器端到端测试、迁移检查、备份和 Compose 健康冒烟。上线前还必须通过人工验收与恢复演练。
- 每次部署按 [RELEASE.md](RELEASE.md) 完成发布前备份、独占迁移、业务冒烟、回滚记录与季度恢复演练。
- 当前 Amazon 渠道仍是 `demo_sp_api`，发布仍是 `mock_published`。更换真实适配器时应保持同样的幂等事件号、失败表、人工审批和审计边界。

## 测试与离线指标

```powershell
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.test-run --cov=app --cov-report=term-missing --cov-fail-under=60
.\.venv\Scripts\python.exe scripts\evaluate.py
```

测试覆盖贡献利润与历史成本、双语 Listing 的职责分离和合规门禁、退款额度、客服状态机、私有媒体、反馈整改、渠道幂等与失败重试、库存与采购、客户保留/法律保全、密钥轮换、MFA/会话/角色、审计检查点、迁移、分页、2000 SKU 性能、真实 Chromium 操作、备份恢复和完整模拟发布闭环。库存与客服 CSV 接口仅为离线开发测试兼容能力，已从页面和 OpenAPI 文档隐藏。

可报告的指标仅限本地合成用例：

- 结构化演示文案校验通过率：由测试运行结果统计。
- 确定性规则边界通过率：由测试用例统计。
- 样例完整工作流成功率：由端到端测试统计。

不要在未运行测试前写出百分比，也不要将这些指标表述为真实店铺生产效果。

## 生产边界

- 当前业务代码可按单店铺、小团队、单机 Web + Worker 拓扑运行，但 Amazon 适配器仍是确定性 `demo_sp_api`；真实上线前必须替换授权、通知、订单、库存、结算、评价及 Messaging 适配器并完成 Sandbox/生产验收。
- SQLite 不支持 Web 水平扩容；应用在 `WEB_CONCURRENCY != 1` 时拒绝生产启动。需要高可用、多店铺或更高并发时先迁移 PostgreSQL、对象存储和外部任务队列。
- 客服“登记完成”和 Listing“模拟发布”均为内部流程状态，不宣称外部平台动作成功。真实动作需要保存平台请求编号、响应、重试与对账结果。
- 上线还必须由部署环境提供 TLS、密钥管理、异地备份、集中日志/告警、漏洞扫描、域名与访问网络策略；这些不能由单个 FastAPI 仓库代替。

## 参考与原创边界

- 业务闭环与人工确认思路参考 [TradeMind](https://github.com/lien0219/trademind-ai)（Apache-2.0）。
- 跨境电商能力分类和证据边界参考 [ecommerce-ai-skills](https://github.com/kangise/ecommerce-ai-skills)（CC0-1.0）。
- 本仓库为独立 Python 实现，未复制上述项目源码、界面和品牌资产。

## 许可证

[MIT](LICENSE)
