# 文净引擎 Demo（前后端打通版）

## 本地启动

在目录 `C:\Users\zhaoyan\Documents\网页` 执行：

```powershell
python server.py
```

启动后访问：

- http://127.0.0.1:8000

## 当前能力

- 支持上传 `.docx/.txt/.md` 文件（最大 25MB）
- 任务异步处理，前端展示真实进度条
- 处理完成后返回下载链接

## 接口

- `POST /api/process`：提交任务，返回 `job_id`
- `GET /api/job/{job_id}`：查询状态（`queued/processing/completed/failed`）与进度
- `GET /api/download/{name}`：下载结果
- `GET /healthz`：健康检查

## 真实 AI 接口配置（推荐 .env）

在项目根目录创建 `.env`：

```env
OPENAI_API_KEY=你的Key
OPENAI_MODEL=gpt-5.5
# 可选：OPENAI_API_URL=https://api.openai.com/v1/responses
```

服务启动时会自动读取 `.env`。

## Render 部署（无自有域名）

Render 会给你一个免费二级域名：`https://xxx.onrender.com`

1. 把项目推到 GitHub（务必确认 `.env` 不提交）
2. 登录 [Render](https://render.com/)
3. New + → Web Service
4. 连接你的 GitHub 仓库
5. 配置：
   - Environment: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python server.py`
6. 在 Render 的 Environment Variables 里添加：
   - `OPENAI_API_KEY=你的新Key`
   - `OPENAI_MODEL=gpt-5.5`
7. 点击 Create Web Service，等待部署完成
8. 打开分配到的 `onrender.com` 链接即可对外访问

## 长文处理机制

- 单段调用上限 `MAX_AI_CHARS=12000`
- 超过上限自动分段（默认每段约 `10000` 字，重叠 `400` 字）调用 AI 后合并

## 回退机制

- 若未配置 Key、网络失败或接口失败，自动回退到本地规则引擎
- 返回包含 `engine` 与 `notice` 字段

## 安全建议

- `.env` 已加入 `.gitignore`
- 任何泄露过的 Key 都建议尽快在控制台撤销并重建

## 限制说明

- `.doc` 为旧二进制格式，当前不支持，建议先另存为 `.docx`
