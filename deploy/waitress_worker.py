#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
单个 waitress 工作进程 —— 由 serve_cluster.py 拉起 N 个，前面挂 nginx 做分发。

    python deploy/waitress_worker.py --port 8001 --threads 8

它扮演的角色，等价于 Linux/容器里 `gunicorn -w N` 的其中**一个** worker。
换句话说：这个文件是 gunicorn 在 Windows 上的替代品，不是它的补充。

【为什么不用现成的 waitress-serve 命令行，非要自己写这 40 行】
  因为 waitress 有一个**默认开启、且不会报错**的行为会破坏审计日志：

      clear_untrusted_proxy_headers = True   （waitress 默认值）

  它会把"不信任的代理"传来的 X-Forwarded-For 等头**直接删掉**。
  而 waitress 认哪个代理可信，只能通过 Python 参数 trusted_proxy 设置 ——
  waitress-serve **没有**对应的命令行开关（实测该 CLI 无 --trusted-proxy）。

  后果：走 nginx → waitress 之后，audit_service.client_ip() 读不到
  X-Forwarded-For，只能退回 request.remote_addr = 127.0.0.1。
  审计日志里所有操作的来源 IP 全都变成 127.0.0.1 ——
  "谁在什么时候从哪改了数据"这条证据链当场失效，而且不报任何错。

  所以这里绕开 CLI，直接调 waitress.serve() 把 trusted_proxy 设上。

【为什么 trusted_proxy 设成 127.0.0.1 是安全的】
  前提是 worker 只监听回环地址（--host 默认就是 127.0.0.1，别改）。
  这样只有本机的 nginx 能连到 worker，外部客户端连不上，
  也就无法自己伪造一个 X-Forwarded-For 来冒充别的 IP。
  一旦把 worker 直接暴露到 0.0.0.0，这个信任假设就破了 ——
  任何人都能自称任意 IP，审计日志比不记还危险（记的是假证据）。
"""

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from env_check import check  # noqa: E402

check()

from waitress import serve  # noqa: E402

from wsgi import app  # noqa: E402  （导入即完成配置加载与安全检查）


def main():
    ap = argparse.ArgumentParser(description='waitress 工作进程')
    ap.add_argument('--host', default='127.0.0.1',
                    help='监听地址。默认只监听回环 —— 见文件头部关于信任边界的说明。')
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--threads', type=int, default=8,
                    help='工作线程数。默认 8 来自实测：单进程吞吐在 8 个并发时见顶，'
                         '再往上加只会让延迟变长、吞吐不涨。')
    ap.add_argument('--ident', default='smart-agri',
                    help='Server 响应头里显示的名字')
    ap.add_argument('--channel-timeout', type=int, default=120,
                    help='空闲连接多久后回收（秒）')
    args = ap.parse_args()

    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        print(f'[警告] --host={args.host} 不是回环地址。', file=sys.stderr)
        print('        这意味着客户端可以绕过 nginx 直连本进程，', file=sys.stderr)
        print('        从而伪造 X-Forwarded-For 冒充任意 IP 写进审计日志。', file=sys.stderr)
        print('        除非你确认这是有意为之，否则请用默认值。', file=sys.stderr)

    print(f'[waitress] 监听 http://{args.host}:{args.port}  '
          f'线程 {args.threads}', flush=True)

    serve(
        app,
        host=args.host,
        port=args.port,
        threads=args.threads,
        ident=args.ident,
        channel_timeout=args.channel_timeout,
        # 关键：认 nginx 为可信代理，X-Forwarded-For 才会被保留下来。
        # 少了这一行，审计日志的 IP 全部退化成 127.0.0.1。
        trusted_proxy='127.0.0.1',
        trusted_proxy_headers={'x-forwarded-for', 'x-forwarded-proto',
                               'x-forwarded-host'},
    )


if __name__ == '__main__':
    main()
