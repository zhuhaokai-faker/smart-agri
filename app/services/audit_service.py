# -*- coding: utf-8 -*-
"""
审计日志服务：记录"谁在什么时候把什么改成了什么"。

【审计表的设计原则：只增不改】
  审计日志一旦允许修改就失去了证据价值 —— 改日志的人恰恰可能是要掩盖
  问题的人。生产环境还应通过数据库权限禁止对该表 UPDATE/DELETE。

【为什么用 JSON 而不是给每个字段建列】
  不同表的变更字段完全不同（地块有面积、作物有积温）。给它们建统一的列
  要么列数爆炸，要么大量 NULL。
  ⚠️ 但 MySQL 5.7 **能用 JSON 存、不能把 JSON 展开成行**（没有 JSON_TABLE），
     所以 JSON 只能当"不可查询的附件"。任何需要检索的字段必须抽成独立列 ——
     本项目用 STORED 生成列 changed_fields 把"被修改的字段列表"抽出来建索引，
     这是 5.7 处理 JSON 检索的标准姿势。
"""

from flask import has_request_context, request
from flask_login import current_user

from ..extensions import db
from ..models import AuditLog


def client_ip():
    """取客户端 IP。优先 X-Forwarded-For（部署在 Nginx 后面时）。"""
    if not has_request_context():
        return ''
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()[:45]
    return (request.remote_addr or '')[:45]


def _current_user_id():
    if current_user and getattr(current_user, 'is_authenticated', False):
        return current_user.id
    return None


def log(action, target_table, target_id=None, before=None, after=None, extra=None):
    """
    写一条审计日志。

    before / after 是变更前后的字段字典，会存进 detail JSON。
    changed_fields（生成列）会从 detail 里自动抽出被修改的字段名，
    所以 detail 里必须有一个 'changed_fields' 键。
    """
    detail = {}
    if before is not None or after is not None:
        b = before or {}
        a = after or {}
        detail['before'] = b
        detail['after'] = a
        # 计算字段级差异。生成列 changed_fields 会从这个键取值。
        keys = set(b) | set(a)
        changed = [k for k in keys if b.get(k) != a.get(k)]
        detail['changed_fields'] = ','.join(sorted(changed))[:255]
    if extra:
        detail['extra'] = extra

    entry = AuditLog(
        user_id=_current_user_id(),
        action=action,
        target_table=target_table,
        target_id=target_id,
        detail=detail or None,
        ip=client_ip(),
    )
    db.session.add(entry)
    # 不在这里 commit —— 让审计日志和业务变更**在同一个事务里**提交。
    # 如果业务操作回滚了，审计日志也应该一起回滚，
    # 否则会出现"日志说改了、数据实际没改"的不一致。
    return entry


def log_login(username, success=True, user_id=None):
    """登录审计。登录失败也要记录 —— 这是排查暴力破解的依据。"""
    entry = AuditLog(
        user_id=user_id,
        action='LOGIN',
        target_table='user',
        target_id=user_id,
        detail={'extra': {'username': username, 'success': bool(success)}},
        ip=client_ip(),
    )
    db.session.add(entry)
    return entry


def diff_dict(obj, fields):
    """把 ORM 对象的指定字段抓成字典，用于变更前/后的快照。"""
    return {f: _serialize(getattr(obj, f, None)) for f in fields}


def _serialize(v):
    """把 date / Decimal 这类不能直接进 JSON 的值转成字符串。"""
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)
