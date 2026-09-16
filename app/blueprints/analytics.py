# -*- coding: utf-8 -*-
"""
分析蓝图 —— 8 组高级分析页面。

【这 8 个页面的统一设计：SQL 源码 + 图表 + 原理解析 三段式】
  每个页面都把**实际执行的 SQL** 原文渲染出来（带语法高亮），
  和结果图表并排展示。这样做有两个目的：
    1. 面试演示时可以直接指着页面讲实现思路，不用翻代码文件；
    2. 强迫自己保证"页面上展示的 SQL"和"实际跑的 SQL"是同一份 ——
       SQL 从 services 层读取，不手抄一份到模板里
       （手抄就会漂移，改了查询忘了改注释是迟早的事）。
"""

from flask import Blueprint, abort, render_template, request
from flask_login import login_required

from ..models import Crop, Planting
from ..services import analytics_service as A

bp = Blueprint('analytics', __name__, url_prefix='/analytics')


def _planting_choices():
    """积温分析的下拉框选项（只列有气象数据覆盖的批次）。"""
    return (Planting.query
            .order_by(Planting.season_seq.desc(), Planting.id)
            .limit(200).all())


@bp.route('/gdd')
@login_required
def gdd():
    """分析 1：有效积温累计与成熟度判定"""
    pid = request.args.get('planting_id', type=int)
    if not pid:
        # 默认选一个已收获、生育期完整的批次
        row = (Planting.query
               .filter(Planting.status == 40)
               .order_by(Planting.id).first())
        pid = row.id if row else None

    curve, planting, gdd_info = [], None, None
    if pid:
        planting = Planting.query.get(pid)
        if planting is None:
            abort(404)
        curve = A.gdd_curve(pid)
        gdd_info = curve[-1] if curve else None

    return render_template(
        'analytics/gdd.html',
        SQL=A.SQL,
        plantings=_planting_choices(),
        selected_id=pid,
        planting=planting,
        curve=curve,
        gdd_info=gdd_info,
        maturity_list=A.gdd_maturity_list(200),
    )


@bp.route('/yoy-mom')
@login_required
def yoy_mom():
    """分析 2：产量同比 (YoY) + 环比 (MoM)"""
    return render_template('analytics/yoy_mom.html', rows=A.yield_yoy_mom(),
                           lag_demo=A.lag_vs_selfjoin_demo(), SQL=A.SQL)


@bp.route('/correlation')
@login_required
def correlation():
    """分析 3：单产影响因子的皮尔逊相关系数"""
    crops = Crop.query.order_by(Crop.id).all()
    cid = request.args.get('crop_id', type=int)
    # 默认选样本量最大的作物，散点图更有代表性
    by_crop = A.fertilizer_correlation_by_crop()
    if not cid and by_crop:
        top = max(by_crop, key=lambda r: r['sample_n'] or 0)
        cid = next((c.id for c in crops if c.name == top['crop_name']), None)

    return render_template(
        'analytics/correlation.html',
        SQL=A.SQL,
        by_crop=by_crop,
        pooled=A.fertilizer_correlation_pooled(),
        crops=crops,
        selected_id=cid,
        scatter=A.yield_scatter(cid) if cid else [],
        scatter_crop=next((c for c in crops if c.id == cid), None),
    )


@bp.route('/roi')
@login_required
def roi():
    """分析 4：投入产出比 (ROI)"""
    return render_template(
        'analytics/roi.html',
        SQL=A.SQL,
        ranking=A.roi_ranking(50),
        by_crop=A.roi_by_crop(),
    )


@bp.route('/topn')
@login_required
def topn():
    """分析 5：分组 TopN —— 各作物单产前 3 的地块"""
    n = request.args.get('n', default=3, type=int)
    n = max(1, min(n, 10))
    return render_template(
        'analytics/topn.html',
        SQL=A.SQL,
        rows=A.topn_by_crop(n),
        verify=A.topn_by_crop_deterministic(n),
        top_n=n,
    )


@bp.route('/abc')
@login_required
def abc():
    """分析 6：产量 ABC 帕累托分析"""
    return render_template(
        'analytics/abc.html',
        SQL=A.SQL,
        rows=A.abc_analysis(40),
        summary=A.abc_summary(),
    )


@bp.route('/cohort')
@login_required
def cohort():
    """分析 7：地块连续种植季同期群分析"""
    return render_template('analytics/cohort.html', rows=A.cohort_retention(), SQL=A.SQL)


@bp.route('/drought')
@login_required
def drought():
    """分析 8：连续干旱日数预警（gaps-and-islands）"""
    min_days = request.args.get('min_days', default=10, type=int)
    min_days = max(3, min(min_days, 60))
    return render_template(
        'analytics/drought.html',
        SQL=A.SQL,
        rows=A.drought_events(min_days, 50),
        min_days=min_days,
    )
