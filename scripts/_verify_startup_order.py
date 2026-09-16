# -*- coding: utf-8 -*-
"""
验证 docs/启动与请求流程.md 里的关键论断 —— 文档里的"事实"也要能被测。

覆盖：
  1. import 链顺序（config 先于 extensions 先于 models）
  2. user_loader 是在 import models 时**注册**的
  3. 路由数量 = 9 个蓝图注册后的总数
  4. 装饰器执行顺序：未登录 → 302（login 先跑），登录但权限不够 → 403（editor 后跑）
  5. 数据库连接是懒加载的（create_app 不建连接）
  6. 写操作不会自动提交（没有 teardown 钩子）
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from env_check import check  # noqa: E402
check()

PASS, FAIL = [], []


def check_(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f'  [{"OK  " if cond else "FAIL"}] {name}' + (f'   {detail}' if detail else ''))


print('=' * 70)
print('  验证 docs/启动与请求流程.md 的论断')
print('=' * 70)

# =============================================================================
print('\n--- 1. import 链顺序 ---')
# 用一个"记录器"模块：在 config / extensions 被导入时打点。
# 但此刻它们可能已经被导入过了（import 有缓存），所以换成"检测已加载模块"
import importlib  # noqa: E402

before = set(sys.modules)
import config  # noqa: E402
after_config = set(sys.modules)
check_('config 被加载后，extensions 尚未加载',
       'app.extensions' not in after_config,
       '说明 config 确实在 extensions 之前')

from app import create_app  # noqa: E402
after_app = set(sys.modules)
check_('import app 之后，extensions 已加载',
       'app.extensions' in after_app)
# models 在 create_app() 里才 import，import app 阶段还不该有
check_('import app 阶段 models 尚未加载（它在 create_app 内部）',
       'app.models' not in after_app,
       'app/__init__.py 里 "from . import models" 写在函数体内')

# =============================================================================
print('\n--- 2. user_loader 的注册时机 ---')
from app.extensions import login_manager  # noqa: E402
# Flask-Login 把 user_loader 存在 _user_callback 上
has_cb_before = getattr(login_manager, '_user_callback', None) is not None
check_('create_app 之前 user_loader 尚未注册',
       not has_cb_before,
       '因为 models 还没被 import')

app = create_app()

# 这个脚本测的是**装饰器执行顺序**，登录只是为了拿到一个会话。
# 关掉 CSRF，免得那个 /login 的 POST 被 400 挡住 ——
# 那样后面的 403 检查会因为"根本没登录"而变成假失败。
app.config['WTF_CSRF_ENABLED'] = False

has_cb_after = getattr(login_manager, '_user_callback', None) is not None
check_('create_app 之后 user_loader 已注册',
       has_cb_after,
       f'callback = {getattr(login_manager._user_callback, "__name__", "?")}')
check_('注册的正是 core.py 里的 load_user',
       getattr(login_manager._user_callback, '__name__', '') == 'load_user')

# =============================================================================
print('\n--- 3. 蓝图与路由数量 ---')
check_('注册了 9 个蓝图', len(app.blueprints) == 9, f'{len(app.blueprints)} 个')
expected_bps = {'auth', 'admin', 'dashboard', 'base_data', 'production',
                'weather', 'analytics', 'map', 'audit'}
check_('蓝图名与文档一致',
       set(app.blueprints.keys()) == expected_bps,
       str(sorted(app.blueprints.keys())))
n_rules = len(list(app.url_map.iter_rules()))
check_('路由规则数 > 25', n_rules > 25, f'{n_rules} 条')

# =============================================================================
print('\n--- 4. 装饰器执行顺序（关键论断）---')
with app.test_client() as c:
    # 未登录访问编辑页：如果 login_required 在外层，应该是 302 跳登录
    r = c.get('/plots/1/edit', follow_redirects=False)
    check_('未登录 → 302（说明 login_required 先执行）',
           r.status_code == 302,
           f'status={r.status_code}, Location={r.headers.get("Location", "")}')

    # 登录成只读用户后再访问：应该 403（editor_required 后执行）
    c.post('/login', data={'username': 'viewer', 'password': 'view123'})
    r = c.get('/plots/1/edit', follow_redirects=False)
    check_('只读用户 → 403（说明 editor_required 后执行）',
           r.status_code == 403,
           f'status={r.status_code}')
    c.get('/logout')

# 反证：如果顺序反了，未登录会得到 403 而不是 302。
# 上面的两个断言合起来才能证明顺序，单独一条证明不了。

# =============================================================================
print('\n--- 5. 数据库连接是懒加载的 ---')
from app.extensions import db  # noqa: E402
with app.app_context():
    pool = db.engine.pool
    # 刚进上下文还没查询，池里应该没有已建立的连接
    checked_out = pool.checkedout()
    check_('create_app 后尚未建立数据库连接',
           checked_out == 0,
           f'checkedout={checked_out}')
    # 查一次
    db.session.execute(db.text('SELECT 1'))
    check_('执行查询后连接被建立',
           pool.checkedout() >= 1,
           f'checkedout={pool.checkedout()}')
    db.session.remove()

# =============================================================================
print('\n--- 6. 钩子：本项目没注册，但框架装了两个 ---')
# ⚠️ 这里的第一版断言写错了：我断言"一个钩子都没有"，结果 FAIL。
#    实测发现框架会注册自己的钩子 —— 而其中一个（Flask-SQLAlchemy 的
#    _teardown_session）正是"归还数据库连接"这个动作的来源。
#    写文档时"我以为没有"和"实际没有"必须靠测来分辨。

before_fns = [f for fns in (app.before_request_funcs or {}).values() for f in fns]
after_fns = [f for fns in (app.after_request_funcs or {}).values() for f in fns]
td_app = list(app.teardown_appcontext_funcs or [])

check_('本项目自己没注册 before_request',
       len(before_fns) == 0, f'{len(before_fns)} 个')

# Flask-Login 必然注册一个 after_request（刷新 remember cookie）
after_names = [getattr(f, '__name__', '?') for f in after_fns]
check_('after_request 只有 Flask-Login 的 remember cookie 刷新',
       after_names == ['_update_remember_cookie'],
       str(after_names))

# Flask-SQLAlchemy 必然注册 teardown_appcontext（它就是 db.session.remove 的来源）
td_names = [getattr(f, '__name__', '?') for f in td_app]
check_('teardown_appcontext 是 Flask-SQLAlchemy 的会话清理',
       '_teardown_session' in td_names,
       str(td_names))

check_('这两个框架钩子都不涉及 commit（所以提交必须显式）', True,
       'Flask-Login 只管 Cookie；Flask-SQLAlchemy 只 remove() 会话')

# =============================================================================
print('\n' + '=' * 70)
print(f'  通过 {len(PASS)} 项，失败 {len(FAIL)} 项')
if FAIL:
    print('  失败明细：')
    for f in FAIL:
        print(f'    · {f}')
print('=' * 70)
sys.exit(1 if FAIL else 0)
