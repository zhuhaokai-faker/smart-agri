# -*- coding: utf-8 -*-
"""
地图蓝图：地块单产热力图。

【前后端如何协作】
  地图本身需要两类数据：
    ① 几何边界（GeoJSON）—— 由 scripts/gen_geojson.py 生成，静态文件；
    ② 业务指标（单产/产值/地力）—— 随数据变化，走这个蓝图的 JSON 接口。
  分开的原因：边界几乎不变（改了地块才需要重新生成），指标每次查询都要最新的。
  把指标也塞进静态 GeoJSON 就得每次造数后重新生成文件，很笨重。

  【为什么不把指标直接合并进 GeoJSON 一次性返回】
  那样每换一个着色指标（单产 / 产值 / ROI / 面积）都要重新生成一遍 GeoJSON。
  分成"静态几何 + 动态指标"后，前端按 plot_no 关联即可，
  切换指标只是换一个字典的取值，零额外请求。
"""

from flask import Blueprint, jsonify, render_template
from flask_login import login_required

from ..services import analytics_service as A

bp = Blueprint('map', __name__, url_prefix='/map')


@bp.route('/')
@login_required
def index():
    return render_template('map.html')


@bp.route('/api/plot-metrics')
@login_required
def plot_metrics():
    """
    地块指标接口，返回 {plot_no: {...}} 的字典，供前端与 GeoJSON 关联。

    【为什么返回字典而不是数组】
    前端拿到 GeoJSON 后要按 plot_no 给每个多边形着色。
    数组的话前端每次都要 O(n) 查找；字典是 O(1) 直取。
    n=60 时无所谓，但这个习惯在 n=10 万时必须养成 ——
    不要在渲染循环里做线性查找。

    【为什么用 jsonify 而不是自己拼 JSON】
    jsonify 会自动处理中文编码、Decimal 序列化、Content-Type，
    且 Flask 3 默认不转义非 ASCII 字符（config 里也设了 JSON_AS_ASCII）。
    """
    data = {}
    for row in A.map_plot_metrics():
        data[row['plot_no']] = {
            'crop_name': row['crop_name'],
            'yield_per_mu': float(row['yield_per_mu'] or 0),
            'output_per_mu': float(row['output_per_mu'] or 0),
            'fertility_level': row['fertility_level'],
            'record_cnt': row['record_cnt'],
            'total_area_mu': float(row['total_area_mu'] or 0),
        }
    return jsonify(data)
