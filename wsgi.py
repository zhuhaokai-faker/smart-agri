#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WSGI 入口 —— 对外部署用。waitress（Windows）和 gunicorn（Linux/容器）共用这一个文件。

    Windows:  waitress-serve --port=8001 --threads=8 wsgi:app
    Linux:    gunicorn -w 4 -k gthread --threads 8 wsgi:app

    容器里通常再套一层 nginx，见 deploy/nginx.conf。

【为什么不复用 run.py】
  run.py 起的是 Flask **内置开发服务器**：单进程 + 线程。
  而 GIL 决定了单进程的 Python 字节码最多用满一个核 —— 这是实测出来的天花板，
  不是理论担忧（scripts/loadtest.py，32 个并发客户端，同一台机器）：

        1 进程      176 RPS
        2 进程      278 RPS   （1.58×）
        4 进程      425 RPS   （2.42×）

  同一轮压测里，纯静态文件能跑到 1126 RPS，裸 MySQL 能跑到 5000+ 查询/秒，
  说明瓶颈既不在 Flask 也不在数据库，就在"一个进程只有一个 GIL"这件事上。
  所以对外部署必须多进程，而多进程需要 WSGI 服务器接管 —— 这个文件就是它的入口。

【为什么安全检查从这个文件里也走一遍】
  run.py 有两道启动前检查：debug 绑非回环地址、--prod 下 SECRET_KEY 仍是占位值。
  后一道在这里同样必须拦 —— 而且更要拦：占位密钥是公开的，
  任何人都能用它伪造 session cookie 直接变成管理员。
  多进程部署往往由 systemd / docker 拉起，**没有人盯着终端**，
  写进日志的警告不会有人看，所以这里选择直接拒绝启动而不是打印警告。

  前一道（debug + 非回环）不在这里查：这是 WSGI 服务器，根本没有 Flask 调试器，
  没有那个攻击面。安全检查只拦真实存在的风险，不然就成了狼来了。

【gunicorn 提示】
  容器里用 -k gthread 而不是默认的 sync worker：
  本项目 84% 的时间在等 MySQL（实测），是 IO 密集型，
  线程能把等待时间重叠起来。sync worker 会让每个请求独占一个进程直到 SQL 返回。
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 环境预检必须放在所有第三方 import 之前：解释器选错了的话，
# 报错会指向 app/extensions.py 而不是"你该用 .venv 里的 python"。见 env_check.py。
from env_check import check  # noqa: E402

check()

from run import KNOWN_WEAK_SECRETS  # noqa: E402  （复用同一份占位密钥清单）
from app import create_app  # noqa: E402

app = create_app()

if app.config.get('SECRET_KEY') in KNOWN_WEAK_SECRETS:
    print('=' * 64, file=sys.stderr)
    print('  [拒绝启动] SECRET_KEY 还是占位值', file=sys.stderr)
    print('=' * 64, file=sys.stderr)
    print('  占位密钥是公开的，任何人都能用它伪造 session cookie，', file=sys.stderr)
    print('  不猜密码就直接变成管理员，而且日志里看不出任何异常。', file=sys.stderr)
    print('', file=sys.stderr)
    print('  生成一个：', file=sys.stderr)
    print('    python -c "import secrets;print(secrets.token_urlsafe(48))"', file=sys.stderr)
    print('  写进 .env 的 SECRET_KEY=... 即可。', file=sys.stderr)
    print('=' * 64, file=sys.stderr)
    raise SystemExit(2)
