#!/bin/bash
set -ex
exec 2>&1

if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/data}"
    chown -R app:app "${DATA_DIR:-/data}"
    exec gosu app bash /app/browser/start.sh
fi

export HOME="${DATA_DIR:-/data}/home"
mkdir -p "${HOME}"

echo "===== browser-service startup ====="
echo "running as: $(id)"
echo "HOME=${HOME}"
echo "PORT=${PORT:-not_set}"
echo "DATA_DIR=${DATA_DIR:-not_set}"

python -c "
import browser.app
print('browser imports ok')
"

exec uvicorn browser.app:app --host :: --port ${PORT} --workers 1 --log-level info
