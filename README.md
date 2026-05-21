# 文净引擎（AIGC率检测 + 定向降重 + 多角色）

## 功能概览

- AIGC疑似率检测：`POST /api/aigc/check`
- 论文定向降重：优先改写高风险段落，不做全篇无差别重写
- 异步任务与进度条：上传后可实时查看进度
- 用户体系：注册、登录、退出、个人任务列表
- 管理员面板：用户列表、任务列表、统计概览

## 本地启动

```powershell
python server.py
```

访问：
- http://127.0.0.1:8000

## 默认管理员

- 用户名：`admin`
- 密码：`admin123456`

可通过环境变量修改：
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`

## LLM 提供商切换

支持 `openai` / `deepseek`，通过 `LLM_PROVIDER` 切换。

### OpenAI

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=your_key
OPENAI_MODEL=gpt-5.5
OPENAI_API_URL=https://api.openai.com/v1/responses
```

### DeepSeek

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_key
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_API_URL=https://api.deepseek.com/chat/completions
```

## 核心接口

- `POST /api/register` 注册
- `POST /api/login` 登录
- `POST /api/logout` 退出
- `GET /api/me` 当前用户
- `POST /api/aigc/check` 文本检测
- `POST /api/process` 上传处理（需登录）
- `GET /api/job/{job_id}` 任务详情（需登录）
- `GET /api/my/jobs` 我的任务（需登录）
- `GET /api/download/{name}` 下载结果（需登录）
- `GET /api/admin/users` 管理员用户列表
- `GET /api/admin/jobs` 管理员任务列表
- `GET /api/admin/stats` 管理员统计
- `GET /api/version` 版本与模型信息
- `GET /healthz` 健康检查

## Render 部署

1. 推送到 GitHub
2. Render 新建 Web Service
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python server.py`
5. 配置环境变量（按 OpenAI 或 DeepSeek）
6. Deploy latest commit

## 说明

- AIGC率为平台内部估计分值，用于风险定位与改写辅助，不等同于外部检测平台最终结果。
- 请在正式提交前进行人工终审。
