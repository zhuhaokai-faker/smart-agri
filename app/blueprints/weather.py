# -*- coding: utf-8 -*-
"""气象蓝图：逐日气象数据的查询与月度汇总。"""

from flask import Blueprint, render_template, request
from flask_login import login_required

from ..extensions import raw_query, raw_scalar
from ..services import analytics_service as A

bp = Blueprint('weather', __name__, url_prefix='/weather')

PER_PAGE = 20


@bp.route('/')
@login_required
def index():
    """
    气象数据页。

    展示逐日数据（分页）+ 月度汇总图（气温与降水双轴）。
    气象是这个项目能够做"有效积温分析"的前提 ——
    没有逐日气温，GDD 就无从算起。
    """
    region = (request.args.get('region') or '').strip()
    year = request.args.get('year', type=int)
    page = request.args.get('page', 1, type=int)

    regions = raw_query("""
        SELECT region_code,
               MIN(obs_date) AS start_date, MAX(obs_date) AS end_date,
               COUNT(*) AS days
        FROM weather_daily GROUP BY region_code ORDER BY region_code
    """)
    if not region and regions:
        region = regions[0]['region_code']
    if not year:
        year = raw_scalar("SELECT MAX(YEAR(obs_date)) FROM weather_daily") or 2024

    years = raw_query("""
        SELECT DISTINCT YEAR(obs_date) AS y FROM weather_daily ORDER BY y DESC
    """)

    where, params = ['region_code = :region', 'YEAR(obs_date) = :year'], \
                    {'region': region, 'year': year}

    total = raw_scalar(f'SELECT COUNT(*) FROM weather_daily WHERE {" AND ".join(where)}',
                       params)
    rows = raw_query(f"""
        SELECT obs_date, temp_max, temp_min, temp_avg, precipitation,
               sunshine_hours, humidity, solar_radiation
        FROM weather_daily
        WHERE {' AND '.join(where)}
        ORDER BY obs_date
        LIMIT :limit OFFSET :offset
    """, {**params, 'limit': PER_PAGE, 'offset': (page - 1) * PER_PAGE})

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    return render_template('weather.html',
                           regions=regions, years=years,
                           region=region, year=year,
                           rows=rows, page=page, total_pages=total_pages,
                           total=total,
                           monthly=A.monthly_weather(region, year))
