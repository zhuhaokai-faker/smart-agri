# -*- coding: utf-8 -*-
"""基础数据蓝图：农场、地块、作物的查询与维护。"""

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)
from flask_login import login_required
from sqlalchemy.orm import joinedload

from ..decorators import editor_required
from ..extensions import db
from ..models import Crop, Farm, Plot
from ..services import audit_service

bp = Blueprint('base_data', __name__)

PER_PAGE = 15

# 变更审计要盯的字段 —— 只记这些，不把整行都塞进 JSON
PLOT_AUDIT_FIELDS = ['name', 'area_mu', 'soil_type', 'irrigation_type',
                     'fertility_level', 'status', 'remark']


@bp.route('/plots')
@login_required
def plot_list():
    """
    地块列表（分页 + 搜索 + 农场筛选）。

    ⚠️ 这里用 joinedload 预加载 farm，避免 N+1 查询。
       不用 joinedload 的话，模板里每行访问 plot.farm.name 都会单独发一条
       SQL —— 15 行就是 16 次查询。列表页是 N+1 最容易发生的地方。
    """
    page = request.args.get('page', 1, type=int)
    keyword = (request.args.get('q') or '').strip()
    farm_id = request.args.get('farm_id', type=int)

    q = Plot.query.options(joinedload(Plot.farm))
    if keyword:
        q = q.filter(db.or_(Plot.plot_no.like(f'%{keyword}%'),
                            Plot.name.like(f'%{keyword}%')))
    if farm_id:
        q = q.filter(Plot.farm_id == farm_id)

    pagination = q.order_by(Plot.plot_no).paginate(
        page=page, per_page=PER_PAGE, error_out=False)

    return render_template('plot_list.html',
                           pagination=pagination,
                           keyword=keyword,
                           farm_id=farm_id,
                           farms=Farm.query.order_by(Farm.id).all())


@bp.route('/plots/<int:plot_id>')
@login_required
def plot_detail(plot_id):
    plot = Plot.query.options(joinedload(Plot.farm)).get_or_404(plot_id)
    # 该地块的种植历史（含作物），同样预加载
    from ..models import Planting
    plantings = (Planting.query
                 .options(joinedload(Planting.crop))
                 .filter(Planting.plot_id == plot_id)
                 .order_by(Planting.season_seq.desc())
                 .all())
    return render_template('plot_detail.html', plot=plot, plantings=plantings)


@bp.route('/plots/<int:plot_id>/edit', methods=['GET', 'POST'])
@login_required
@editor_required
def plot_edit(plot_id):
    plot = Plot.query.get_or_404(plot_id)
    if request.method == 'POST':
        # 变更前先抓快照 —— 审计日志要能回答"改成了什么"
        before = audit_service.diff_dict(plot, PLOT_AUDIT_FIELDS)

        plot.name = (request.form.get('name') or '').strip() or plot.name
        try:
            plot.area_mu = float(request.form.get('area_mu') or plot.area_mu)
        except ValueError:
            flash('面积必须是数字', 'warning')
            return render_template('plot_edit.html', plot=plot)
        plot.soil_type = request.form.get('soil_type', type=int) or plot.soil_type
        plot.irrigation_type = request.form.get('irrigation_type', type=int) or plot.irrigation_type
        plot.fertility_level = request.form.get('fertility_level', type=int) or plot.fertility_level
        plot.status = request.form.get('status', type=int) if \
            request.form.get('status') is not None else plot.status
        plot.remark = (request.form.get('remark') or '').strip()

        after = audit_service.diff_dict(plot, PLOT_AUDIT_FIELDS)
        audit_service.log('UPDATE', 'plot', plot.id, before=before, after=after)
        db.session.commit()
        flash('地块信息已更新', 'success')
        return redirect(url_for('base_data.plot_detail', plot_id=plot.id))

    return render_template('plot_edit.html', plot=plot)


@bp.route('/crops')
@login_required
def crop_list():
    """
    作物品种列表。

    这一页同时展示作物的**生理参数**（生物学零度、所需积温、生育期），
    它们是全部有效积温分析的基础 —— 把领域参数放在页面上可见，
    比埋在代码里更有说服力。
    """
    crops = Crop.query.order_by(Crop.category, Crop.id).all()
    return render_template('crop_list.html', crops=crops)


@bp.route('/farms')
@login_required
def farm_list():
    farms = Farm.query.options(joinedload(Farm.plots)).order_by(Farm.id).all()
    return render_template('farm_list.html', farms=farms)
