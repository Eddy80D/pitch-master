# 路演大师 PitchMaster

JUNCTION CHINA 2026 · MiMo 赛道。输入 GitHub 仓库 → 解剖员产出证据包 → 5 位 AI 评委初评 → 自我介绍 → 答辩 → 复议 → 互怼 → 亮椅 → Markdown 报告，SSE 实时推送到网页。

## 本地运行

```bash
python -m pip install -r requirements.txt
cp .env.example .env   # 填入 MIMO_API_KEY / MIMO_BASE_URL
python -m uvicorn app.main:app --reload --port 8000
```

打开 http://127.0.0.1:8000 ，填一个 GitHub 仓库地址，点「开始评审」。

演示模式默认 4 问 / 每问 45 秒 / 介绍 30 秒；完整模式改 `.env` 里的三个值（10 / 120 / 180）。

### 支付与老板后台

- `PRECHECK_ENABLED=True`（默认）：提交后先零 token 预检仓库，展示"已读取 N 个文件 / M 处疑点"的悬念卡片，再收费。
- `PAYMENT_REQUIRED=True`（默认）：预检完成后出现收款码 `static/pay_qr.png`，点「我已支付」进入待放行；老板在 `/admin?token=<ADMIN_TOKEN>` 手动放行后才开始评审。
- `PAYMENT_REQUIRED=False`：跳过收款，预检后直接开跑（开发/评审演示用）。
- `PRICE_YUAN=19.9`：单场定价；`ADMIN_TOKEN` 为空时所有 `/api/admin/*` 返回 404。

老板后台：营收看板（今日/累计/待放行/待核验/逃单/测试单 + 纯 SVG 柱状/折线图）、订单列表、订单详情（transcript / 初评终评 / 质检通过或打回 / 免费重审 / 老板亲签）。订单流水写 `logs/orders.jsonl`，重启回放。

### 轻量注册（D3-growth）

- 提交表单新增「称呼/队名（必填）」与「联系方式（选填）」，无密码、无验证。
- 首次提交自动注册到 `logs/users.jsonl`；同称呼再次提交 `session_count+1`，预检卡片显示「欢迎回来，第 N 次登台」。
- 称呼写入报告标题与徽章位；联系方式只出现在 `.env` 之外的 `users.jsonl` 与 admin 用户列表，**不进 SSE / 公开快照 / 报告**。

### 成本看板

- `usage.jsonl` 每条记录新增 `session_id`（历史记录归为"未归属"）。
- `GET /api/admin/stats` 返回 cost 区块：今日 / 本月 / 历史累计 token、单均消耗（真实单）、按模型分组、pricing 就绪后的成本与毛利率。
- 订单详情显示「本单消耗 N tokens」；后台成本 vs 收入柱状图为纯 SVG，零外部依赖。

### 营业打磨（D3.5）

- `AUTO_APPROVE=True`（默认）：用户点「我已支付」后立即开审；订单进入「待核验」，老板事后在后台标记「已核验 / 逃单」。`False` 回退人工放行。
- `MAX_ACTIVE_SESSIONS_PER_IP=3`：同一 IP 最多 3 个非终态会话，超限返回 429「当前排队人数较多，请稍后再试」。
- 测试单：`PAYMENT_REQUIRED=False` 创建的订单自动标记为测试，不计营业额、不触发质检建议；后台可手动「标为测试 / 取消测试」。

### 闭环接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sessions` | 创建会话 |
| GET | `/api/sessions/{id}/events` | SSE 事件流（支持 Last-Event-ID 续传） |
| POST | `/api/sessions/{id}/intro` | 提交自我介绍（≤500 字） |
| POST | `/api/sessions/{id}/answer` | 提交当前问题回答（≤2000 字） |
| POST | `/api/sessions/{id}/skip` | 跳过答辩直达合议（仅 intro/qa） |
| GET | `/api/sessions/{id}/report` | 下载 Markdown 报告（202=生成中，404=未生成） |
| POST | `/api/sessions/{id}/paid` | 用户标记已支付 → pending |
| GET | `/api/admin/orders?token=…` | 待放行订单（错 token 返回 404） |
| POST | `/api/admin/approve/{id}?token=…` | 老板放行 → 启动流水线 |
| POST | `/api/admin/comment/{id}?token=…` | 老板亲签评语 |
| GET | `/api/admin/stats?token=…` | 营收看板聚合数据 |
| GET | `/api/admin/orders/{id}?token=…` | 订单详情（transcript / 评分） |
| POST | `/api/admin/qa/{id}?token=…` | 质检通过 / 打回 |
| POST | `/api/admin/rework/{id}?token=…` | 免费重审（新单 price=0） |
| POST | `/api/admin/refund/{id}?token=…` | 标记线下退款 |
| POST | `/api/admin/verify/{id}?token=…` | 自动放行单标记已核验 |
| POST | `/api/admin/evade/{id}?token=…` | 标记逃单 |
| POST | `/api/admin/test/{id}?token=…` | 标为/取消测试单 |
| GET | `/api/admin/users?token=…` | 注册用户列表（含联系方式） |

## 自测

```bash
python scripts/smoke.py                      # T1：pro 非流式 + flash 流式 + usage 记账
python scripts/test_state_machine.py         # T2/T9：等待点、超时、skip、事件序列
python scripts/test_full_loop.py             # T16：mock 全流程（不调 API）
python scripts/test_patch.py                 # D2 补丁：支付/admin/PRD 上限/HTML 提取（不调 API）
python scripts/test_d3.py                    # D3：预检/订单持久化/统计/质检/重审（不调 API）
python scripts/test_polish.py                # D3.5：测试单/自动放行/核验/IP 护栏（不调 API）
python scripts/test_growth.py                # D3-growth：注册/隐私/成本口径（不调 API）
python scripts/test_timer.py                 # 限时显示：需用短超时启动服务（见脚本头注释）
python scripts/test_e2e.py                   # T16：浏览器端到端（需 Chrome + playwright，会调真实 API）
python scripts/business_report.py            # 经营流水与成本分析（纯标准库，零 LLM）
```

## 目录

```
app/
  main.py            FastAPI 入口 + SSE
  config.py          .env 配置
  mimo_client.py     MiMo 调用 / 重试 / token 记账
  events.py          前后端事件合同
  state_machine.py   7 阶段状态机 + 会话存储
  schemas.py         证据包 / 评委结论 / JSON 解析
  repo_loader.py     仓库抓取 / 过滤 / 截断
  agents/analyst.py  解剖员（pro）
  agents/judges.py   初评 / 短评 / 复议 / 互怼 / 安可
  agents/report.py   报告生成 + 模板降级
judges.yaml          评委人设（改这里不用改代码）
static/index.html    文字渲染器 v0（含自我介绍 / 答辩 / 下载）
logs/usage.jsonl     token 记账（gitignore）
reports/{id}.md      生成的报告（gitignore）
```

## 部署（Sealos）

```bash
docker build -t pitchmaster .
docker run -p 8000:8000 --env-file .env pitchmaster
```

Sealos 上配置同样的环境变量（`MIMO_API_KEY`、`MIMO_BASE_URL`、`MODEL_PRO`、`MODEL_FLASH`），端口读 `PORT`（默认 8000）。SSE 已加 `X-Accel-Buffering: no`，反向代理不会缓冲。

> `.env` 已被 `.gitignore` / `.dockerignore` 排除，密钥不会进仓库或镜像。
