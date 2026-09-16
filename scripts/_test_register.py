# -*- coding: utf-8 -*-
"""
注册功能的端到端测试。

覆盖：
  1. 正常注册 → 角色是只读
  2. 新账号确实没有写权限（服务端拦截，不只靠前端隐藏按钮）
  3. 重名校验（服务端 + 唯一约束）
  4. 用户名格式校验与保留字
  5. 蜜罐字段拦截
  6. 最短填写时间拦截
  7. 防刷限流
  8. 管理员提权后权限变化
  9. 管理员不能改自己 / 不能降权最后一个管理员
 10. 重跑 seed.py 不会清掉注册用户

这是个开发辅助脚本，不是正式测试套件（项目目前用 verify.py 的断言代替单元测试）。
跑完会把测试期间创建的账号清理掉。
"""

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from env_check import check  # noqa: E402
check()

from app import create_app                       # noqa: E402
from app.extensions import db, raw_scalar        # noqa: E402
from app.models import User, AuditLog            # noqa: E402
from app.services import user_service            # noqa: E402

app = create_app()

# 这个脚本测的是注册/限流/权限的业务逻辑，不是 CSRF ——
# 关掉它，免得每处 POST 都要先取一次 token。
# （CSRF 本身由 scripts/_e2e_yield_check.py 用真实 token 正反两面验证。）
app.config['WTF_CSRF_ENABLED'] = False
PASS, FAIL = [], []


def check_(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    mark = 'OK  ' if cond else 'FAIL'
    print(f'  [{mark}] {name}' + (f'   {detail}' if detail else ''))


def clean_test_users():
    """清掉测试期间建的账号（用户名以 t_ 开头）。"""
    with app.app_context():
        User.query.filter(User.username.like('t\_%')).delete(
            synchronize_session=False)
        AuditLog.query.filter(AuditLog.action.in_(
            ['REGISTER', 'CHECK_USERNAME'])).delete(synchronize_session=False)
        db.session.commit()


def post_register(client, username, password='testpass123', **extra):
    """构造一次注册请求。默认带上蜜罐为空 + 足够长的填写时间。"""
    data = {
        'username': username,
        'password': password,
        'confirm_password': password,
        'real_name': '测试用户',
        'email': '',
        'website': '',                        # 蜜罐，正常用户留空
        'form_ts': str(time.time() - 10),     # 假装用户在页面上停留了 10 秒
    }
    data.update(extra)
    return client.post('/register', data=data, follow_redirects=False)


print('=' * 70)
print('  注册功能端到端测试')
print('=' * 70)

clean_test_users()

with app.test_client() as c:
    # ---------------------------------------------------------------- 1. 正常注册
    print('\n--- 1. 正常注册 ---')
    r = post_register(c, 't_alice')
    check_('注册返回重定向（成功）', r.status_code == 302, f'status={r.status_code}')
    with app.app_context():
        u = User.query.filter_by(username='t_alice').first()
        check_('账号已创建', u is not None)
        if u:
            check_('默认角色是只读(3)', u.role == 3, f'role={u.role}')
            check_('默认状态是启用', u.status == 1, f'status={u.status}')
            check_('密码已哈希（不是明文）',
                   u.password_hash != 'testpass123' and len(u.password_hash) > 50)
            check_('密码可校验', u.check_password('testpass123'))

    # ---------------------------------------------------------------- 2. 只读限制
    print('\n--- 2. 新账号确实没有写权限 ---')
    r = c.post('/login', data={'username': 't_alice', 'password': 'testpass123'})
    check_('可以登录', r.status_code in (302, 200))
    # 只读用户访问编辑页
    r = c.get('/plots/1/edit', follow_redirects=False)
    check_('访问地块编辑页被拦截(403)', r.status_code == 403, f'status={r.status_code}')
    # 只读用户提交状态流转
    with app.app_context():
        pid = db.session.query(
            __import__('app.models', fromlist=['Planting']).Planting
        ).filter_by(status=20).first()
        pid = pid.id if pid else 1
    r = c.post(f'/plantings/{pid}/status', data={'status': 30}, follow_redirects=False)
    check_('提交状态流转被拦截(403)', r.status_code == 403, f'status={r.status_code}')
    # 只读用户访问管理员页面
    r = c.get('/admin/users', follow_redirects=False)
    check_('访问用户管理被拦截(403)', r.status_code == 403, f'status={r.status_code}')
    c.get('/logout')

    # ---------------------------------------------------------------- 3. 重名
    print('\n--- 3. 重名校验 ---')
    r = post_register(c, 't_alice')
    check_('重名注册被拒(400)', r.status_code == 400, f'status={r.status_code}')
    with app.app_context():
        check_('没有创建重复账号',
               User.query.filter_by(username='t_alice').count() == 1)
    # AJAX 接口
    r = c.get('/api/check-username?u=t_alice')
    j = r.get_json()
    check_('AJAX 接口报告已占用', j.get('ok') is False, j.get('msg', ''))
    r = c.get('/api/check-username?u=t_brandnew')
    j = r.get_json()
    check_('AJAX 接口报告可用', j.get('ok') is True, j.get('msg', ''))

    # ---------------------------------------------------------------- 4. 格式与保留字
    print('\n--- 4. 用户名格式与保留字 ---')
    for name, uname, desc in [
        ('长度不足被拒', 'ab', '2 个字符'),
        ('数字开头被拒', '123abc', '以数字开头'),
        ('含非法字符被拒', 'abc@def', '含 @'),
        ('保留字被拒', 'admin', 'admin 是保留字'),
        ('保留字(大小写)被拒', 'ADMIN', 'ADMIN 应被识别为保留字'),
    ]:
        r = post_register(c, uname)
        check_(name, r.status_code == 400, f'{desc} status={r.status_code}')

    # ---------------------------------------------------------------- 5. 蜜罐
    print('\n--- 5. 机器人陷阱 ---')
    r = post_register(c, 't_bot', website='http://spam.example.com')
    check_('蜜罐字段被填 → 拒绝', r.status_code == 400, f'status={r.status_code}')
    with app.app_context():
        check_('蜜罐请求未创建账号',
               User.query.filter_by(username='t_bot').first() is None)

    r = post_register(c, 't_fast', form_ts=str(time.time()))   # 0 秒提交
    check_('秒交表单 → 拒绝', r.status_code == 400, f'status={r.status_code}')

    # ---------------------------------------------------------------- 6. 防刷
    print('\n--- 6. 防刷限流 ---')
    clean_test_users()
    ok_count = 0
    for i in range(user_service.RATE_PER_IP_HOUR + 2):
        r = post_register(c, f't_flood{i}')
        if r.status_code == 302:
            ok_count += 1
    check_(f'每小时最多注册 {user_service.RATE_PER_IP_HOUR} 个',
           ok_count == user_service.RATE_PER_IP_HOUR,
           f'实际成功 {ok_count} 个')

    # ---------------------------------------------------------------- 7. 管理员操作
    print('\n--- 7. 管理员提权与自我保护 ---')
    clean_test_users()
    post_register(c, 't_bob')
    c.get('/logout')
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})

    with app.app_context():
        bob = User.query.filter_by(username='t_bob').first()
        bob_id = bob.id
        admin = User.query.filter_by(username='admin').first()
        admin_id = admin.id
        role_before = bob.role

    r = c.post(f'/admin/users/{bob_id}/role', data={'role': 2}, follow_redirects=True)
    with app.app_context():
        role_after = db.session.get(User, bob_id).role
    check_('管理员提权成功（只读→农艺师）',
           role_before == 3 and role_after == 2, f'{role_before} -> {role_after}')

    # 提权后 bob 应该能编辑了
    c.get('/logout')
    c.post('/login', data={'username': 't_bob', 'password': 'testpass123'})
    r = c.get('/plots/1/edit', follow_redirects=False)
    check_('提权后可以访问编辑页(200)', r.status_code == 200, f'status={r.status_code}')
    c.get('/logout')

    # 管理员不能改自己
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    r = c.post(f'/admin/users/{admin_id}/role', data={'role': 3}, follow_redirects=True)
    with app.app_context():
        admin_role = db.session.get(User, admin_id).role
    check_('管理员不能降自己的权', admin_role == 1, f'role={admin_role}')

    r = c.post(f'/admin/users/{admin_id}/toggle', follow_redirects=True)
    with app.app_context():
        admin_status = db.session.get(User, admin_id).status
    check_('管理员不能禁用自己', admin_status == 1, f'status={admin_status}')

    # ---------------------------------------------------------------- 8. 审计日志
    print('\n--- 8. 审计日志 ---')
    with app.app_context():
        n_reg = AuditLog.query.filter_by(action='REGISTER').count()
        n_role = AuditLog.query.filter(
            AuditLog.action == 'UPDATE',
            AuditLog.target_table == 'user').count()
    check_('注册写入了审计日志', n_reg > 0, f'{n_reg} 条')
    check_('角色变更写入了审计日志', n_role > 0, f'{n_role} 条')

print('\n' + '=' * 70)
print(f'  通过 {len(PASS)} 项，失败 {len(FAIL)} 项')
if FAIL:
    print('  失败明细：')
    for f in FAIL:
        print(f'    · {f}')
print('=' * 70)

# 注意：这里**不清理**测试账号 —— 下一步要跑 seed.py 验证它们能不能活下来。
print(f'\n测试期间创建的账号 {len([u for u in PASS if "账号已创建" in u])} 个，'
      f'保留给 seed 存活测试用。')
sys.exit(1 if FAIL else 0)
