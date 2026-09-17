#!/bin/bash
# =============================================================================
#  容器入口 —— 把 nginx 和 gunicorn 两个进程一起管起来
# =============================================================================
#  容器里放两个进程是有意的取舍（见 Dockerfile 头部）：镜像能整包带走，
#  代价就是得自己当这个监工。这里做的事和 Windows 版的
#  deploy/serve_cluster.py 是同一套路子：
#
#    · 启动前把"起不来"的情况拦在前面，并说清楚原因
#    · 等上游真的开始监听，再起前面的入口
#    · 任何一个进程意外退出，立刻整体收摊（不留半个在跑）
#    · 收到停止信号时由外向内收：nginx → gunicorn
#
#  三者都不是形式主义，理由在下面各段里。
# =============================================================================
set -euo pipefail

APP_DIR=/app
VENV_PY="$APP_DIR/.venv/bin/python"
PORT=8000

WORKERS="${GUNICORN_WORKERS:-4}"
THREADS="${GUNICORN_THREADS:-8}"

echo "============================================================"
echo "  智慧农业种植管理与产量分析平台 —— 容器启动"
echo "============================================================"
echo "  时区       $(date '+%Z %z')"
echo "  解释器     $VENV_PY"

# ---------------------------------------------------------------- 启动前检查
# 这两件事都会以"每个请求 500"或"worker 反复重启"的形式出现，日志里看不出
# 真正的原因。所以在起进程之前先查一次，直接拒绝启动 —— 和 wsgi.py 里那道
# SECRET_KEY 检查是同一个立场：能拦在启动前就别留到运行期。
if ! "$VENV_PY" - <<'PY'
import sys
sys.path.insert(0, '/app')

import pymysql

from config import Config, DB_CONFIG, DB_NAME
from run import KNOWN_WEAK_SECRETS

if Config.SECRET_KEY in KNOWN_WEAK_SECRETS:
    print('  [拒绝启动] SECRET_KEY 还是占位值（或者压根没传进来）。')
    print()
    print('  占位密钥是公开的：任何人都能用它伪造 session cookie，')
    print('  不猜密码就直接变成管理员，而且日志里看不出任何异常。')
    print()
    print('  容器的配置全部由运行时注入，镜像里不烘焙任何口令：')
    print('      docker run --env-file docker/container.env ...')
    print('  生成一个：')
    print('      python -c "import secrets;print(secrets.token_urlsafe(48))"')
    sys.exit(1)

try:
    pymysql.connect(connect_timeout=5, database=DB_NAME, **DB_CONFIG).close()
except Exception as e:
    print(f'  [拒绝启动] 连不上 MySQL：{e}')
    print()
    print(f'  当前目标：{DB_CONFIG["user"]}@{DB_CONFIG["host"]}:'
          f'{DB_CONFIG["port"]}/{DB_NAME}')
    print()
    print('  容器连宿主机的库时来源主机不是 localhost，MySQL 里只有')
    print("  root@localhost 的话必然 Access denied。")
    print('  先执行 docker/create-db-user.sql 建一个专用账号；')
    print('  Linux 上跑还要把 host.docker.internal 换成宿主机地址。')
    sys.exit(1)

print(f'  MySQL      {DB_CONFIG["host"]}:{DB_CONFIG["port"]}/{DB_NAME}  连接正常')
PY
then
    echo "============================================================"
    exit 1
fi

# ------------------------------------------------------------ 连接数预算
# 和 Windows 侧 serve_cluster.py 算的是同一笔账：
#     总连接数 = 进程数 × (pool_size + max_overflow)   ← 相乘关系
# config.py 里的池子默认值（10 + 10）是按"单进程开发"定的，4 个进程一乘就是
# 80 条，MySQL 默认 max_connections 才 151 —— 再加几个人就顶穿，而报错是运行期的
# "Too many connections"，启动时一切正常。所以容器里必须按进程数重新给。
# 池子只要够用（≥ 每进程线程数）即可，加大多出来的全是空转连接。
export DB_POOL_SIZE="${DB_POOL_SIZE:-$THREADS}"
export DB_MAX_OVERFLOW="${DB_MAX_OVERFLOW:-2}"

echo "  gunicorn   ${WORKERS} 进程 × ${THREADS} 线程  (:$PORT)"
echo "  连接预算   ${WORKERS} × (${DB_POOL_SIZE} + ${DB_MAX_OVERFLOW})" \
     "= $((WORKERS * (DB_POOL_SIZE + DB_MAX_OVERFLOW))) 条"
echo "============================================================"

# ---------------------------------------------------------------- 进程编排
GUNICORN_PID=""
NGINX_PID=""

cleanup() {
    local rc="${1:-0}"
    trap - TERM INT EXIT
    echo "[entrypoint] 收尾中..."

    # 由外向内，且**严格有先后**：先让 nginx 优雅退出（QUIT 会立刻关闭监听
    # 套接字，不再接新连接，但会把在途请求处理完），等它退干净了再让
    # gunicorn 收工。反过来做的话，中间那几秒里外面的人会连到一个上游已经
    # 停了的 nginx，拿到一堆 502。
    if [ -n "$NGINX_PID" ]; then
        kill -QUIT "$NGINX_PID" 2>/dev/null || true
        wait "$NGINX_PID" 2>/dev/null || true
        echo "[entrypoint] nginx 已停止"
    fi
    if [ -n "$GUNICORN_PID" ]; then
        kill -TERM "$GUNICORN_PID" 2>/dev/null || true
        wait "$GUNICORN_PID" 2>/dev/null || true
        echo "[entrypoint] gunicorn 已停止"
    fi
    exit "$rc"
}
trap 'cleanup 0' TERM INT

# --chdir 到 /app，wsgi:app 才能被找到（wsgi.py 里有 sys.path 处理，
# 但它自己得先能被 import）。
#
# 不记 access log：nginx 已经每个请求记一行了，gunicorn 再记一遍就是双份。
# error log 走 stderr，交给 docker logs。
gunicorn -w "$WORKERS" -k gthread --threads "$THREADS" \
         -b "127.0.0.1:$PORT" \
         --chdir "$APP_DIR" \
         --error-logfile - \
         --timeout 60 --graceful-timeout 30 \
         wsgi:app &
GUNICORN_PID=$!

# 等 gunicorn 真的开始监听，再起 nginx。顺序反过来的话，nginx 起来后的头几个
# 请求会撞上还没绑定的上游，拿到 502 —— 容器启动的那几秒正好有人在访问，
# 就是这个现象。
# 2>/dev/null 是必需的：探测没连上时 curl 会往 stderr 打一行
# "Failed to connect to 127.0.0.1 port 8000"，那行会永久留在 docker logs 里 ——
# 每次启动都有一条看着像故障的报错，真出故障时就没人当回事了。
for _ in $(seq 1 40); do
    if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login" 2>/dev/null; then
        break
    fi
    if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then
        echo "[entrypoint] [错误] gunicorn 启动过程中就退出了，看上面的日志。"
        cleanup 1
    fi
    sleep 0.5
done

if ! curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login" 2>/dev/null; then
    echo "[entrypoint] [错误] gunicorn 20 秒内没有开始响应，不再起 nginx。"
    cleanup 1
fi

echo "[entrypoint] gunicorn 就绪，启动 nginx..."
nginx -g 'daemon off;' &
NGINX_PID=$!

# 谁都行，只要有一个退出就整体收摊 —— 和 serve_cluster.py 同一个理由：
# 留一半在跑只会制造"时好时坏"的现象，而那是最难排查的一类问题。
# （容器外面有 HEALTHCHECK 和重启策略兜底，但前提是容器得先真的退出。）
wait -n || true
echo "[entrypoint] [错误] 有进程退出了，整体收尾。"
cleanup 1
