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

- `PAYMENT_REQUIRED=True`（默认）：用户创建会话后先看到收款码 `static/pay_qr.png`，点「我已支付」进入待放行；老板在 `/admin?token=<ADMIN_TOKEN>` 手动放行后才开始评审。
- `PAYMENT_REQUIRED=False`：跳过收款，直接开跑（开发/评审演示用）。
- `ADMIN_TOKEN` 为空时，所有 `/api/admin/*` 返回 404。

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

## 自测

```bash
python scripts/smoke.py                      # T1：pro 非流式 + flash 流式 + usage 记账
python scripts/test_state_machine.py         # T2/T9：等待点、超时、skip、事件序列
python scripts/test_full_loop.py             # T16：mock 全流程（不调 API）
python scripts/test_patch.py                 # D2 补丁：支付/admin/PRD 上限/HTML 提取（不调 API）
python scripts/test_e2e.py                   # T16：浏览器端到端（需 Chrome + playwright，会调真实 API）
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
