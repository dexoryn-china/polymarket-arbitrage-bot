# Polymarket AI 交易机器人

面向 Polymarket 预测市场的自主交易代理。自动发现市场、搜集证据、通过多模型 AI 集成预测概率，并在严格风控下执行交易。

默认模拟交易（Paper Trading）。实盘下单需解锁三道安全门。

> **需要帮助或获取更新版本？**  
> **Telegram：** [t.me/dexoryn777](https://t.me/dexoryn777)  
> **微信：** 扫描下方二维码添加 **DexorynWe**

[English README](README.en.md)

---

## 为什么选择这款 Polymarket AI 代理

大多数 Polymarket 工具要么复制他人钱包，要么要求你手动研究、定价、定仓每个市场。本代理自主运行完整闭环——发现、证据搜集、概率预测、风控检查、执行——让你专注于配置与监督，而不是在数百个市场中逐一点击。

### 端到端自主，而非跟单

- **独立预测** — 三模型 LLM 集成（GPT-4o、Claude、Gemini）估算概率，不锚定当前市场价格
- **自主研究** — 按类别定向网页搜索、HTML 提取、域名权威评分，从一手来源（`bls.gov`、`sec.gov`、`fec.gov` 等）拉取证据
- **自我校准** — Platt 缩放、Brier 分数追踪、市场结算后自动重训，随时间收紧预测

### 为严肃风控而设计

- **15+ 项交易前检查** — 回撤热度、Kelly 仓位、流动性/价差过滤、类别敞口上限等；任一失败即阻止交易
- **三重模拟门** — 默认模拟交易；实盘需三道独立解锁
- **巨鲸情报** — 追踪聪明钱钱包，巨鲸与模型一致或分歧时调整边际优势（Edge）

### 生产级可观测性

- **9 标签页实时仪表盘** — 引擎状态、持仓、预测、风控、聪明钱、绩效指标，端口 2345
- **不可篡改审计链** — 每笔决策记录 SHA-256 完整性校验
- **多渠道告警** — Telegram、Discord、Slack，覆盖交易、回撤警告与错误

### 功能对比

| 能力 | 本 AI 代理 | 常见替代方案 |
|------|-----------|-------------|
| **自主研究与预测** | 是 — 完整流水线 | 跟单机器人仅复制钱包 |
| **多模型集成** | 3 个 LLM + 自适应权重 | 单模型或人工猜测 |
| **校准反馈闭环** | 从已结算市场重训 | 静态或无校准 |
| **风控深度** | 15+ 检查 + 回撤热度 | 基础限制或无 |
| **聪明钱集成** | 巨鲸扫描 + Edge 调整 | 仅钱包复制 |
| **默认模拟交易** | 三重安全门 | 常仅实盘或无防护 |
| **实时仪表盘与审计** | 9 标签页 + SHA-256 日志 | 仅 CLI 或不透明 |

---

## 功能特性

### 市场发现与分类

- 通过 Polymarket Gamma API 扫描活跃市场，按成交量、流动性、价差过滤
- 11 类分类器（宏观、选举、企业、法律、科技、科学、加密、监管、地缘政治、体育、娱乐），100+ 正则规则，无 LLM 成本
- 每个市场获得可研究性评分（0–100），控制研究预算分配
- 研究前质量过滤，在昂贵 API 调用前拦截垃圾市场（约节省 90% 成本）
- 可配置冷却时间，避免频繁重复扫描同一市场

### 自主研究引擎

- 查询构建器按类别生成站点限定搜索 — 宏观用 `site:bls.gov`，企业用 `site:sec.gov`，选举用 `site:fec.gov`
- 包含反向查询，避免确认偏误
- 3 个可插拔搜索后端 — SerpAPI、Bing、Tavily — 失败时自动降级
- 通过 BeautifulSoup 完整 HTML 提取，而非仅搜索摘要
- 域名权威评分 — 一手来源（1.0）> 二手（0.6）> 未知（0.3）
- 自动过滤低质量域名（Wikipedia、Reddit、Medium、Twitter、TikTok）
- 来源缓存，可配置 TTL（默认 1 小时）

### 多模型 AI 预测

- 3 个前沿 LLM 并行集成：
  - GPT-4o（权重 40%）— 主预测器
  - Claude 3.5 Sonnet（权重 35%）— 第二意见
  - Gemini 1.5 Pro（权重 25%）— 第三意见
- 3 种聚合方法 — 截尾均值、中位数、加权平均
- 模型独立预测 — 明确禁止锚定当前市场价格
- 优雅降级 — 某模型失败时，集成继续使用其余模型
- 自适应权重 — 按类别追踪各模型 Brier 分数并随时间重加权

### 校准与自我改进

- Platt 缩放 — 逻辑压缩，将极端概率拉向 0.50
- 历史校准 — 通过逻辑回归从过往预测 vs 结果对学习
- 证据质量惩罚 — 弱证据将预测拉向 0.50
- 矛盾惩罚 — 冲突来源增加不确定性
- 集成分歧惩罚 — 模型分歧超过 10% 时增加不确定性
- 校准反馈闭环 — 30+ 已结算市场后自动重训
- Brier 分数追踪 — 监控预测准确度随时间变化

### 风险管理（15+ 项检查）

每笔交易须全部通过 — 任一失败即阻止：

- 紧急停止开关（Kill Switch）
- 最大回撤 20% 时自动停止
- 四级回撤热度系统：
  - 正常（< 10%）→ 全仓
  - 警告（≥ 10%）→ 半仓
  - 严重（≥ 15%）→ 四分之一仓
  - 最大（≥ 20%）→ 全部交易停止
- 单市场最大下注（默认 $50）
- 日亏损上限（默认 $500）
- 最大持仓数（25）
- 扣费后最小净 Edge（4%）
- 最小流动性（$2,000）
- 最大价差（6%）
- 证据质量阈值（0.55）
- 置信度过滤（最低 MEDIUM）
- 隐含概率下限（5%）
- 组合类别敞口上限（每类 35%）
- 时间线终局检查（结算前 48 小时）
- 套利检测 — 扫描定价错误的互补与多结果市场

### 执行引擎

- 分数 Kelly 仓位，7 个乘数（置信度、回撤、时间线、波动率、体制、类别、流动性）
- 自动策略选择：
  - Simple — 小单限价单
  - TWAP — 大单拆成 5 个时间加权切片
  - Iceberg — 仅显示真实订单量的 20%
  - Adaptive — 按订单簿深度调整定价
- 三重模拟安全门：
  - 订单对象上的 `dry_run` 标志
  - `config.yaml` 中的 `execution.dry_run`
  - 环境变量 `ENABLE_LIVE_TRADING`
  - 三者均允许时，真实订单才会发出
- 成交追踪 — 监控各策略的成交率、滑点、成交时间
- 6 种退出策略 — 动态止损、追踪止损、持有至结算、时间退出、Edge 反转、Kill Switch 强制退出

### 巨鲸与聪明钱情报

- 钱包扫描器追踪从排行榜种子的 Polymarket 顶级交易者
- 自动发现利润前 50 与成交量前 50 钱包
- 增量检测 — 发现新建仓、平仓、加仓、减仓
- 信念评分 — 巨鲸数量 × 美元规模合成信号
- Edge 集成 — 巨鲸与模型一致 → +8% Edge 加成；分歧 → -2% 惩罚
- 7 阶段流动性扫描流水线：
  - 从排行榜种子钱包 → 拉取市场 → 扫描全局成交 → 逐市场巨鲸扫描 → 地址排名 → 深度钱包分析 → 评分入库
- API 池在多个端点间轮询，支持 round-robin、最少负载、加权随机策略

### 市场微观结构

- 60 分钟、4 小时、24 小时窗口的订单流失衡
- VWAP 偏离 — 价格低于成交量加权均价时发出入场信号
- 巨鲸订单检测 — 标记单笔超过 $2,000 的成交
- 成交加速 — 检测异常活动激增（>2× 基线）
- 订单簿深度比 — 衡量买卖盘压力
- 智能入场计算器 — 综合所有信号推荐最优入场价

### 实时仪表盘

9 标签页 Flask 仪表盘，玻璃拟态暗色主题，端口 2345：

- **概览** — 引擎状态、循环次数、盈亏、权益曲线
- **交易引擎** — 启停控制、循环历史、流水线可视化
- **持仓** — 开仓实时盈亏、已平仓历史
- **预测** — 证据分解、模型 vs 市场概率、推理过程
- **风控与回撤** — 回撤仪表、热度等级、Kelly 乘数、敞口分解
- **聪明钱** — 追踪钱包、信念信号、巨鲸活动流
- **流动性扫描** — 7 阶段流水线状态、发现候选、API 池健康
- **绩效** — 胜率、ROI、Sharpe、Sortino、Calmar、类别分解、模型准确度
- **设置** — 环境状态、配置查看、Kill Switch 开关

由 `DASHBOARD_API_KEY` 保护。自动刷新，带实时状态指示。

### 可观测性与告警

- structlog JSON 日志，自动脱敏敏感数据
- 多渠道告警 — Telegram、Discord、Slack（带冷却防刷屏）
- 告警触发 — 交易、回撤警告、Kill Switch 激活、错误、日汇总
- Sentry 集成 — 可选错误追踪与数据清洗
- API 成本追踪 — 按调用估算 LLM 与搜索费用
- JSON 运行报告 — 可导出至 `reports/`

### 存储与审计

- SQLite WAL 模式，支持并发读写
- 10 次自动 schema 迁移
- 不可篡改审计链 — 每笔决策 SHA-256 完整性校验
- TTL 缓存 — 搜索结果（1 小时）、订单簿（30 秒）、LLM 响应（30 分钟）、市场列表（5 分钟）
- 自动备份与轮转（最多 10 份），通过 `make backup` 触发

---

## 快速开始

```
git clone https://github.com/dylanpersonguy/polymarket-ai-trading-bot.git
cd polymarket-ai-trading-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

在 `.env` 中添加 API 密钥 — 至少需要 `OPENAI_API_KEY` 和 `SERPAPI_KEY`。

```
make dashboard
```

打开 **http://localhost:2345**。

**帮助：** Telegram [@dexoryn777](https://t.me/dexoryn777)

---

## Docker

```
cp .env.example .env
docker compose up -d
```

---

## CLI 命令

```
bot scan --limit 20            # 发现市场
bot research --market <ID>     # 研究某市场
bot forecast --market <ID>     # 完整流水线：研究、预测、风控、定仓
bot paper-trade --market <ID>  # 模拟交易
bot trade --market <ID>        # 实盘交易（需 ENABLE_LIVE_TRADING=true）
bot engine start               # 持续交易循环
bot engine status              # 引擎健康状态
bot dashboard                  # 启动仪表盘
bot portfolio                  # 组合风控报告
bot drawdown                   # 回撤状态
bot arbitrage                  # 扫描套利机会
bot alerts                     # 告警历史
```

---

## 配置

所有配置位于 `config.yaml` 与 `.env`。

**必填密钥** — `OPENAI_API_KEY`、`SERPAPI_KEY`

**可选密钥** — `ANTHROPIC_API_KEY`、`GOOGLE_API_KEY`、`BING_API_KEY`、`TAVILY_API_KEY`、`DASHBOARD_API_KEY`

**实盘交易** — 设置 `ENABLE_LIVE_TRADING=true`，并添加 `POLYMARKET_API_KEY`、`POLYMARKET_API_SECRET`、`POLYMARKET_API_PASSPHRASE`、`POLYMARKET_PRIVATE_KEY`

---

## 安全说明

- 默认模拟运行 — 三道独立门（订单标志、配置标志、环境变量）均须允许才会实盘
- 四级回撤热度 — 逐步缩减仓位，20% 回撤时停止
- 代码库不含密钥 — 全部通过 `.env` 配置
- Docker 以非 root 用户运行

---

## 测试

```
make test
make lint
make format
```

---

## 作者与联系方式

**Dexoryn Labs** — Polymarket AI 交易自动化

- **Telegram：** [@dexoryn777](https://t.me/dexoryn777)（最快回复）
- **微信：** 扫描添加 **DexorynWe**

<p align="center">
  <img src="wechat.png" alt="微信二维码 — 扫描添加 DexorynWe 为好友" width="280"/>
</p>

---

## 许可证

MIT

---

在 Polymarket 交易存在**重大亏损风险**。Dexoryn 不对使用本软件造成的损失负责。钱包安全、配置与资金风险由您自行承担。

**请仅使用您可承受损失的资金进行交易。**

---

若本项目对您有帮助，欢迎 Star 仓库或提交 Issue/PR。有问题请联系 Telegram [@dexoryn777](https://t.me/dexoryn777) 或微信 **DexorynWe**。

*为预测市场社区而建。不构成投资建议。*
