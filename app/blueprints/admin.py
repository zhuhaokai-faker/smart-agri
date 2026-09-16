# -*- coding: utf-8 -*-
"""
管理员蓝图：用户管理（提权 / 禁用 / 查看）。

【为什么单独一个蓝图，而不是塞进 auth.py】
  职责不同：auth.py 管的是"我是谁"（认证），这里管的是"别人能干什么"（授权）。
  混在一起会让 auth.py 同时承担登录和权限分配两件事，
  而且权限分配属于高危操作，单独一个文件便于审查和加限制。
"""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from ..decorators import admin_required
from ..models import AuditLog, User
from ..services import audit_service, user_service

bp = Blueprint('admin', __name__, url_prefix='/admin')

PER_PAGE = 20


@bp.route('/users')
@login_required
@admin_required
def user_list():
    """
    用户列表。

    ⚠️ 只允许管理员访问 —— 普通用户能看到全站有哪些账号，
       等于白送一份用户名列表（配合密码猜测就是攻击面的第一步）。
    """
    page = request.args.get('page', 1, type=int)
    role = request.args.get('role', type=int)
    keyword = (request.args.get('q') or '').strip()

    q = User.query
    if role:
        q = q.filter(User.role == role)
    if keyword:
        q = q.filter(User.username.like(f'%{keyword}%'))

    pagination = q.order_by(User.role, User.id).paginate(
        page=page, per_page=PER_PAGE, error_out=False)

    # 统计：让管理员一眼看到有多少待提权的只读账号
    stats = {
        'total': User.query.count(),
        'viewer': User.query.filter_by(role=3).count(),
        'agronomist': User.query.filter_by(role=2).count(),
        'admin': User.query.filter_by(role=1).count(),
        'disabled': User.query.filter_by(status=0).count(),
    }
    return render_template('user_list.html', pagination=pagination,
                           role=role, keyword=keyword, stats=stats)


@bp.route('/users/<int:uid>/role', methods=['POST'])
@login_required
@admin_required
def change_role(uid):
    """调整用户角色（提权 / 降权）。"""
    user = User.query.get_or_404(uid)
    new_role = request.form.get('role', type=int)
    if not new_role:
        flash('未指定角色', 'warning')
        return redirect(url_for('admin.user_list'))

    try:
        old, new = user_service.change_role(user, new_role, current_user)
    except user_service.RegisterError as e:
        flash(str(e), 'danger')
        return redirect(url_for('admin.user_list'))

    from config import ROLE_NAMES
    flash(f'{user.username} 的角色已从「{ROLE_NAMES.get(old)}」'
          f'改为「{ROLE_NAMES.get(new)}」', 'success')
    return redirect(url_for('admin.user_list'))


@bp.route('/users/<int:uid>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_status(uid):
    """启用 / 禁用账号。"""
    user = User.query.get_or_404(uid)
    try:
        old, new = user_service.toggle_status(user, current_user)
    except user_service.RegisterError as e:
        flash(str(e), 'danger')
        return redirect(url_for('admin.user_list'))

    flash(f'{user.username} 已{"启用" if new == 1 else "禁用"}', 'success')
    return redirect(url_for('admin.user_list'))


@bp.route('/users/<int:uid>')
@login_required
@admin_required
def user_detail(uid):
    """单个用户的详情 + 它的操作历史。"""
    user = User.query.get_or_404(uid)
    logs = (AuditLog.query
            .options(joinedload(AuditLog.user))
            .filter(AuditLog.user_id == uid)
            .order_by(AuditLog.created_at.desc())
            .limit(50).all())
    return render_template('user_detail.html', u=user, logs=logs)
