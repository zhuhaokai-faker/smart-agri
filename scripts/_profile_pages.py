#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
【一次性诊断脚本，不是项目的一部分】

把一个页面请求拆成两半：**等数据库的时间** 和 **Python 自己的时间**。

做法：挂 SQLAlchemy 的 before/after_cursor_execute 事件，累计每个请求
真正花在 SQL 上的时间；再用 test_client 量整页耗时，两者相减
就是 ORM 水合 + Jinja 渲染 + 蓝图逻辑的成本。

【为什么必须拆开】
  这两半的修法完全不同，而且互相不能替代：
    · SQL 占大头 → 加索引、改查询。（加进程只会在排队的人变多，不会变快）
    · Python 占大头 → 加进程/加机器，因为 GIL 决定了单进程只能用满一个核。
  不拆开就拍脑袋加进程，是压测里最常见的白干。

用 test_client 是**有意**的：不经过网络和 HTTP 解析，测的就是
"处理这个页面本身要多久"，把网络栈的噪声排除掉。
"""

import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from env_check import check  # noqa: E402

check()

from sqlalchemy import event  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PAGES = ['/', '/plots', '/plantings', '/yields', '/weather', '/farms', '/crops',
         '/analytics/gdd', '/analytics/yoy-mom', '/analytics/correlation',
         '/analytics/roi', '/analytics/topn', '/analytics/abc',
         '/analytics/cohort', '/analytics/drought', '/audit/']

_CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


def _install_profiler(engine, stats, slowest):
    """把每条 SQL 的耗时累计进 stats，同时记下最慢的那一条。"""

    @event.listens_for(engine, 'before_cursor_execute')
    def _before(conn, cursor, statement, params, context, executemany):
        conn.info.setdefault('_t0', []).append(time.perf_counter())

    @event.listens_for(engine, 'after_cursor_execute')
    def _after(conn, cursor, statement, params, context, executemany):
        dt = time.perf_counter() - conn.info['_t0'].pop()
        stats['n'] += 1
        stats['sql'] += dt
        if dt > slowest['ms']:
            slowest['ms'] = dt
            slowest['sql'] = ' '.join(statement.split())[:90]


def main():
    app = create_app()
    stats = {'n': 0, 'sql': 0.0}
    slowest = {'sql': '', 'ms': 0.0}

    # 事件要挂在 engine 上，而 db.engine 只有在应用上下文里才拿得到
    # （Flask-SQLAlchemy 3.x 是按 app 存 engine 的）。
    with app.app_context():
        _install_profiler(db.engine, stats, slowest)

    with app.test_client() as c:
        token = _CSRF_RE.search(c.get('/login').get_data(as_text=True)).group(1)
        c.post('/login', data={'csrf_token': token,
                               'username': 'admin', 'password': 'admin123'})

        print('  test_client 逐页拆解（各跑 3 次取中位数）')
        print('  ' + '-' * 82)
        print(f'  {"页面":<24}{"整页":>10}{"SQL 时间":>12}{"SQL 条数":>10}'
              f'{"SQL 占比":>10}   Python')
        print('  ' + '-' * 82)

        totals = []
        for path in PAGES:
            times, sqls, counts = [], [], []
            for _ in range(3):
                stats['n'], stats['sql'] = 0, 0.0
                t0 = time.perf_counter()
                r = c.get(path)
                wall = time.perf_counter() - t0
                if r.status_code != 200:
                    break
                times.append(wall)
                sqls.append(stats['sql'])
                counts.append(stats['n'])
            if not times:
                print(f'  {path:<24}{"非 200，跳过":>10}')
                continue
            times.sort(); sqls.sort(); counts.sort()
            wall, sqlt = times[1], sqls[1]
            n = counts[1]
            share = sqlt / wall * 100 if wall else 0
            totals.append((path, wall, sqlt, n))
            print(f'  {path:<24}{wall * 1000:>9.1f}ms{sqlt * 1000:>11.1f}ms'
                  f'{n:>10}{share:>9.0f}%{(wall - sqlt) * 1000:>9.1f}ms')

        print('  ' + '-' * 82)
        tw = sum(t[1] for t in totals)
        ts = sum(t[2] for t in totals)
        tn = sum(t[3] for t in totals)
        print(f'  {"合计":<24}{tw * 1000:>9.1f}ms{ts * 1000:>11.1f}ms'
              f'{tn:>10}{ts / tw * 100:>9.0f}%{(tw - ts) * 1000:>9.1f}ms')
        print()
        print(f'  最慢的单条 SQL：{slowest["ms"] * 1000:.1f}ms')
        print(f'    {slowest["sql"]}')
        print()
        print('  读法：SQL 占比高 → 优化查询；Python 列高 → 加进程（GIL 只能用满一个核）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
