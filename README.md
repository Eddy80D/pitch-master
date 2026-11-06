# 路演大师 PitchMaster

JUNCTION CHINA 2026 · MiMo 赛道。输入 GitHub 仓库 → 解剖员产出证据包 → 5 位 AI 评委并行初评，SSE 实时推送到网页。

## 本地运行

```bash
python -m pip install -r requirements.txt
cp .env.example .env   # 填入 MIMO_API_KEY / MIMO_BASE_URL
python -m uvicorn app.main:app --reload --port 8000
```

打开 http://127.0.0.1:8000 ，填一个 GitHub 仓库地址，点「开始评审」。

## 自测

```bash
python scripts/smoke.py                      # T1：pro 非流式 + flash 流式 + usage 记账
python scripts/test_state_machine.py         # T2：事件序列
python scripts/test_e2e.py                   # T7：浏览器端到端（需本机 Chrome + playwright）
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
  agents/judges.py   5 评委并行初评（flash）
judges.yaml          评委人设（改这里不用改代码）
static/index.html    文字渲染器 v0
logs/usage.jsonl     token 记账（gitignore）
```

## 部署（Sealos）

```bash
docker build -t pitchmaster .
docker run -p 8000:8000 --env-file .env pitchmaster
```

Sealos 上配置同样的环境变量（`MIMO_API_KEY`、`MIMO_BASE_URL`、`MODEL_PRO`、`MODEL_FLASH`），端口读 `PORT`（默认 8000）。SSE 已加 `X-Accel-Buffering: no`，反向代理不会缓冲。

> `.env` 已被 `.gitignore` / `.dockerignore` 排除，密钥不会进仓库或镜像。
