# -*- coding: utf-8 -*-
"""
用户服务层：注册、重名校验、防刷、角色管理。

【为什么注册这件事值得单独一个服务层】
  "加个注册表单"听起来是个 20 行的活。但真正要想清楚的是三件事：

  ① **注册后给什么权限？**
      如果默认给可编辑角色，那前面做的整个 RBAC 就白费了 ——
      任何人都能注册进来改数据。
      本项目的策略：**注册即只读，提权由管理员手动做**。

  ② **怎么防刷？**
      开放注册意味着任何人可以批量建号。这里用三层限制 +
      两个机器人陷阱（见 check_rate_limit / verify_human）。

  ③ **重名校验为什么要做三层？**
      前端 AJAX 是体验，服务端是业务，数据库唯一约束是**最终防线**。
      前两层都是"先查再写"，两次查询之间有时间窗 ——
      两个并发请求可以同时查到"用户名可用"，然后都去插入。
      只有数据库的 `uk_username` 能拦住这种竞态。
"""

import re
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from ..extensions import db, raw_scalar
from ..models import User
from . import audit_service


class RegisterError(Exception):
    """注册校验失败。视图层捕获后转成 flash 提示。"""


# =============================================================================
# 常量与策略
# =============================================================================

# 用户名规则：字母、数字、下划线、连字符，3~30 位
#
# 【为什么开头必须是字母】
# 纯数字用户名在很多系统里会和 ID 混淆（"用户 12345"到底是 ID 还是用户名？），
# 而且容易被用来做遍历。要求以字母开头可以避开这两类麻烦。
USERNAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{2,29}$')

# 保留用户名：防止注册成容易混淆或冒充系统的名字
RESERVED_USERNAMES = {
    'admin', 'administrator', 'root', 'system', 'sysadmin',
    'superuser', 'guest', 'test', 'null', 'undefined',
    'api', 'static', 'login', 'logout', 'register',
}

# 防刷策略
RATE_PER_IP_HOUR = 3        # 同一 IP 每小时最多注册 3 个
RATE_PER_IP_DAY = 10        # 同一 IP 每天最多 10 个
RATE_GLOBAL_DAY = 200       # 全站每天最多 200 个（防分布式刷）

# 表单最短填写时间（秒）。低于这个值基本可以断定是脚本：
# 正常人填用户名 + 两遍密码 + 姓名怎么也超过 3 秒。
MIN_FORM_SECONDS = 3


# =============================================================================
# 校验
# =============================================================================

def validate_username(username: str):
    """校验用户名格式。返回规范化后的用户名，不合法则抛 RegisterError。"""
    username = (username or '').strip()
    if not username:
        raise RegisterError('请填写用户名')
    if len(username) < 3:
        raise RegisterError('用户名至少 3 个字符')
    if len(username) > 30:
        raise RegisterError('用户名最多 30 个字符')
    if not USERNAME_RE.match(username):
        raise RegisterError('用户名只能包含字母、数字、下划线和连字符，且必须以字母开头')
    if username.lower() in RESERVED_USERNAMES:
        raise RegisterError(f'"{username}" 是系统保留用户名，请换一个')
    if is_username_taken(username):
        raise RegisterError(f'用户名 "{username}" 已被占用，请换一个')
    return username


def validate_password(password: str, confirm: str):
    """
    校验密码。

    ⚠️ 本项目**刻意不做密码强度策略**（大写/小写/数字/符号/长度组合那一套）。
    原因是：
      · 强制复杂度会把人逼去用 "Password1!" 这类可预测的密码，安全性反而下降；
      · 真正有效的措施是长度下限 + 登录限流 + 强哈希，前两者本项目已具备。
    这里只做一个最低限度：不能为空、两次一致。
    """
    if not password:
        raise RegisterError('请填写密码')
    if password != confirm:
        raise RegisterError('两次输入的密码不一致')


def is_username_taken(username: str) -> bool:
    """用户名是否已被占用。"""
    return db.session.query(
        User.query.filter_by(username=username).exists()
    ).scalar()


def verify_human(form, now_ts: float):
    """
    两个廉价的机器人陷阱。不引入验证码服务，纯前端 + 时间戳。

    ① **蜜罐字段**：表单里放一个 CSS 隐藏的输入框（正常用户看不见、
       也不会填）。自动化脚本会把所有 input 都填上，于是暴露自己。
       这比验证码轻量得多，而且不打扰真实用户。

    ② **最短填写时间**：表单加载时写入一个时间戳，提交时算差值。
       脚本通常是"秒交"，正常人填完怎么也要几秒。

    这两个都不是万无一失的（有心人可以绕过），但能挡掉绝大多数
    无脑脚本 —— 对付自动化刷号，性价比很高。
    """
    # ① 蜜罐
    if (form.get('website') or '').strip():
        raise RegisterError('注册失败，请稍后重试')

    # ② 填写时长
    try:
        started = float(form.get('form_ts') or 0)
    except (TypeError, ValueError):
        started = 0
    if started <= 0 or (now_ts - started) < MIN_FORM_SECONDS:
        raise RegisterError(f'提交太快了，请确认信息后重试（至少 {MIN_FORM_SECONDS} 秒）')


def check_rate_limit(ip: str):
    """
    基于 audit_log 的注册频率限制。

    【为什么复用 audit_log 而不是新建一张限流表】
      项目本来就在记录 `action='REGISTER'` 的审计日志，里面有 ip 和时间。
      限流需要的"某个 IP 最近 N 次注册"从这张表直接数出来即可，
      不用再引入 Redis 或新表。
    ⚠️ 代价：audit_log 只增不减，量大了以后这个 COUNT 会变慢。
       真实高流量场景应该用 Redis 计数器 + 过期时间。
       本项目的量级（几百行）完全够用，但这个边界要说清楚。
    """
    if not ip:
        return  # 拿不到 IP 就不拦（本地开发常见）

    per_hour = raw_scalar("""
        SELECT COUNT(*) FROM audit_log
        WHERE action = 'REGISTER' AND ip = :ip
          AND created_at > NOW() - INTERVAL 1 HOUR
    """, {'ip': ip}) or 0
    if per_hour >= RATE_PER_IP_HOUR:
        raise RegisterError(f'同一网络每小时最多注册 {RATE_PER_IP_HOUR} 个账号，请稍后再试')

    per_day = raw_scalar("""
        SELECT COUNT(*) FROM audit_log
        WHERE action = 'REGISTER' AND ip = :ip
          AND created_at > NOW() - INTERVAL 1 DAY
    """, {'ip': ip}) or 0
    if per_day >= RATE_PER_IP_DAY:
        raise RegisterError(f'同一网络每天最多注册 {RATE_PER_IP_DAY} 个账号')

    global_day = raw_scalar("""
        SELECT COUNT(*) FROM audit_log
        WHERE action = 'REGISTER'
          AND created_at > NOW() - INTERVAL 1 DAY
    """) or 0
    if global_day >= RATE_GLOBAL_DAY:
        raise RegisterError('今日注册量已达上限，请明天再试')


# =============================================================================
# 注册
# =============================================================================

def register(username, password, confirm, real_name, email, form, now_ts, ip,
             default_role=3):
    """
    执行注册。返回新建的 User。

    默认角色是 3（只读）—— 这是本功能最重要的一个设计决策：
    **注册不带来任何写权限**，想要写权限必须由管理员手动提权。
    这样即使注册被刷，攻击者拿到的也只是一堆只能看的账号。
    """
    # 蜜罐 + 填写时长
    verify_human(form, now_ts)

    # 频率限制
    check_rate_limit(ip)

    # 格式与重名
    username = validate_username(username)
    validate_password(password, confirm)

    user = User(
        username=username,
        real_name=(real_name or '').strip()[:50],
        email=(email or '').strip()[:100],
        # ⚠️ 注册一律只读。不要图省事给 2（农艺师）——
        #    那等于把写权限开放给了所有人。
        role=default_role,
        status=1,          # 立即可用（但只读）
    )
    user.set_password(password)

    # 审计日志和用户插入放在**同一个事务**里提交：
    # 如果唯一约束冲突导致回滚，审计记录也应该一起回滚，
    # 否则会留下"注册了但其实没成功"的假记录，
    # 而限流是靠数审计记录实现的 —— 假记录会白白占用配额。
    audit_service.log('REGISTER', 'user', None, after={
        'username': username, 'role': default_role, 'ip': ip,
    })

    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        # 第三层防线：数据库唯一约束。
        # 走到这里说明"先查再插"之间被并发插入了同名用户 ——
        # 前面的 is_username_taken() 查过是空的，但那之后有另一个请求抢先插入。
        # 前端检查和服务端检查都拦不住这种情况，**只有唯一约束能拦**。
        db.session.rollback()
        raise RegisterError(f'用户名 "{username}" 刚被占用，请换一个')

    return user


# =============================================================================
# 角色与状态管理（管理员操作）
# =============================================================================

def change_role(user, new_role, actor):
    """
    修改用户角色。只有管理员能调用（在蓝图层用 @admin_required 保证）。

    ⚠️ 核心保护：**管理员不能改自己的角色**。
    否则一次误操作就能把自己降成只读，系统里可能再没人能改回来 ——
    这类"把自己锁在门外"的问题是权限系统最常见的事故。
    """
    from config import ROLE_NAMES
    if new_role not in ROLE_NAMES:
        raise RegisterError('无效的角色')
    if user.id == actor.id:
        raise RegisterError('不能修改自己的角色 —— 否则可能把系统里最后一个管理员降权')

    # 保护"最后一个管理员"：如果这是全系统最后一个管理员，不许降权
    if user.role == 1 and new_role != 1:
        admin_count = raw_scalar("SELECT COUNT(*) FROM `user` WHERE role = 1 AND status = 1")
        if admin_count is not None and admin_count <= 1:
            raise RegisterError('这是系统里最后一个可用管理员，不能降权')

    old_role = user.role
    user.role = new_role
    audit_service.log('UPDATE', 'user', user.id,
                      before={'role': old_role}, after={'role': new_role,
                                                        'username': user.username})
    db.session.commit()
    return old_role, new_role


def toggle_status(user, actor):
    """
    启用 / 禁用账号。

    同样的自锁保护：不能禁用自己，也不能禁用最后一个管理员。
    """
    if user.id == actor.id:
        raise RegisterError('不能禁用自己的账号')

    if user.status == 1 and user.role == 1:
        admin_count = raw_scalar(
            "SELECT COUNT(*) FROM `user` WHERE role = 1 AND status = 1")
        if admin_count is not None and admin_count <= 1:
            raise RegisterError('这是系统里最后一个可用管理员，不能禁用')

    old = user.status
    user.status = 0 if old == 1 else 1
    audit_service.log('UPDATE', 'user', user.id,
                      before={'status': old},
                      after={'status': user.status, 'username': user.username})
    db.session.commit()
    return old, user.status
