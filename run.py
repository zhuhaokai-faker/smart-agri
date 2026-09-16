#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
应用启动入口。

    python run.py              # 开发模式（默认 5000 端口，热重载）
    python run.py --port 8080  # 指定端口
    python run.py --prod       # 关闭调试（不要用 Flask 内置服务器上生产）

⚠️ 请用**项目自带的虚拟环境**启动：
       Windows:  .venv\\Scripts\\python.exe run.py
       macOS/Linux:  .venv/bin/python run.py
   或直接双击 start.bat（它已经写死了正确的解释器路径）。

   直接敲 `python run.py` 有风险 —— 如果 PATH 上的 python 是别的环境
   （比如另一个项目的 venv 或系统 Python），就会报
   `ModuleNotFoundError: No module named 'flask_login'`。
   env_check.check() 会把这种情况变成一条可操作的提示。
"""

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from env_check import check  # noqa: E402  （必须在 sys.path 处理之后导入）


# 已知的开发用占位密钥。列在这里是为了**能识别出它们**，不是为了使用它们。
KNOWN_WEAK_SECRETS = {
    'dev-secret-key-change-in-production',   # config.py 的兜底值
    'replace-with-random-string',            # .env.example 的模板值
    'smart-agri-dev-secret-please-change',   # 早期 .env 里用过的值
}

_LOOPBACK = {'127.0.0.1', 'localhost', '::1'}


def preflight(args, app):
    """
    启动前的安全检查 —— 只拦"一旦暴露就不可挽回"的两种情况。

    ① **debug 开着却绑到非回环地址**
       Flask 调试器允许访问者在浏览器里执行任意 Python 代码。绑到 0.0.0.0
       相当于把一个交互式控制台交给整个局域网（或者隧道）。
       Werkzeug 那个 PIN 是防手滑的，不是安全边界。
       这里**直接拒绝启动**而不是打印警告 —— 警告没人看。

    ② **--prod 模式下 SECRET_KEY 还是占位值**
       占位密钥是公开的，任何人都能用它**伪造 session cookie**，
       不猜密码就直接变成管理员，而且日志里看不出任何异常 ——
       比口令泄露更隐蔽。--prod 的语义不是"关掉 debug"，
       而是"这个实例会被别人访问到"，所以在这个模式下必须拦住。

    **有意不拦的**：演示账号口令、ALLOW_REGISTRATION 公开注册开关。
    那是演示体验与风险之间的取舍，属于使用者的决定 ——
    启动脚本没资格替使用者否决它，只把风险写在这里备查。
    """
    if not args.prod and args.host not in _LOOPBACK:
        print('=' * 64)
        print('  [拒绝启动] debug 模式不能绑定到非本机地址')
        print('=' * 64)
        print(f'  当前绑定 {args.host}，而 debug 是开启的。')
        print('  Flask 调试器会允许访问者在浏览器里执行任意 Python 代码。')
        print()
        print('  只是本机自用：  python run.py')
        print('  要给别人访问：  python run.py --prod')
        print('=' * 64)
        sys.exit(2)

    if args.prod and app.config.get('SECRET_KEY') in KNOWN_WEAK_SECRETS:
        print('=' * 64)
        print('  [拒绝启动] SECRET_KEY 还是占位值')
        print('=' * 64)
        print('  --prod 意味着这个实例会被别人访问到，而占位密钥是公开的：')
        print('  任何人都能用它伪造 session cookie，不猜密码就变成管理员。')
        print()
        print('  生成一个再启动：')
        print('    python -c "import secrets;print(secrets.token_urlsafe(48))"')
        print('  写进 .env 的 SECRET_KEY=... 即可。')
        print('=' * 64)
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description='启动智慧农业分析平台')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=5000)
    ap.add_argument('--prod', action='store_true',
                    help='关闭 debug，并按"会被别人访问到"做安全检查')
    args = ap.parse_args()

    # 环境不对就别往下走了 —— 否则会抛出一堆指向无关文件的 ModuleNotFoundError。
    # 检查逻辑抽在 env_check.py 里，四个入口（run.py + 三个脚本）共用。
    check()

    from app import create_app

    app = create_app()
    preflight(args, app)

    print('=' * 64)
    print('  智慧农业种植管理与产量分析平台')
    print('=' * 64)
    print(f'  解释器     {sys.executable}')
    print(f'  访问地址   http://{args.host}:{args.port}')
    if args.prod:
        print('  模式       对外模式（debug 已关闭）')
    else:
        # 演示账号只在开发模式下打印：对外模式下它是本地终端里的无谓留痕
        print('  模式       开发模式（debug 开启，仅本机自用）')
        print('  演示账号   admin / admin123        （管理员，可编辑）')
        print('             agronomist / agri123    （农艺师，可编辑）')
        print('             viewer / view123        （只读，写操作会被拦截）')
    print('=' * 64)

    # 注意：Flask 内置服务器仅供开发/演示。
    # 生产环境要用 gunicorn（Linux）或 waitress（Windows）这类 WSGI 服务器，
    # 它们才具备多进程/多线程、超时控制、优雅重启等能力。
    app.run(host=args.host, port=args.port, debug=not args.prod)


if __name__ == '__main__':
    main()
