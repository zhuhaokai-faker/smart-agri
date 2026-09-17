#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
【一次性诊断脚本，不是项目的一部分】

绕开 Flask 和 SQLAlchemy，用裸 pymysql 多线程反复执行**与页面完全相同的 SQL**，
测 MySQL 本身的吞吐上限。

目的：把"应用层慢"和"数据库慢"分开。
  · 裸 SQL 的 RPS ≈ 页面 RPS   → 瓶颈在 MySQL，优化方向是 SQL / 索引 / 服务端参数
  · 裸 SQL 的 RPS >> 页面 RPS  → 瓶颈在应用层（ORM 对象水合、Jinja 渲染、GIL）

不这样分开测，很容易得出"加进程就完事了"这种错结论：
进程加上去了，但真正排队的地方一点没动。
"""

import argparse
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from env_check import check  # noqa: E402

check()

import pymysql  # noqa: E402

from config import DB_CONFIG, DB_NAME  # noqa: E402


def workload():
    """一个"页面"相当于跑这几条 SQL —— 和 /dashboard 的取数一致。"""
    return [
        """SELECT
            (SELECT COUNT(*) FROM farm)                        AS farm_cnt,
            (SELECT COUNT(*) FROM plot)                        AS plot_cnt,
            (SELECT ROUND(SUM(area_mu), 1) FROM plot)          AS total_area_mu,
            (SELECT COUNT(*) FROM planting WHERE status IN (20,30,40)) AS planting_cnt,
            (SELECT COUNT(*) FROM yield_record)                AS yield_record_cnt,
            (SELECT ROUND(SUM(yield_kg) / 1000, 1) FROM yield_record)  AS total_yield_ton,
            (SELECT ROUND(SUM(output_value) / 10000, 1) FROM yield_record) AS total_value_wan,
            (SELECT ROUND(SUM(yield_kg) / NULLIF(SUM(harvest_area_mu), 0), 1)
               FROM yield_record)                              AS avg_yield_per_mu""",
    ]


def worker(stop_at, sqls, counter, idx):
    conn = pymysql.connect(**{**DB_CONFIG, 'database': DB_NAME,
                              'autocommit': True})
    cur = conn.cursor()
    n = 0
    while time.time() < stop_at:
        for s in sqls:
            cur.execute(s)
            cur.fetchall()
        n += 1
    counter[idx] = n
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--users', default='1,4,8,16,32')
    ap.add_argument('--duration', type=float, default=10)
    args = ap.parse_args()

    sqls = workload()
    print(f'裸 SQL 压测（{len(sqls)} 条/轮，等同 /dashboard 的取数）')
    print(f'  {"并发":>5}{"轮/秒":>12}{"SQL/秒":>12}{"平均每轮":>12}')
    print('  ' + '-' * 44)

    for n in [int(x) for x in args.users.split(',')]:
        stop_at = time.time() + args.duration
        counter = [0] * n
        threads = [threading.Thread(target=worker,
                                    args=(stop_at, sqls, counter, i))
                   for i in range(n)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.time() - t0
        total_rounds = sum(counter)
        rps = total_rounds / wall
        # 闭式并发下的平均每轮耗时（Little's law：N 个线程并行 N 轮的总时间 / 轮数）
        avg_round = n * wall / total_rounds * 1000 if total_rounds else 0
        print(f'  {n:>5}{rps:>12.1f}{rps * len(sqls):>12.1f}'
              f'{avg_round:>11.1f}ms')

    print()
    print('  对比：应用层 /dashboard 在并发 8 时约 200 req/s（含 3 条 SQL）。')
    print('        裸 SQL 明显更高 → 差额就是 ORM + Jinja + GIL 的成本。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
