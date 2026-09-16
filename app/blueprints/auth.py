# -*- coding: utf-8 -*-
"""认证蓝图：登录、登出、修改密码。"""

import time
from datetime import datetime

from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db, raw_scalar
from ..models import User
from ..services import audit_service, user_service

bp = Blueprint('auth', __name__)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        user = User.query.filter_by(username=username).first()

        # ⚠️ 安全要点：无论是"用户不存在"还是"密码错误"，都返回**同一句提示**。
        #    如果分别提示"用户不存在"和"密码错误"，攻击者就能用这个差异
        #    枚举出系统里有哪些用户名（用户名枚举漏洞）。
        if user is None or not user.check_password(password):
            audit_service.log_login(username, success=False,
                                    user_id=user.id if user else None)
            db.session.commit()
            flash('用户名或密码错误', 'danger')
            return render_template('login.html', username=username), 401

        if not user.is_active:
            flash('该账号已被禁用，请联系管理员', 'warning')
            return render_template('login.html', username=username), 403

        login_user(user, remember=bool(request.form.get('remember')))
        user.last_login_at = datetime.now()
        audit_service.log_login(username, success=True, user_id=user.id)
        db.session.commit()

        # 只允许跳回站内地址，防止开放重定向漏洞
        # （攻击者可以构造 ?next=https://evil.com 把用户骗走）
        nxt = request.args.get('next') or ''
        if nxt.startswith('/') and not nxt.startswith('//'):
            return redirect(nxt)
        return redirect(url_for('dashboard.index'))

    return render_template('login.html', username='')


@bp.route('/logout')
@login_required
def logout():
    audit_service.log('LOGOUT', 'user', current_user.id)
    db.session.commit()
    logout_user()
    flash('已退出登录', 'info')
    return redirect(url_for('auth.login'))


@bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        old = request.form.get('old_password') or ''
        new = request.form.get('new_password') or ''
        confirm = request.form.get('confirm_password') or ''

        if not current_user.check_password(old):
            flash('原密码不正确', 'danger')
        elif len(new) < 6:
            flash('新密码至少 6 位', 'warning')
        elif new != confirm:
            flash('两次输入的新密码不一致', 'warning')
        else:
            current_user.set_password(new)
            audit_service.log('UPDATE', 'user', current_user.id)
            db.session.commit()
            flash('密码已修改', 'success')
            return redirect(url_for('dashboard.index'))

    return render_template('change_password.html')


# =============================================================================
# 注册
# =============================================================================

@bp.route('/register', methods=['GET', 'POST'])
def register():
    """
    公开注册。

    【核心设计：注册即只读】
    新账号的角色固定为 3（只读）。想要写权限必须由管理员在用户管理页手动提权。
    这样即使注册接口被刷，攻击者拿到的也只是一堆只能看的账号 ——
    把"注册"这个不可控入口的危害面压到最小。

    【三层防护】
      1. 前端 AJAX 即时校验用户名（体验）
      2. 服务端格式校验 + 重名检查（业务）
      3. 数据库 uk_username 唯一约束（防并发竞态）—— 见 user_service.register
    外加机器人陷阱（蜜罐字段 + 最短填写时间）和 IP 限流。
    """
    if not current_app.config.get('ALLOW_REGISTRATION', True):
        flash('本站已关闭公开注册，请联系管理员开通账号', 'warning')
        return redirect(url_for('auth.login'))

    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    # 表单渲染时把时间戳写进隐藏字段，提交时算差值判断是否"秒交"
    now_ts = time.time()
    form_data = {'username': '', 'real_name': '', 'email': ''}

    if request.method == 'POST':
        form_data = {
            'username': (request.form.get('username') or '').strip(),
            'real_name': (request.form.get('real_name') or '').strip(),
            'email': (request.form.get('email') or '').strip(),
        }
        try:
            user = user_service.register(
                username=form_data['username'],
                password=request.form.get('password') or '',
                confirm=request.form.get('confirm_password') or '',
                real_name=form_data['real_name'],
                email=form_data['email'],
                form=request.form,
                now_ts=now_ts,
                ip=audit_service.client_ip(),
            )
        except user_service.RegisterError as e:
            flash(str(e), 'danger')
            return render_template('register.html', form=form_data,
                                   now_ts=now_ts), 400

        flash(f'注册成功！账号 "{user.username}" 已创建，'
              f'当前为【只读】权限。如需编辑数据，请联系管理员提权。', 'success')
        return redirect(url_for('auth.login'))

    return render_template('register.html', form=form_data, now_ts=now_ts)


@bp.route('/api/check-username')
def check_username():
    """
    用户名可用性校验（注册页的 AJAX 接口）。

    ⚠️ 【一个必须说清楚的取舍】
       这个接口天然就是一个**用户名枚举**的入口 —— 攻击者可以逐个查询
       哪些用户名已被注册。而登录接口我是刻意做成"用户名和密码错误返回
       同一句提示"来防止枚举的。

       那为什么注册这里又允许暴露？
       因为**注册本身就无法避免这个信息泄露** —— 用户必须知道名字是否可用
       才能完成注册。除非改成"不管是否重名都返回成功，实际失败才发邮件通知"
       那种重流程，否则这个信息必然可见。

       这是业内普遍接受的取舍，但它确实降低了攻击者猜密码的成本
       （至少知道了哪些用户名是真实存在的）。所以我给这个接口也加了限流。

       如果哪天要收紧：把实时校验去掉，只在提交后提示"用户名已被占用"。
       体验差一点，但枚举成本高很多。
    """
    username = (request.args.get('u') or '').strip()
    if not username:
        return jsonify({'ok': False, 'msg': '请输入用户名'})

    # 接口级限流：同一 IP 每分钟最多查 30 次。
    # 不限制的话，这个接口就成了批量枚举用户名的工具。
    ip = audit_service.client_ip()
    if ip:
        recent = raw_scalar("""
            SELECT COUNT(*) FROM audit_log
            WHERE action = 'CHECK_USERNAME' AND ip = :ip
              AND created_at > NOW() - INTERVAL 1 MINUTE
        """, {'ip': ip}) or 0
        if recent >= 30:
            return jsonify({'ok': False, 'msg': '查询过于频繁，请稍后再试'}), 429
        audit_service.log('CHECK_USERNAME', 'user', None)
        db.session.commit()

    # 格式问题直接返回，不查库
    if len(username) < 3 or len(username) > 30:
        return jsonify({'ok': False, 'msg': '长度需在 3~30 个字符之间'})
    if not user_service.USERNAME_RE.match(username):
        return jsonify({'ok': False, 'msg': '只能包含字母、数字、下划线、连字符，且以字母开头'})
    if username.lower() in user_service.RESERVED_USERNAMES:
        return jsonify({'ok': False, 'msg': '这是系统保留用户名'})

    if user_service.is_username_taken(username):
        return jsonify({'ok': False, 'msg': '该用户名已被占用'})
    return jsonify({'ok': True, 'msg': '该用户名可以使用'})

