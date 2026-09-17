#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
并发压测 —— 测量"当前这个部署形态"到底能扛住多少并发用户。

    # 先起服务（另开一个终端）
    .venv\\Scripts\\python.exe run.py --prod --port 5055

    # 再压（用户数支持逗号列表，一次跑出整条曲线）
    .venv\\Scripts\\python.exe scripts\\loadtest.py --users 1,4,8,16,32
    .venv\\Scripts\\python.exe scripts\\loadtest.py --users 16 --scenario analytics

【为什么用标准库自己写，不装 locust / wrk】
  1. requirements.txt 里写明了"依赖少是刻意的"。压测是**一次性诊断工具**，
     不是运行时依赖 —— 为它引入一个要装一堆东西的框架，和那个原则冲突。
     urllib + threading 写 200 行就够了。
  2. 本项目所有受测页面都要登录，还要带 CSRF token。
     通用工具要额外写脚本才能做到，自己写反而更短。
  3. 顺带能从同一个进程里采样 MySQL 的 Threads_connected ——
     这是判断"连接池够不够"的直接证据，外部工具看不到。

【为什么不测出 RPS 就完事】
  单一的 RPS 数字没有意义，**延迟随并发上升的拐点**才是结论。
  所以默认跑一条并发梯度曲线：延迟还平着 = 还有余量；
  延迟开始翻倍 = 到拐点了。--users 传列表就是为了这个。

【测量口径的诚实说明】
  · 压测客户端和服务端在**同一台机器**上，互相抢 CPU。
    所以绝对延迟偏高、绝对 RPS 偏低。但拐点位置是可比的。
  · think time 默认 0 —— 测的是吞吐上限，不是"像真人一样用"。
    想模拟真人浏览加 --think 3。
"""

import argparse
import http.cookiejar
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from env_check import check  # noqa: E402

check()

import pymysql  # noqa: E402

from config import DB_CONFIG, DB_NAME  # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


# =============================================================================
# 受测场景
# =============================================================================
# 【为什么每个场景是一串"路径权重"而不是一个固定列表】
#   不同页面的成本差一个数量级：/dashboard 是几个 COUNT，
#   /analytics/drought 要扫 5480 行气象数据做 gaps-and-islands。
#   混在一起压只能得到一个平均数，看不出是谁把服务拖垮的。
#   所以拆成场景，分别压，才能定位。
#
#   weight 是该路径在一次循环里出现的次数 —— 用"重复占位"而不是
#   概率采样，是为了让每个 worker 的请求序列**确定性可复现**。
SCENARIOS = {
    # 日常混合：大部分时间在看列表页和看板，偶尔看一次分析
    'mixed': [
        ('/', 3),
        ('/plots', 2),
        ('/plantings', 2),
        ('/yields', 1),
        ('/weather', 1),
        ('/analytics/roi', 1),
        ('/analytics/abc', 1),
    ],
    # 纯分析页轮转 —— 这是最重的一档，用来找"几个人同时看分析会垮"
    'analytics': [
        ('/analytics/gdd', 1),
        ('/analytics/yoy-mom', 1),
        ('/analytics/correlation', 1),
        ('/analytics/roi', 1),
        ('/analytics/topn', 1),
        ('/analytics/abc', 1),
        ('/analytics/cohort', 1),
        ('/analytics/drought', 1),
    ],
    # 只看板 —— 轻档，用作基准线
    'dashboard': [('/', 1)],
    # 静态文件 —— 用来验证"静态交给 Nginx"能省下多少
    'static': [('/static/vendor/echarts.min.js', 1)],
}

# 一个场景能跑的前提是它至少有一条路径
_CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


# =============================================================================
# 单个压测线程
# =============================================================================

class Worker(threading.Thread):
    """
    一个"虚拟用户"。

    每个 worker 有**独立的 cookie jar**，各自登录一次，然后才开始施压。
    共用 cookie 的话测的就不是并发会话了，而且一旦有人 logout 会互相干扰。
    """

    def __init__(self, base, username, password, paths, stop_at, think,
                 start_gate, ready, idx, samples, errors):
        super().__init__(daemon=True)
        self.base = base
        self.username = username
        self.password = password
        self.paths = paths
        self.stop_at = stop_at
        self.think = think
        self.start_gate = start_gate
        self.ready = ready
        self.idx = idx
        self.samples = samples      # 本线程自己的样本列表，跑完再合并 —— 见下方说明
        self.errors = errors

    # ------------------------------------------------------------ 登录
    def _open(self, opener, path, limit=200):
        """
        发一个 GET，返回 (最终URL, 状态码, 耗时秒, 正文)。

        limit 默认只读前 200 字 —— 施压路径拿正文只是为了在报错时
        附一小段现场，全文读下来纯属浪费（大页面几 MB 会拖慢客户端，
        把瓶颈从服务端挪到压测机上，测出来的数就不是服务端的了）。
        需要解析页面的地方（登录取 CSRF）显式传 limit=None 读全文。
        """
        req = urllib.request.Request(self.base + path)
        t0 = time.perf_counter()
        try:
            with opener.open(req, timeout=60) as resp:
                body = resp.read() if limit is None else resp.read(limit)
                return resp.geturl(), resp.status, time.perf_counter() - t0, body
        except urllib.error.HTTPError as e:
            body = e.read(limit) if limit is not None else e.read()
            return req.full_url, e.code, time.perf_counter() - t0, body
        except Exception as e:                       # 超时 / 连接被拒 / 重置
            return req.full_url, 0, time.perf_counter() - t0, str(e).encode()

    def _login(self):
        """走完整登录流程：取 CSRF → POST /login。失败返回 None。"""
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        # ⚠️ 这里必须读全文：csrf_token 在 login.html 的第 27 行，
        #    用默认的 200 字节截断会取不到 token，登录静默失败。
        _, status, _, body = self._open(opener, '/login', limit=None)
        if status != 200:
            return None
        m = _CSRF_RE.search(body.decode('utf-8', 'replace'))
        if not m:
            return None

        data = urllib.parse.urlencode({
            'csrf_token': m.group(1),
            'username': self.username,
            'password': self.password,
        }).encode()
        req = urllib.request.Request(self.base + '/login', data=data)
        try:
            with opener.open(req, timeout=30) as resp:
                # 登录成功是 302 → 跟随后落到 /。
                # 停在 /login 说明没登上（比如账号被禁）。
                if resp.geturl().rstrip('/').endswith('/login'):
                    return None
                return opener
        except Exception:
            return None

    # ------------------------------------------------------------ 主循环
    def run(self):
        opener = self._login()
        if opener is None:
            self.errors.append((self.idx, '登录失败', '检查账号密码 / 服务是否已启动'))
            self.ready.set()
            return
        self.ready.set()          # 只是"我准备好了"，不代表可以开打

        # 所有 worker 都登录完再统一开始 —— 否则先登录的已经在压，
        # 后登录的还在跑登录流程，前几秒的并发数根本不是设定值。
        self.start_gate.wait()

        i = self.idx
        while time.time() < self.stop_at:
            path = self.paths[i % len(self.paths)]
            i += 1
            url, status, dt, body = self._open(opener, path)

            # 会话掉了会 302 到 /login 且跟随后是 200 —— 只看状态码会误判成功。
            # 所以额外检查最终 URL。
            if url.rstrip('/').endswith('/login'):
                self.errors.append((self.idx, path, '会话失效，被重定向到登录页'))
                self.samples.append((path, dt, 401))
            else:
                self.samples.append((path, dt, status))
                if status != 200:
                    self.errors.append(
                        (self.idx, path, f'HTTP {status}: '
                         f'{body.decode("utf-8", "replace")[:120]}'))
            if self.think:
                time.sleep(self.think)


# =============================================================================
# MySQL 侧观测
# =============================================================================

class MySQLSampler(threading.Thread):
    """
    压测期间采样 MySQL 的连接数。

    【为什么这个比客户端的 RPS 更重要】
      本项目是 IO 密集（时间几乎全花在等 MySQL），所以"服务端能开多少并发"
      的硬上限就是**连接池 + MySQL max_connections**。
      客户端看到的是"变慢了"，这里能看到"其实是被连接数卡住了" ——
      两者的修法完全不同。
    """

    def __init__(self, stop_at, interval=0.5):
        super().__init__(daemon=True)
        self.stop_at = stop_at
        self.interval = interval
        self.connected = []
        self.running = []
        self.error = None

    def run(self):
        try:
            conn = pymysql.connect(connect_timeout=5, **{
                **DB_CONFIG, 'database': DB_NAME,
                'cursorclass': pymysql.cursors.Cursor})
        except Exception as e:
            self.error = str(e)
            return
        try:
            cur = conn.cursor()
            while time.time() < self.stop_at:
                cur.execute("SHOW STATUS LIKE 'Threads_connected'")
                self.connected.append(int(cur.fetchone()[1]))
                cur.execute("SHOW STATUS LIKE 'Threads_running'")
                self.running.append(int(cur.fetchone()[1]))
                time.sleep(self.interval)
        except Exception as e:
            self.error = str(e)
        finally:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# 统计
# =============================================================================

def percentile(sorted_vals, p):
    """取 p 分位。样本少时退化成最大值，避免插值出好看的假象。"""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def ms(v):
    return f'{v * 1000:.0f}ms'


def expand(paths):
    """把 [(path, weight)] 展开成重复占位的确定性序列。"""
    out = []
    for p, w in paths:
        out.extend([p] * w)
    return out


# =============================================================================
# 跑一个并发档位
# =============================================================================

def run_level(base, creds, paths, users, duration, think, warmup):
    """跑一个并发档位，返回统计结果字典。"""
    # 预热：先单线程跑一遍，把模板缓存、连接池、MySQL 的缓冲池都填上。
    # 不预热的话第一档的数字里混着"冷启动成本"，和后面的档次不可比。
    if warmup:
        gate = threading.Event()
        gate.set()                      # 预热不参与"统一开打"，直接放行
        w = Worker(base, creds[0], creds[1], paths, time.time() + warmup, 0,
                   gate, threading.Event(), 0, [], [])
        w.start()
        w.join(timeout=warmup + 30)     # 兜底：预热卡住也不能把整轮拖死

    stop_at = time.time() + duration
    start_gate = threading.Event()
    samples, errors, workers, readies = [], [], [], []
    for i in range(users):
        r = threading.Event()
        readies.append(r)
        wk = Worker(base, creds[0], creds[1], paths, stop_at, think,
                    start_gate, r, i, samples, errors)
        workers.append(wk)
        wk.start()

    # 等所有虚拟用户登录完（最多 60 秒），再统一发令开打。
    t0 = time.time()
    for r in readies:
        r.wait(timeout=max(0.0, 60 - (time.time() - t0)))

    wall_start = time.time()
    start_gate.set()
    # 采样器在开打**之后**才启动 —— 否则会把"等所有用户登录"那几秒的
    # 空闲连接也平均进去，把平均连接数压低，看不出真实的池子压力。
    sampler = MySQLSampler(stop_at)
    sampler.start()
    for wk in workers:
        wk.join()
    wall = time.time() - wall_start
    sampler.join(timeout=5)

    # 按路径聚合
    by_path = {}
    for path, dt, status in samples:
        by_path.setdefault(path, []).append((dt, status))

    total = len(samples)
    ok = sum(1 for _, _, s in samples if s == 200)
    return {
        'users': users,
        'wall': wall,
        'total': total,
        'ok': ok,
        'rps': total / wall if wall else 0,
        'by_path': by_path,
        'errors': errors,
        'conn_peak': max(sampler.connected) if sampler.connected else None,
        'conn_avg': (sum(sampler.connected) / len(sampler.connected)
                     if sampler.connected else None),
        'run_peak': max(sampler.running) if sampler.running else None,
        'sampler_error': sampler.error,
        'lat': sorted(dt for _, dt, _ in samples),
    }


def print_level(r, prev_p95=None):
    print()
    print(f'  并发 {r["users"]:<3} 时长 {r["wall"]:.1f}s   '
          f'总计 {r["total"]} 请求   {r["rps"]:.1f} req/s   '
          f'成功 {r["ok"]}  失败 {r["total"] - r["ok"]}')
    print('  ' + '-' * 88)
    print(f'  {"路径":<28}{"次数":>6}{"p50":>9}{"p95":>9}{"p99":>9}{"最大":>9}{"错误":>6}')
    for path, rows in sorted(r['by_path'].items(),
                             key=lambda kv: -len(kv[1])):
        lat = sorted(dt for dt, _ in rows)
        errs = sum(1 for _, s in rows if s != 200)
        print(f'  {path:<28}{len(rows):>6}{ms(percentile(lat, .5)):>9}'
              f'{ms(percentile(lat, .95)):>9}{ms(percentile(lat, .99)):>9}'
              f'{ms(max(lat)):>9}{errs:>6}')
    print('  ' + '-' * 88)

    p95 = percentile(r['lat'], .95)
    verdict = ''
    if prev_p95:
        ratio = p95 / prev_p95
        if ratio > 2:
            verdict = f'   ← p95 比上一档涨了 {ratio:.1f} 倍，已经过拐点'
        elif ratio > 1.3:
            verdict = f'   ← p95 涨了 {ratio:.1f} 倍，接近拐点'
        else:
            verdict = f'   ← p95 基本持平（{ratio:.2f}x），仍有余量'
    print(f'  整体 p50 {ms(percentile(r["lat"], .5))}  '
          f'p95 {ms(p95)}  p99 {ms(percentile(r["lat"], .99))}{verdict}')

    if r['conn_peak'] is not None:
        print(f'  MySQL 连接数 峰值 {r["conn_peak"]} / 平均 {r["conn_avg"]:.1f}'
              f'（并发线程峰值 {r["run_peak"]}）')
    elif r['sampler_error']:
        print(f'  MySQL 采样失败：{r["sampler_error"]}')

    if r['errors']:
        seen = {}
        for idx, path, msg in r['errors']:
            seen.setdefault((path, msg), 0)
            seen[(path, msg)] += 1
        print(f'  ⚠️ {len(r["errors"])} 个错误，去重后 {len(seen)} 类：')
        for (path, msg), n in sorted(seen.items(), key=lambda kv: -kv[1])[:5]:
            print(f'      ×{n:<5} {path}  {msg}')
    return p95


# =============================================================================

def main():
    ap = argparse.ArgumentParser(
        description='智慧农业分析平台 —— 并发压测',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='场景：' + '、'.join(SCENARIOS) + '\n'
               '例：  python scripts/loadtest.py --users 1,4,8,16\n'
               '      python scripts/loadtest.py --users 16 --scenario analytics')
    ap.add_argument('--url', default='http://127.0.0.1:5055',
                    help='被测服务地址（默认 http://127.0.0.1:5055）')
    ap.add_argument('--users', default='10',
                    help='并发虚拟用户数，可传逗号列表：1,4,8,16,32')
    ap.add_argument('--scenario', default='mixed', choices=list(SCENARIOS),
                    help='受测场景（默认 mixed）')
    ap.add_argument('--duration', type=float, default=20,
                    help='每个并发档位的持续秒数（默认 20）')
    ap.add_argument('--think', type=float, default=0,
                    help='每次请求后的思考时间秒数（默认 0，即压吞吐上限）')
    ap.add_argument('--warmup', type=float, default=2,
                    help='预热秒数（默认 2；设 0 关闭）')
    ap.add_argument('--user', default='admin', help='登录账号（默认 admin）')
    ap.add_argument('--password', default='admin123', help='登录口令')
    args = ap.parse_args()

    base = args.url.rstrip('/')
    paths = expand(SCENARIOS[args.scenario])
    levels = [int(x) for x in args.users.split(',') if x.strip()]

    # 先确认服务活着，并确认账号能登上 —— 否则后面每个 worker 都白跑一遍
    probe = Worker(base, args.user, args.password, paths, 0, 0,
                   threading.Event(), threading.Event(), 0, [], [])
    if probe._login() is None:
        print(f'无法登录 {base}（账号 {args.user}）。')
        print('请确认：')
        print('  1. 服务已启动，例如  .venv\\Scripts\\python.exe run.py --prod --port 5055')
        print('  2. --url 指向的地址和端口一致')
        print('  3. 账号口令正确（--user / --password）')
        return 2

    print('=' * 90)
    print(f'  并发压测   场景={args.scenario}   并发档位={levels}   '
          f'每档 {args.duration:g}s   think={args.think:g}s')
    print(f'  目标 {base}   账号 {args.user}')
    print(f'  请求序列 {paths}')
    print('=' * 90)

    prev_p95 = None
    summary = []
    for n in levels:
        r = run_level(base, (args.user, args.password), paths,
                      n, args.duration, args.think, args.warmup)
        prev_p95 = print_level(r, prev_p95)
        summary.append((n, r['rps'], percentile(r['lat'], .5),
                        percentile(r['lat'], .95), r['ok'], r['total'],
                        r['conn_peak']))
        time.sleep(2)     # 档位之间歇一下，让连接池和 MySQL 回收

    print()
    print('=' * 90)
    print('  汇总')
    print('=' * 90)
    print(f'  {"并发":>5}{"RPS":>10}{"p50":>10}{"p95":>10}{"成功率":>10}{"连接峰值":>10}')
    for n, rps, p50, p95, ok, total, conn in summary:
        rate = ok / total * 100 if total else 0
        print(f'  {n:>5}{rps:>10.1f}{ms(p50):>10}{ms(p95):>10}'
              f'{rate:>9.1f}%{conn if conn is not None else "-":>10}')
    print()
    print('  读法：p95 开始翻倍的那一档，就是当前配置的容量上限。')
    print('        RPS 不再涨而 p95 猛涨 = 已经排队了，再加并发只会更慢。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
