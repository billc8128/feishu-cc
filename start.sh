#!/bin/bash
set -ex
exec 2>&1

# ---- 第一阶段:root 进来 → 修 /data 所有权 → gosu 切到 app 用户重入 ----
if [ "$(id -u)" = "0" ]; then
    echo "===== bootstrap (root) ====="
    mkdir -p "${DATA_DIR:-/data}"
    chown -R app:app "${DATA_DIR:-/data}"
    echo "chowned ${DATA_DIR:-/data} to app:app"
    exec gosu app bash /app/start.sh
fi

# ---- 第二阶段:app 用户运行真正的服务 ----
echo "===== feishu-cc startup diagnostics ====="
echo "running as: $(id)"
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

echo "===== probing bundled Claude CLI ====="
echo "node version: $(node --version 2>&1 || echo 'NODE NOT INSTALLED')"
echo "npm version: $(npm --version 2>&1 || echo 'NPM NOT INSTALLED')"
BUNDLED_CLI=$(python -c "import claude_agent_sdk, os; print(os.path.join(os.path.dirname(claude_agent_sdk.__file__), '_bundled', 'claude'))" 2>&1)
echo "bundled cli path: ${BUNDLED_CLI}"
echo "bundled cli exists: $(test -e "${BUNDLED_CLI}" && echo yes || echo no)"
echo "bundled cli executable: $(test -x "${BUNDLED_CLI}" && echo yes || echo no)"
echo "bundled cli file type: $(file "${BUNDLED_CLI}" 2>&1 || echo 'no file cmd')"
echo "bundled cli first line: $(head -n 1 "${BUNDLED_CLI}" 2>&1 || echo unreadable)"
echo "--- attempting: ${BUNDLED_CLI} --version ---"
"${BUNDLED_CLI}" --version 2>&1 || echo "EXITED WITH CODE $?"
echo "--- attempting: node ${BUNDLED_CLI} --version ---"
node "${BUNDLED_CLI}" --version 2>&1 || echo "EXITED WITH CODE $?"

echo "===== launching uvicorn on 0.0.0.0:${PORT} ====="
exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-level info
