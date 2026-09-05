# 跨境智营台——亚马逊美国站智能运营平台

一个可本地运行、可测试、可按单机生产拓扑部署的运营后台：覆盖订单/退货、商品主数据、SKU 贡献利润、选品评分、双语文案、人工审批、模拟发布、FBA 库存预警、采购补货、客服履约和客户反馈整改闭环。

> 证据边界：本项目不连接真实亚马逊店铺；数据与商品主图均为人工智能合成样例，不代表真实商品或店铺经营结果；“发布”只保存本地快照；离线测试结果不代表生产效果或亚马逊合规认证。

## 设计原则

- AI 生成建议，确定性代码负责选品评分、库存阈值、工单优先级和发布门禁。
- 商品文案必须人工审核；草稿或拒绝状态不能发布。
- 智能商品文案固定生成中英双语，并按 SKU、品类和使用场景生成差异化标题、五点与描述；英文作为 Amazon US 发布字段，中文作为可编辑运营译稿，两套内容一起进入审批与发布快照。
- 仪表盘按 30 天合成数据估算销售额、贡献利润、广告费、退款损失与缺货影响，明确不作为财务报表。
- 开发环境没有模型密钥时使用确定性演示模型；生产环境默认拒绝静默降级，只有显式设置 `ALLOW_DEMO_AI=true` 才允许演练模式。
- 密码采用 PBKDF2 哈希，会话签名且 HttpOnly；写操作包含 CSRF 与角色校验（viewer/operator/approver/admin）。
- 登录含失败锁定、TOTP 多因素认证、临时密码强制修改和服务端会话撤销；客户附件存放于非静态私有目录并经过鉴权下载。
- 审批、发布和工单状态使用版本号防止并发覆盖；审计记录禁止修改和删除，并记录操作人及请求编号。
- 审计事件使用 SHA-256 前向哈希链，健康检查会识别链条异常；这提供篡改检测，不替代外部不可变日志归档。
- 渠道同步进入持久化任务表，由独立 Worker 消费，失败采用有限次数退避重试；每条失败数据可追溯。
- SQLite 单体适用于单店铺、小团队、单机部署；不要把 Web 水平扩容为多实例。多店铺或高并发场景必须先迁移 PostgreSQL。
- Amazon US 固定标记为 `demo_sp_api`；字段和页面职责参考真实运营流程，但不伪装成真实授权。

## 本地启动（Windows PowerShell）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m app.main init-db
.\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --reload --port 8030
```

打开 <http://127.0.0.1:8030>，使用 `.env` 中的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。初始示例密码只用于本地，必须修改。本机裸 `python` 可能指向旧版本，文档统一使用 `py -3.12` 或项目 `.venv`。

## 5 分钟演示路线

1. 在“商品与选品”导入 `samples/products.csv`，对商品执行“分析”。
2. 打开“经营利润”，按 SKU 查看采购、佣金、FBA、头程、广告、退款和贡献利润拆解。
3. 点击“生成文案”，进入智能商品文案工作台并排查看英文发布内容和完整中文运营译稿。
4. 人工编辑并批准，之后点击“模拟发布”；它只验证发布门禁并固化快照，不会写入真实店铺。
5. 在“库存中心”同步库存，查看历史趋势；在“采购补货”审批计划、转采购单并登记分批收货。
6. 在“客服工单”同步订单事件，生成英文回复建议，上传私有凭证，指定负责人并完成人工售后审批闭环。
7. 在“客户反馈洞察”同步聚合主题，再创建负责人、截止时间和处理结果完整的整改行动。
8. 在“账号与安全”修改临时密码、启用 MFA、撤销会话并按角色维护用户。

开发环境 API 文档位于 <http://127.0.0.1:8030/docs>；生产环境自动关闭文档。存活检查为 `/health/live`，就绪检查为 `/health/ready`，脱敏运营计数为 `/health/metrics`。

## 配置真实模型（可选）

编辑 `.env` 或设置环境变量：

```text
AI_API_KEY=your-key
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4.1-mini
AI_TIMEOUT_SECONDS=30
```

接口必须兼容 `POST /chat/completions`。模型返回内容会经过结构化数据格式、字段数、标题长度和高风险表述校验；失败不会回退为看似成功的发布结果。请勿提交 `.env`。

## Docker

先复制 `.env.example` 为 `.env`，至少替换 `SESSION_SECRET` 和 `ADMIN_PASSWORD`，并将 `APP_ENV` 改为 `production`。真实运行应设置 `AI_API_KEY` 并将 `ALLOW_DEMO_AI=false`、`SYNC_INLINE=false`。

```powershell
docker compose -p cross-border-ai up --build
```

访问 <http://127.0.0.1:8030>。停止服务时使用：

```powershell
docker compose -p cross-border-ai down
```

Compose 同时启动一个 Web 和一个持久化同步 Worker，数据保存在独立卷 `cross-border-ai-data`。Web 容器使用非 root 用户并带就绪检查。SQLite 配置只允许单 Web 实例，不要执行 `--scale web`。

执行一次带完整性校验、校验和清单和 14 份保留策略的容器内备份：

```powershell
docker compose -p cross-border-ai --profile maintenance run --rm backup
```

## 上线与运维

- TLS 必须由 Nginx、Caddy 或云负载均衡终止；生产 Cookie 只通过 HTTPS 发送。
- 用 `python -m scripts.manage_user create <用户名> --role operator` 创建账号；临时密码首次登录必须修改。停用账号会同时撤销全部有效会话。
- 每日执行 `python scripts/backup_db.py --retention 14`，并执行 `python scripts/cleanup_attachments.py` 清理超过保留期的附件；把备份及同名 JSON 校验清单复制到独立存储。恢复前停止 Web 和 Worker，再运行 `python scripts/restore_db.py <备份文件>`；脚本拒绝覆盖现存数据库。
- Windows 单机部署可用 `powershell -ExecutionPolicy Bypass -File scripts/install_backup_task.ps1` 安装每日 02:30 备份任务；安装后应在“任务计划程序”中手动运行一次并核对 `backups` 目录。Linux 可将同一 `backup_db.py` 命令加入 cron。计划任务只负责本机备份，异地复制与告警仍由部署环境负责。
- 部署前运行 `python scripts/check_migrations.py --database data/app.db`，它会核对版本、关键表字段、索引和不可变审计触发器。`/health/ready` 只校验最近 100 条审计链以控制探针耗时；管理员可调用 `POST /api/admin/audit/verify` 执行全量审计链校验。`/health` 的 `backup=missing` 是运维预警，不会伪装成已有备份。
- HTTP 日志为单行 JSON，含请求编号、用户、状态码和耗时，不记录密钥。生产日志仍需送往外部集中日志平台并配置告警。
- `.github/workflows/ci.yml` 会执行编译、测试和镜像构建。上线前必须通过 CI、数据库备份恢复演练和人工验收。
- 当前 Amazon 渠道仍是 `demo_sp_api`，发布仍是 `mock_published`。更换真实适配器时应保持同样的幂等事件号、失败表、人工审批和审计边界。

## 测试与离线指标

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
.\.venv\Scripts\python.exe scripts\evaluate.py
```

测试覆盖贡献利润、双语 Listing 与证据/合规门禁、客服状态机和售后审批、私有附件、反馈整改闭环、渠道幂等与失败重试、库存与采购收货、订单和客户脱敏、MFA/会话/角色、审计链、备份恢复和完整模拟发布闭环。库存与客服 CSV 接口仅为离线开发测试兼容能力，已从页面和 OpenAPI 文档隐藏。

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
