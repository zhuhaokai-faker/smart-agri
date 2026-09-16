# -*- coding: utf-8 -*-
"""权限装饰器（RBAC）。"""

from functools import wraps

from flask import abort, flash, redirect, url_for
from flask_login import current_user


def role_required(*roles):
    """
    限制只有指定角色能访问。

        @role_required(ROLE_ADMIN, ROLE_AGRONOMIST)
        def edit_plot(...): ...

    【为什么用装饰器而不是在每个视图里写 if】
      权限判断是**横切关注点**：几乎每个写操作都要检查。
      散落在几十个视图里的 if 判断，只要有一个人忘了写就是权限漏洞。
      装饰器把"必须检查权限"变成声明式的一条语句，漏写的概率大幅降低。

    【为什么用 403 而不是跳转到登录页】
      未登录（匿名用户）→ 401/跳登录页，因为"登录后可能就有权限了"。
      已登录但角色不够   → 403，因为再登录也没用。
      把这两种情况混为一谈，用户体验和安全性都会打折。
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('请先登录', 'warning')
                return redirect(url_for('auth.login'))
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


def admin_required(view):
    """
    要求管理员角色。

    【为什么单独一个装饰器，而不是写 @role_required(ROLE_ADMIN)】
    两者等价，但 admin_required 在**阅读时更清楚**：
    看到它就知道"这个入口是管理员专属"，不用去回想 ROLE_ADMIN 是几。
    权限相关的代码应该让人一眼看出意图 —— 这是被误改代价最高的部分。

    【为什么用户管理必须是管理员专属】
    能看到全站用户列表 = 拿到一份有效的用户名清单，
    配合密码猜测就是攻击的第一步。
    而且提权操作本身就是权限系统的核心，必须收紧到最小范围。
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('请先登录', 'warning')
            return redirect(url_for('auth.login'))
        if current_user.role != 1:
            flash('该功能仅管理员可用', 'danger')
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def editor_required(view):
    """
    要求"可编辑"权限（管理员或农艺师）。只读角色不能写。

    单独抽出来是因为这是最常见的权限粒度 ——
    "谁能改数据"比"谁能访问某个具体角色"用得多。
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('请先登录', 'warning')
            return redirect(url_for('auth.login'))
        if not current_user.can_edit:
            flash('当前账号是只读角色，没有修改权限', 'danger')
            abort(403)
        return view(*args, **kwargs)
    return wrapper
