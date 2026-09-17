#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
对外部署的进程编排 —— 拉起 N 个 waitress 工作进程 + 一个 nginx，并负责干净地收场。

    .venv\\Scripts\\python.exe deploy\\serve_cluster.py
    .venv\\Scripts\\python.exe deploy\\serve_cluster.py --workers 6 --threads 8

通常不直接调它，双击 start_prod.bat 即可。

【它到底解决了什么】
  Windows 上没有 gunicorn（依赖 os.fork），而 waitress 是**单进程纯线程**的。
  单进程意味着 Python 字节码最多用满一个核 —— 实测（scripts/loadtest.py，
  32 个并发客户端，同一台机器）：

        1 进程      176 RPS
        2 进程      278 RPS   （1.58×）
        4 进程      425 RPS   （2.42×）

  而同一轮压测里纯静态文件能跑 1126 RPS、裸 MySQL 能跑 5000+ 查询/秒，
  说明瓶颈既不在 Flask 也不在数据库。所以出路只有多进程，
  多进程又必须有东西在前面分发请求 —— 那就是 nginx。

  这个脚本把"启动 N 个进程 + nginx + 收尾"这件容易半途而废的事做完：
  少停一个进程，端口就占着，下次启动会以一堆看不懂的 'address in use' 收场。

【启动前拦两件事，都是"不拦就会在运行期炸"的类型】
  ① 数据库连接总额超限
     总连接数 = 进程数 × (pool_size + max_overflow)，是**相乘**关系。
     MySQL 默认 max_connections = 151。开到 8 个进程、每进程 10+10 就是 160 条，
     直接顶穿 —— 报错是运行期的 "Too many connections"，
     而且往往在演示到一半、并发上来之后才出现。
     这里在启动前算一遍并对着 MySQL 的实际配置校验。

  ② 端口被占
     多半是上一次没退干净的 worker。让它启动到一半才发现，
     用户看到的是 nginx 502 —— 一个完全不指向真实原因的报错。

【--tunnel：把这套集群临时挂到公网】
  加 --tunnel 会再拉起一个 cloudflared 快速隧道，指向 nginx 的端口，
  拿到一个 https://xxxx.trycloudflare.com 的地址。给 start_public.bat 用。

  【为什么隧道放在这里，而不是像以前那样写在 .bat 里】
    旧版把 cloudflared 放在 bat 末尾当前台进程 —— 那是因为应用在**另一个
    窗口**里跑，bat 自己没别的事可做。现在集群本身需要一个前台来值守
    （任何一个 worker 挂了都要立刻收摊，见下），一个窗口容不下两个前台
    进程。放进这里还有个附带好处：公网地址是我们自己从 cloudflared 的
    输出里读出来的，不用人眼去另一个窗口里翻。

  【地址是"读"出来的，不是"配"出来的】
    快速隧道不接受指定子域，地址只能从 cloudflared 启动横幅里抓。
    所以这个模式**没有稳定的入口地址**，每次重启都换一个 ——
    对外发布前请先确认新地址。要固定地址得用命名隧道（需要域名和账号）。
"""

import argparse
import os
import re
import shutil
import socket
import subprocess
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

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKER = BASE_DIR / 'deploy' / 'waitress_worker.py'
NGINX_CONF_SRC = BASE_DIR / 'deploy' / 'nginx.conf'
CLOUDFLARED = BASE_DIR / 'tools' / 'cloudflared.exe'

# 快速隧道分配到的公网地址只出现在 cloudflared 的启动横幅里。
# 它是个 .exe，没有"把地址写到文件"的选项，所以只能抓输出。
TUNNEL_URL_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')

# 给 nginx 用的仓库根路径。正常情况下就是 BASE_DIR，
# 但仓库位于非 ASCII 路径下时会被换成 ASCII junction —— 见 resolve_nginx_base()。
NGINX_BASE = BASE_DIR

# 每进程连接数上限的经验值：池子只要 ≥ 线程数就够，多出来的全是空转。
# 实测把 pool_size 从 10 加到 40，吞吐一点没变（甚至略降）——
# 瓶颈从来不是连接不够，而是 GIL。所以这里给得比开发环境还小。
OVERFLOW_PER_WORKER = 2


# =============================================================================
# 启动前检查
# =============================================================================

def check_ports(ports):
    """端口能不能绑上。返回被占用的端口列表。"""
    busy = []
    for p in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', p))
            except OSError:
                busy.append(p)
    return busy


def check_db_budget(workers, pool_size, overflow):
    """
    算总连接数，并对 MySQL 的 max_connections 校验。

    这是多进程部署最容易忽略、又最先炸的地方：
    单进程时 pool_size=10 毫无问题，乘以进程数就超了。
    """
    per_worker = pool_size + overflow
    total = workers * per_worker
    try:
        conn = pymysql.connect(connect_timeout=5,
                               **{**DB_CONFIG, 'database': DB_NAME})
    except Exception as e:
        print(f'  [警告] 连不上 MySQL，跳过连接数校验：{e}')
        print(f'         按当前参数预计占用 {total} 条连接。')
        return True
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW VARIABLES LIKE 'max_connections'")
            max_conn = int(cur.fetchone()[1])
    finally:
        conn.close()

    print(f'  连接数预算   {workers} 进程 × ({pool_size} + {overflow}) = {total} 条'
          f'   / MySQL max_connections = {max_conn}')

    if total > max_conn:
        print()
        print('=' * 64)
        print('  [拒绝启动] 数据库连接总额超限')
        print('=' * 64)
        print(f'  {workers} 个进程各开 {per_worker} 条，合计 {total} 条，')
        print(f'  超过 MySQL 的 max_connections = {max_conn}。')
        print()
        print('  这会表现为运行期的 "Too many connections" ——')
        print('  启动时一切正常，等并发上来才炸，排查成本很高。')
        print()
        print('  两个改法（选一个）：')
        print(f'    1. 减少进程数      --workers {max(1, max_conn // per_worker)}')
        print(f'    2. 缩小每进程池子  --pool-size {max(2, max_conn // workers - overflow)}')
        print('=' * 64)
        return False
    return True


def check_secret_key():
    """
    占位 SECRET_KEY 下不许起。

    【这不是新增的限制，只是把报错挪到前面】
      wsgi.py 里每个 worker 自己就会拦（那是最后一道，绕过编排脚本直接
      跑 worker 也拦得住）。但在这里先拦一次，报错才指向真实原因 ——
      否则 4 个 worker 各自打印一遍拒绝信息然后退出，用户看到的是
      "worker :8001 在 30 秒内没有起来"，而真正的原因被刷到屏幕外面去了。
    """
    from config import Config
    from run import KNOWN_WEAK_SECRETS

    if Config.SECRET_KEY not in KNOWN_WEAK_SECRETS:
        return True

    print()
    print('=' * 64)
    print('  [拒绝启动] SECRET_KEY 还是占位值')
    print('=' * 64)
    print('  对外部署模式下，占位密钥是公开的：任何人都能用它伪造')
    print('  session cookie，不猜密码就直接变成管理员，')
    print('  而且日志里看不出任何异常。')
    print()
    print('  生成一个再启动：')
    print('    .venv\\Scripts\\python.exe -c "import secrets;'
          'print(secrets.token_urlsafe(48))"')
    print('  写进 .env 的 SECRET_KEY=... 即可。')
    print('=' * 64)
    return False


def resolve_nginx_base():
    """
    给 nginx 算出它要用的"仓库根路径"。

    【为什么需要这一步 —— Windows 版 nginx 不支持非 ASCII 路径】
      nginx 的 Windows 构建走 ANSI 文件 API，路径里只要有非 ASCII 字符就直接启动失败：

          nginx: [emerg] CreateFile() "...\\conf/nginx.conf" failed
                 (1113: No mapping for the Unicode character exists in the
                  target multi-byte code page)

      而本项目的目录往往是 C:\\Users\\<中文用户名>\\Desktop\\<中文目录>\\...，
      正好踩中。下面这些都不行：
        · 8.3 短名 —— 实测只给 `smart-agri-analytics` 生成了 SMART-~1，
          `朱浩恺` 和 `杂` 没有短名别名，路径照样含中文。
        · 把 nginx 挪到 ASCII 目录 —— conf 和 static 仍在非 ASCII 路径下。

      可行的是**目录联接（junction）**：在纯 ASCII 位置建一个指向仓库根的联接，
      nginx 全程只见 ASCII 路径，由内核在文件系统层解析到真实目录。
      配置里的相对路径（`root ../../app`）也照样有效——
      路径穿过 junction 时内核先解析联接，再继续处理剩余部分。
      junction 不需要管理员权限（符号链接才需要），普通用户即可创建。

    【这是 Windows 特有的绕法，Linux/容器不需要】
      容器里的路径本来就是 ASCII，也不会用这份编排脚本（那是 gunicorn 的活）。

    返回 True 表示可用（路径已经是 ASCII，或联接建好了）。
    """
    global NGINX_BASE

    if all(ord(c) < 128 for c in str(BASE_DIR)):
        return True

    public = os.environ.get('PUBLIC') or r'C:\Users\Public'
    target = Path(public) / 'smart-agri'
    if not all(ord(c) < 128 for c in str(target)):
        print(f'  [错误] 找不到可用的 ASCII 目录（PUBLIC={public}）。')
        print('         请把仓库挪到纯英文路径下，或手工建一个 junction 后重试。')
        return False

    print(f'  路径含非 ASCII 字符，nginx 无法直接使用：')
    print(f'    {BASE_DIR}')

    # 已经存在且指向正确就直接复用；指向别处（比如仓库搬过家）就重建。
    if target.exists():
        try:
            if os.path.samefile(target, BASE_DIR):
                print(f'  复用已有的 ASCII 联接：{target}')
                NGINX_BASE = target
                return True
        except OSError:
            pass
        subprocess.run(['cmd', '/c', 'rmdir', str(target)], capture_output=True)

    r = subprocess.run(['cmd', '/c', 'mklink', '/J', str(target), str(BASE_DIR)],
                       capture_output=True)
    if r.returncode != 0 or not target.exists():
        print(f'  [错误] 创建目录联接失败：{r.stderr.decode("gbk", "replace").strip()}')
        print('         手工执行（cmd 里）：')
        print(f'           mklink /J "{target}" "{BASE_DIR}"')
        return False

    print(f'  已建立 ASCII 联接：{target}')
    print(f'    → {BASE_DIR}')
    print('  （这是 Windows 特有绕法；Linux/容器里路径本身就是 ASCII，用不上。）')
    NGINX_BASE = target
    return True


def check_nginx():
    """nginx 在不在。缺了就给出可照做的下载指引，而不是让 subprocess 抛 WinError。"""
    exe = NGINX_BASE / 'tools' / 'nginx' / 'nginx.exe'
    if exe.is_file():
        return True
    print()
    print('=' * 64)
    print('  [缺少组件] 没有找到 tools/nginx/nginx.exe')
    print('=' * 64)
    print('  Windows 的 nginx 需要单独下载（不进仓库，见 .gitignore 里的 tools/）。')
    print()
    print('  1. 打开 https://nginx.org/en/download.html')
    print('  2. 下载 Stable version 的 nginx/Windows 压缩包（文件名形如')
    print('     nginx-1.28.3.zip）')
    print('  3. 解压后把里面的 nginx-<版本> 目录**改名成 nginx**，')
    print('     放到本项目的 tools\\ 下，最终路径要是：')
    print(f'       {exe}')
    print()
    print('  验证：tools\\nginx\\nginx.exe -v  应打印版本号')
    print('=' * 64)
    return False


def check_cloudflared():
    """cloudflared 在不在（--tunnel 才需要）。缺了就给可照做的下载指引。"""
    if CLOUDFLARED.is_file():
        return True
    print()
    print('=' * 64)
    print('  [缺少组件] 没有找到 tools/cloudflared.exe')
    print('=' * 64)
    print('  cloudflared 不进仓库（50MB 的二进制，且平台相关，见 .gitignore）。')
    print()
    print('  下载 cloudflared-windows-amd64.exe：')
    print('    https://github.com/cloudflare/cloudflared/releases/latest')
    print('  GitHub 慢的话，在前缀加一个镜像，例如：')
    print('    https://ghfast.top/https://github.com/cloudflare/cloudflared/'
          'releases/latest')
    print()
    print('  下完改名放到这里：')
    print(f'    {CLOUDFLARED}')
    print()
    print('  验证：tools\\cloudflared.exe --version  应打印版本号')
    print('=' * 64)
    return False


def nginx_paths():
    """返回 (nginx 目录, nginx.exe, conf/nginx.conf)，都基于 NGINX_BASE 解析。"""
    d = NGINX_BASE / 'tools' / 'nginx'
    return d, d / 'nginx.exe', d / 'conf' / 'nginx.conf'


def render_nginx_conf(workers, base_port, http_port):
    """
    把 deploy/nginx.conf 复制到 nginx 的 conf/ 下。

    【为什么要复制，不能直接用 -c 指过去】
      nginx 解析相对路径有两套基准（实测确认）：
        · include mime.types      → 相对**配置文件所在目录**
        · root / logs / 临时目录  → 相对 **prefix**（-p 指定的目录）
      mime.types 在 tools/nginx/conf/ 下。配置若留在 deploy/ 下加载，
      `include mime.types` 会去 deploy/ 里找，nginx 直接启动失败。
      复制过去之后两套基准都落在它预期的位置上，不用做任何路径替换。

      走 junction 时这个复制照做不误 —— 通过联接写进去的就是同一个文件。
    """
    _, _, conf_dst = nginx_paths()
    shutil.copyfile(NGINX_CONF_SRC, conf_dst)
    NGINX_CONF_DST = conf_dst

    # 上游端口列表是唯一需要和 worker 数量对齐的地方。
    # 这里做一次一致性检查，因为对不上时现象是 nginx 502，
    # 而 502 永远不会告诉你是"upstream 里写了一个没人监听的端口"。
    text = NGINX_CONF_DST.read_text(encoding='utf-8')
    expected = [f'127.0.0.1:{base_port + i}' for i in range(workers)]
    missing = [s for s in expected if s not in text]
    if missing:
        print()
        print('  [警告] deploy/nginx.conf 的 upstream 和本次进程数对不上')
        print(f'         期望包含：{", ".join(expected)}')
        print(f'         缺少：{", ".join(missing)}')
        print('         nginx 会把请求转给没人监听的端口，表现为 502。')
        print('         请同步修改 deploy/nginx.conf 的 upstream 段。')

    if f'listen       {http_port};' not in text and f'listen {http_port};' not in text:
        print(f'  [警告] deploy/nginx.conf 里没有找到 "listen {http_port};"，'
              f'对外端口可能不是 {http_port}。')


# =============================================================================
# 进程编排
# =============================================================================

def wait_ready(port, timeout=30):
    """等某个 worker 端口能接受连接。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(('127.0.0.1', port)) == 0:
                return True
        time.sleep(0.2)
    return False


def start_tunnel(port):
    """
    起 cloudflared 快速隧道，返回 (进程, 结果字典, 就绪事件)。

    结果字典里的 'url' 由读取线程填 —— 地址要等 cloudflared 连上 Cloudflare
    边缘之后才会打印出来，协程和线程在这里换了个名字，但都得等。

    【为什么要专门开一个线程去读】
      cloudflared 启动后会持续往 stdout 写日志。管道有缓冲上限，
      不读就会把它堵死 —— 表现是隧道明明起来了却再也不打印地址。
      边读边挑是唯一稳妥的写法。
    """
    cmd = [str(CLOUDFLARED), 'tunnel',
           '--url', f'http://127.0.0.1:{port}',
           # 快速隧道默认会自检更新，更新完**重启自己** —— 演示到一半
           # 地址突然变掉就是这么来的。这个模式下不需要它自更新。
           '--no-autoupdate']
    proc = subprocess.Popen(cmd, cwd=str(BASE_DIR),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace',
                            bufsize=1)
    info = {}
    ready = threading.Event()

    def pump():
        for line in proc.stdout:
            m = TUNNEL_URL_RE.search(line)
            if m:
                if 'url' not in info:
                    info['url'] = m.group(0)
                    ready.set()
                continue
            # cloudflared 启动时会刷十几行 INF 日志（版本、配置、连接数），
            # 原样打出来会把上面刚打印的启动摘要冲出屏幕。只留 WRN/ERR ——
            # 那些才是真要看的东西，也是最常见的失败原因（网络/代理）。
            if ' ERR ' in line or ' WRN ' in line:
                print(f'  [tunnel] {line.strip()}')
        # 进程结束也要放行，否则调用方会一直干等到超时，
        # 而拿不到地址的真实原因是 "cloudflared 已经退出了"。
        ready.set()

    threading.Thread(target=pump, daemon=True).start()
    return proc, info, ready


def main():
    ap = argparse.ArgumentParser(
        description='启动 nginx + N 个 waitress 工作进程',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--workers', type=int, default=4,
                    help='waitress 进程数（默认 4）。实测 1→4 进程吞吐 176→425 RPS，'
                         '但收益是递减的，先看 CPU 核数和数据库撑不撑得住再往上加。')
    ap.add_argument('--threads', type=int, default=8,
                    help='每个进程的工作线程数（默认 8）。实测单进程吞吐在 8 并发时见顶。')
    ap.add_argument('--base-port', type=int, default=8001,
                    help='worker 起始端口（默认 8001，只监听回环）')
    ap.add_argument('--http-port', type=int, default=8080,
                    help='nginx 对外端口（默认 8080）')
    ap.add_argument('--pool-size', type=int, default=None,
                    help='每进程连接池大小（默认 = --threads）')
    ap.add_argument('--no-nginx', action='store_true',
                    help='只起 worker，不起 nginx（调试用）')
    ap.add_argument('--tunnel', action='store_true',
                    help='再起一个 Cloudflare 快速隧道指向 nginx，临时开放到公网'
                         '（地址每次重启都变）。start_public.bat 用的就是这个。')
    args = ap.parse_args()

    pool_size = args.pool_size if args.pool_size is not None else args.threads
    worker_ports = [args.base_port + i for i in range(args.workers)]

    print('=' * 68)
    print('  智慧农业分析平台 —— 对外部署模式')
    if args.tunnel:
        print('  ⚠️ 带隧道启动：本机将开放到公网，任何人都能访问')
    print('=' * 68)

    if args.tunnel and args.no_nginx:
        print()
        print('  [错误] --tunnel 要指向 nginx，不能和 --no-nginx 一起用。')
        print('         隧道会把请求打到 nginx 的端口上，没有 nginx 就没入口。')
        return 1

    if not args.no_nginx and not resolve_nginx_base():
        return 1
    if not check_nginx():
        return 1
    if not check_secret_key():
        return 1
    if args.tunnel and not check_cloudflared():
        return 1
    if not check_db_budget(args.workers, pool_size, OVERFLOW_PER_WORKER):
        return 1

    busy = check_ports(worker_ports + ([] if args.no_nginx else [args.http_port]))
    if busy:
        print()
        print(f'  [拒绝启动] 端口被占用：{", ".join(map(str, busy))}')
        print()
        print('  多半是上一次没退干净的进程。查并停掉：')
        for p in busy:
            print(f'    netstat -ano | findstr :{p}')
            print('    taskkill /F /PID <上面查到的 PID>')
        return 1

    if not args.no_nginx:
        render_nginx_conf(args.workers, args.base_port, args.http_port)

    # ---------------------------------------------------------------- 起 worker
    # 池子大小通过环境变量传给子进程 —— config.py 读 DB_POOL_SIZE / DB_MAX_OVERFLOW。
    # 这样开发（单进程，用 config.py 的默认值）和生产（多进程，按进程数缩放）
    # 共用同一份代码，不需要维护两套配置。
    env = dict(os.environ)
    env['DB_POOL_SIZE'] = str(pool_size)
    env['DB_MAX_OVERFLOW'] = str(OVERFLOW_PER_WORKER)

    procs = []
    print()
    for p in worker_ports:
        cmd = [sys.executable, str(WORKER),
               '--port', str(p), '--threads', str(args.threads)]
        procs.append(subprocess.Popen(cmd, env=env, cwd=str(BASE_DIR)))
        print(f'  [worker] 启动 :{p}（线程 {args.threads}）')

    for p in worker_ports:
        if not wait_ready(p):
            print(f'  [错误] :{p} 在 30 秒内没有起来，已终止全部进程。')
            print('         单独跑一个看具体报错：')
            print(f'           {sys.executable} deploy\\waitress_worker.py --port {p}')
            for pr in procs:
                pr.terminate()
            return 1
    print(f'  [worker] {len(worker_ports)} 个进程全部就绪')

    # ------------------------------------------------------------------ 起 nginx
    nginx = None
    if not args.no_nginx:
        nginx_dir, nginx_exe, _ = nginx_paths()
        nginx = subprocess.Popen([str(nginx_exe), '-p', str(nginx_dir)],
                                 cwd=str(nginx_dir))
        if not wait_ready(args.http_port, timeout=15):
            print(f'  [错误] nginx 没能在 :{args.http_port} 上监听。')
            print(f'         看日志：{nginx_dir / "logs" / "error.log"}')
            nginx.terminate()
            for pr in procs:
                pr.terminate()
            return 1
        print(f'  [nginx]  监听 :{args.http_port}，静态文件直接读磁盘')

    # ------------------------------------------------------------------ 起隧道
    # 放在 nginx 之后：隧道要连的是一个已经在监听的端口，反过来会先拿到
    # 一连串连接拒绝。放在摘要之前：地址要出现在摘要里，而不是被人从
    # 一堆日志中间翻出来。
    tunnel = None
    tunnel_url = None
    if args.tunnel:
        print('  [tunnel] 正在建立 Cloudflare 快速隧道...')
        tunnel, info, ready = start_tunnel(args.http_port)
        if not ready.wait(timeout=45) or 'url' not in info:
            print()
            if tunnel.poll() is not None:
                print(f'  [错误] cloudflared 已退出（退出码 {tunnel.returncode}），'
                      '没拿到公网地址。')
                print('         上面的 [tunnel] 行是它留下的原因。')
            else:
                print('  [错误] 45 秒内没拿到公网地址，cloudflared 还在跑。')
                print('         多半是连不上 Cloudflare（网络/代理/防火墙）。')
            for pr in [tunnel, nginx] + procs:
                if pr is not None and pr.poll() is None:
                    pr.terminate()
            return 1
        tunnel_url = info['url']

    # -------------------------------------------------------------------- 摘要
    print()
    print('=' * 68)
    if tunnel_url:
        print(f'  公网地址   {tunnel_url}')
        print(f'  本机地址   http://127.0.0.1:{args.http_port}')
    else:
        print(f'  对外地址   http://127.0.0.1:{args.http_port}')
    print(f'  架构       nginx(:{args.http_port}) → {args.workers} × waitress'
          f'(:{args.base_port}+) × {args.threads} 线程'
          + ('，前面套 Cloudflare 隧道' if tunnel_url else ''))
    print(f'  静态文件   nginx 直接返回，不经过 Python')
    print(f'  连接预算   {args.workers} × ({pool_size} + {OVERFLOW_PER_WORKER})'
          f' = {args.workers * (pool_size + OVERFLOW_PER_WORKER)} 条')
    print()
    if tunnel_url:
        print('  ⚠️ 上面那个公网地址**每次重启都会变** —— 先把隧道起好，')
        print('     确认是这次的新地址，再发出去。')
        print('  ⚠️ 公网上任何人都能访问，包括公开注册入口（ALLOW_REGISTRATION）。')
        print('     演示完请立刻 Ctrl+C —— 隧道会跟着一起断开。')
    print('  ⚠️ worker 只监听回环地址，绕过 nginx 直连会丢失 X-Forwarded-For，')
    print('     审计日志的 IP 会退化成 127.0.0.1。别直接压 worker 端口。')
    print('  Ctrl+C 停止全部进程' + ('（含隧道）。' if tunnel_url else '。'))
    print('=' * 68)

    # -------------------------------------------------------------------- 值守
    # 盯住所有子进程：任何一个意外退出都要立刻收摊。
    # 不盯的话，worker 挂掉一个之后 nginx 会继续把 1/N 的请求转给死端口，
    # 用户间歇性地看到 502 —— 这种"时好时坏"最难排查。
    ret = 0
    try:
        while True:
            time.sleep(1)
            for p, pr in zip(worker_ports, procs):
                if pr.poll() is not None:
                    raise RuntimeError(f'worker :{p} 意外退出（退出码 {pr.returncode}）')
            if nginx is not None and nginx.poll() is not None:
                raise RuntimeError(f'nginx 意外退出（退出码 {nginx.returncode}）')
            # 隧道挂了也不留着一半在跑：公网入口已经断了，"还在跑"没有任何
            # 意义，只会留下下次启动要收拾的进程。地址也会过期。
            if tunnel is not None and tunnel.poll() is not None:
                raise RuntimeError(f'隧道意外退出（退出码 {tunnel.returncode}）'
                                   ' —— 公网地址已失效')
    except KeyboardInterrupt:
        print('\n  收到停止信号，正在收尾...')
    except RuntimeError as e:
        print(f'\n  [错误] {e}')
        ret = 1
    finally:
        # 由外向内收：隧道 → nginx → worker，和启动顺序正好相反。
        # 先断隧道，公网上的人立刻连不进来；否则接下来的几秒里，
        # 外面的人会撞上正在关停的 nginx，拿到一堆 502。
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tunnel.kill()
            print('  [tunnel] 已停止')
        # 再停 nginx：它还在把请求往后端转，先断掉入口才不会让用户拿到 502。
        if nginx is not None and nginx.poll() is None:
            nginx_dir, nginx_exe, _ = nginx_paths()
            subprocess.run([str(nginx_exe), '-p', str(nginx_dir), '-s', 'quit'],
                           cwd=str(nginx_dir), capture_output=True)
            try:
                nginx.wait(timeout=10)
            except subprocess.TimeoutExpired:
                nginx.terminate()
            print('  [nginx]  已停止')
        for p, pr in zip(worker_ports, procs):
            if pr.poll() is None:
                pr.terminate()
        for pr in procs:
            try:
                pr.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pr.kill()
        print(f'  [worker] {len(procs)} 个进程已停止')

    return ret


if __name__ == '__main__':
    sys.exit(main())
