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
    gosu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 关键:不缓冲 Python 输出,确保 print/traceback 立刻刷新到容器 stdout
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 兜底 chmod,确保 start.sh 可执行
RUN chmod +x /app/start.sh && ls -la /app/start.sh

# Claude Code CLI 拒绝以 root 跑 bypassPermissions 模式(安全护栏)。
# 创建非 root 用户 app,start.sh 会用 gosu 切到 app 跑实际服务。
# 容器仍以 root 启动,因为 Railway 挂载的 /data Volume 默认所有者是 root,
# start.sh 顶部会先 chown /data 再 su 到 app 用户。
RUN groupadd --system app && \
    useradd --system --gid app --home /app --shell /bin/bash app && \
    chown -R app:app /app

# Railway 会注入 PORT 环境变量
ENV PORT=8080
EXPOSE 8080

# 注意:不在这里 USER app —— 容器以 root 启动,start.sh 内部用 gosu 切换。
CMD ["bash", "/app/start.sh"]
