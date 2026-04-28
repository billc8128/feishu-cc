#!/bin/bash
set -ex
exec 2>&1

if [ "${SERVICE_ROLE:-}" = "browser" ]; then
    exec bash /app/browser/start.sh
fi

# ---- 第一阶段:root 进来 → 修 /data 所有权 → gosu 切到 app 用户重入 ----
if [ "$(id -u)" = "0" ]; then
    echo "===== bootstrap (root) ====="
    mkdir -p "${DATA_DIR:-/data}"
    mkdir -p "${DATA_DIR:-/data}/home/.claude"
    chown -R app:app "${DATA_DIR:-/data}"
    echo "chowned ${DATA_DIR:-/data} to app:app"
    exec gosu app bash /app/start.sh
fi

# ---- 第二阶段:app 用户运行真正的服务 ----
# 把 HOME 指到 Volume 里,这样 SDK 的 session(默认 ~/.claude/projects/)
# 就持久化在 /data/home/.claude/,跨容器重启不丢。
export HOME="${DATA_DIR:-/data}/home"

echo "===== feishu-cc startup diagnostics ====="
echo "running as: $(id)"
echo "HOME=${HOME}"
echo "PORT=${PORT:-not_set}"
echo "DATA_DIR=${DATA_DIR:-not_set}"
echo "FEISHU_APP_ID=${FEISHU_APP_ID:0:8}..."
echo "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL:-not_set}"
echo "ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-not_set}"
echo "Python version: $(python --version)"
echo "Working dir: $(pwd)"
echo "Files: $(ls)"

echo "===== import probe ====="
python -c "
import sys
print('Python sys.path:', sys.path)
try:
    print('importing config...'); import config
    print('importing feishu.events...'); from feishu import events
    print('importing feishu.client...'); from feishu import client
    print('importing project.manager...'); from project import manager
    print('importing app...'); import app
    print('ALL IMPORTS OK')
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)
"

echo "===== session storage check ====="
# Session 文件由 bundled CLI 写到 $HOME/.claude/projects/,每次启动扫一眼
# 方便排查"记忆丢失"这类问题
SESSION_COUNT=$(find "${HOME}/.claude/projects" -name "*.jsonl" 2>/dev/null | wc -l)
echo "persisted session files: ${SESSION_COUNT}"
find "${HOME}/.claude/projects" -name "*.jsonl" -printf "  %p (%s bytes, %TY-%Tm-%Td %TH:%TM)\n" 2>/dev/null | head -10 || true

echo "===== launching uvicorn on 0.0.0.0:${PORT} ====="
exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-level info
