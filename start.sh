#!/bin/bash
set -ex
exec 2>&1

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

echo "===== probing Claude session storage ====="
echo "--- HOME tree ---"
ls -la "${HOME}" 2>&1 || echo "(HOME does not exist)"
echo "--- HOME/.claude tree ---"
ls -la "${HOME}/.claude" 2>&1 || echo "(~/.claude does not exist)"
echo "--- HOME/.claude/projects tree ---"
ls -la "${HOME}/.claude/projects" 2>&1 || echo "(~/.claude/projects does not exist)"
echo "--- recursive scan for .jsonl session files under HOME ---"
find "${HOME}/.claude" -name "*.jsonl" -printf "%p %s bytes, mtime=%TY-%Tm-%Td %TH:%TM\n" 2>&1 | head -40 || echo "(no jsonl files)"
echo "--- recursive scan for .jsonl anywhere under /data ---"
find /data -name "*.jsonl" 2>/dev/null | head -20 || true
echo "--- recursive scan for .jsonl anywhere under /root (in case CLI ignores HOME) ---"
find /root -name "*.jsonl" 2>/dev/null | head -20 || true
echo "--- recursive scan for .jsonl anywhere under /home ---"
find /home -name "*.jsonl" 2>/dev/null | head -20 || true
echo "--- bundled CLI subcommands (looking for session list tools) ---"
BUNDLED_CLI=$(python -c "import claude_agent_sdk, os; print(os.path.join(os.path.dirname(claude_agent_sdk.__file__), '_bundled', 'claude'))" 2>&1)
"${BUNDLED_CLI}" --help 2>&1 | head -60 || echo "(help failed)"

echo "===== launching uvicorn on 0.0.0.0:${PORT} ====="
exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-level info
