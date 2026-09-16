# -*- coding: utf-8 -*-
"""
解释器环境预检 —— 把"看不懂的报错"变成"能照着做的提示"。

【为什么需要这个模块】
机器上往往同时存在多个 Python 环境：系统 Python、PyCharm 的 venv、
其他项目的 venv、conda 环境……`python` 这个词在不同终端里指向的可能完全不同。

一旦用错了环境，几个入口脚本会各自抛出**位置误导**的报错：

    $ python run.py
    File "app/extensions.py", line 15, in <module>
        from flask_login import LoginManager
    ModuleNotFoundError: No module named 'flask_login'

报错指向 app/extensions.py —— 一个和问题毫无关系的地方。
看堆栈会以为是代码写错了，实际是解释器选错了。
这类"报错位置和真实原因不在一处"的问题最消耗排查时间。

所以在本项目的**每个入口**都先跑一次 preflight()，直接说清楚：
用哪个解释器、缺什么、怎么修。花 20 行代码省掉半小时排查，很划算。

【这个模块自己不依赖任何第三方包】
否则它自己就会因为环境不对而导入失败 —— 那就失去意义了。
"""

import sys
from pathlib import Path

# 项目直接依赖的顶层模块名 -> pip 包名
# （模块名和包名经常不一致，比如 flask_login 属于 Flask-Login 包。
#   提示里要给出准确的 pip 包名，否则用户照着装也会失败。）
REQUIRED = {
    'flask': 'Flask',
    'flask_login': 'Flask-Login',
    'flask_sqlalchemy': 'Flask-SQLAlchemy',
    'pymysql': 'PyMySQL',
    'sqlalchemy': 'SQLAlchemy',
    'cryptography': 'cryptography',
}


def project_root() -> Path:
    """本项目根目录（即本文件所在目录）。"""
    return Path(__file__).resolve().parent


def check(require_mysql_deps: bool = True, exit_on_fail: bool = True) -> int:
    """
    检查当前解释器是否适合运行本项目。

    返回 0 表示正常；非 0 表示有问题（默认直接 sys.exit）。

    require_mysql_deps=False 用于"不连数据库也能跑"的入口
    （目前没有这样的入口，但保留这个开关以备将来）。
    """
    root = project_root()
    project_venv = root / '.venv'
    problems = []

    # ---- 1. 是否运行在虚拟环境里 ----
    in_venv = sys.prefix != sys.base_prefix
    using_project_venv = False
    if in_venv:
        try:
            using_project_venv = Path(sys.prefix).resolve() == project_venv.resolve()
        except OSError:
            pass

    if not in_venv:
        problems.append(f'当前没有运行在任何虚拟环境里，用的是系统 Python。\n'
                        f'      sys.executable = {sys.executable}')
    elif not using_project_venv:
        problems.append(f'当前用的是**别的**虚拟环境，不是本项目的 .venv。\n'
                        f'      sys.executable = {sys.executable}\n'
                        f'      本项目预期 = {project_venv}')

    # ---- 2. 关键依赖能否导入 ----
    missing = []
    if require_mysql_deps:
        for mod, pkg in REQUIRED.items():
            try:
                __import__(mod)
            except ImportError:
                missing.append(pkg)
    if missing and not problems:
        # 环境对，只是依赖没装全 —— 单独报，提示也不一样
        problems.append('缺少依赖：' + '、'.join(missing))

    if not problems:
        return 0

    # ---- 打印可操作的提示 ----
    py = (project_venv / 'Scripts' / 'python.exe') if sys.platform == 'win32' \
        else (project_venv / 'bin' / 'python')
    line = '=' * 68
    print(line)
    print('  X 启动前检查未通过')
    print(line)
    for p in problems:
        print(f'  · {p}')
    print()

    # 【提示要区分"原因"和"症状"】
    # 环境和依赖是两回事：
    #   · 解释器选错了 → 依赖"缺失"只是这个错环境的症状，修法是换解释器，
    #                    这时候让人去 pip install 是开错药方（装到错环境上更乱）；
    #   · 解释器对了但依赖没装 → 才是真的该 pip install。
    # 一开始我把两者混在一起，无论哪种情况都提示"补装依赖"，是误导。
    env_wrong = not using_project_venv

    if not project_venv.exists():
        print('  本项目还没有 .venv，先创建虚拟环境并安装依赖：')
        print()
        print('      py -3.12 -m venv .venv')
        print(f'      "{py}" -m pip install -r requirements.txt')
        print()
    elif env_wrong:
        print('  【怎么修】换用项目自带的虚拟环境运行（不要直接敲 python）：')
        print()
        print(f'      "{py}" {Path(sys.argv[0]).name}')
        print()
        print('  或者直接双击 start.bat —— 它已经写死了正确的解释器路径。')
        print()
        print('  ⚠️ 不要在这个错误的环境里 pip install —— 那是把依赖装到了别的项目上，')
        print('     本项目依然跑不起来。**换解释器**才是修法。')
        print()
    elif missing:
        print('  解释器是对的，只是依赖没装全。补装：')
        print()
        print(f'      "{py}" -m pip install -r requirements.txt')
        print()

    print('  【VS Code 用户】把解释器切到项目的 .venv：')
    print('      Ctrl+Shift+P -> Python: Select Interpreter')
    print(f'      -> 选择 {py}')
    print('      （项目已提供 .vscode/settings.json，通常会自动选中）')
    print(line)

    if exit_on_fail:
        sys.exit(1)
    return 1


def require_venv(func):
    """
    装饰器版：给 main() 套上预检。

        @require_venv
        def main():
            ...
    """
    def wrapper(*args, **kwargs):
        check()
        return func(*args, **kwargs)
    wrapper.__name__ = getattr(func, '__name__', 'main')
    wrapper.__doc__ = func.__doc__
    return wrapper
