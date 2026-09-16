# -*- coding: utf-8 -*-
"""审计日志蓝图。"""

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy.orm import joinedload

from ..models import AuditLog
from ..services import analytics_service as A

bp = Blueprint('audit', __name__, url_prefix='/audit')

PER_PAGE = 20


@bp.route('/')
@login_required
def index():
    """操作日志列表（原始记录，detail 显示为 JSON 原文）。"""
    action = (request.args.get('action') or '').strip()
    page = request.args.get('page', 1, type=int)

    q = AuditLog.query.options(joinedload(AuditLog.user))
    if action:
        q = q.filter(AuditLog.action == action)

    pagination = (q.order_by(AuditLog.created_at.desc())
                  .paginate(page=page, per_page=PER_PAGE, error_out=False))

    actions = [r[0] for r in
               AuditLog.query.with_entities(AuditLog.action).distinct().all()]
    return render_template('audit.html', pagination=pagination,
                           action=action, actions=sorted(actions),
                           stats=A.audit_json_stats())


@bp.route('/json-table')
@login_required
def json_table():
    """
    【MySQL 8.0 独有】用 JSON_TABLE 把变更快照展开成"字段级"的行。

    【这个页面演示的核心问题】
    audit_log.detail 存的是 {"before": {...}, "after": {...}} 这样的 JSON。
    在 MySQL 5.7 里，这个 JSON **只能整块读出来给应用层解析** ——
    5.7 有 JSON 类型和 JSON_EXTRACT，但**没有 JSON_TABLE**，
    无法把 JSON 里的键展开成"行"。

    8.0 有了 JSON_TABLE 之后，"这次修改动了哪几个字段、
    每个字段从什么变成了什么"变成了一条普通的 SELECT，
    可以 JOIN、可以 GROUP BY、可以参与窗口函数计算。

    页面把两种写法并排展示，可以直接对比。
    """
    return render_template(
        'audit_json.html',
        SQL=A.SQL,
        changes=A.audit_field_changes(200),
        hot_fields=A.audit_hot_fields(30),
        stats=A.audit_json_stats(),
    )
