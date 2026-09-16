# -*- coding: utf-8 -*-
"""看板蓝图：首页的可视化总览。"""

from flask import Blueprint, render_template
from flask_login import login_required

from ..services import analytics_service as A

bp = Blueprint('dashboard', __name__)


@bp.route('/')
@login_required
def index():
    """看板首页 —— 6 张 ECharts 图表的数据都在这里一次性取好。

    这个页面用 JSON 的方式传数据到模板（tojson 过滤器），
    模板里只负责把它塞给 ECharts。不在模板里做数据加工 ——
    模板里写逻辑是后期最难维护的代码。
    """
    return render_template(
        'dashboard.html',
        summary=A.dashboard_summary(),
        trend=A.yield_trend_monthly(),
        crop_share=A.crop_output_share(),
        regions=A.region_output(),
        top_plots=A.top_plots(10),
        cost=A.cost_structure(),
        abc=A.abc_summary(),
    )
