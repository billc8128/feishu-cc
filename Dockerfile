# syntax=docker/dockerfile:1
FROM python:3.12-slim

# 基础工具:
#   git    - clone 项目 + Claude 用 Bash 跑 git 命令
#   curl   - Claude 用来下载东西
#   nodejs - claude-agent-sdk 自带 CLI 是 Node 写的
#   ca-certificates - HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    nodejs \
    npm \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway 会注入 PORT 环境变量
ENV PORT=8080
EXPOSE 8080

# 启动脚本:先做 import 探针 + 环境诊断,再起 uvicorn。
# 这样如果 import 失败能在 Railway 日志里看到完整 traceback。
CMD ["/app/start.sh"]
