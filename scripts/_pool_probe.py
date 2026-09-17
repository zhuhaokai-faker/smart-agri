#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
【一次性诊断脚本，不是项目的一部分】

用来回答一个问题：mixed 场景下 RPS 在并发 8 之后就不再涨、
而 MySQL 连接数恰好停在 21（= pool_size 10 + max_overflow 10 + 1），
那么**天花板是连接池，还是别的**？

做法：不改 config.py，只在进程内把 SQLALCHEMY_ENGINE_OPTIONS 换掉再建 app。
Flask-SQLAlchemy 是在 init_app 那一刻读这份配置的，
所以在这里改类属性就能生效，源码一行不用动 —— 对比实验最忌讳
"为了做 A 顺便改了 B"，那样测出来的差异就说不清是谁造成的。

    python scripts/_pool_probe.py --port 5056 --pool 40 --overflow 20
"""

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from env_check import check  # noqa: E402

check()

from config import Config  # noqa: E402  （必须在 check() 之后）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=5056)
    ap.add_argument('--pool', type=int, default=40)
    ap.add_argument('--overflow', type=int, default=20)
    ap.add_argument('--timeout', type=float, default=10)
    args = ap.parse_args()

    opts = dict(Config.SQLALCHEMY_ENGINE_OPTIONS)
    opts.update(pool_size=args.pool, max_overflow=args.overflow,
                pool_timeout=args.timeout)
    Config.SQLALCHEMY_ENGINE_OPTIONS = opts

    from app import create_app
    app = create_app()

    print(f'池参数 pool_size={args.pool} max_overflow={args.overflow} '
          f'pool_timeout={args.timeout}')
    print(f'监听 http://127.0.0.1:{args.port}')
    app.run(host='127.0.0.1', port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
