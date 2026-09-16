# -*- coding: utf-8 -*-
"""
种植批次服务：状态机流转、生育期校验、有效积温计算。

【为什么业务规则要放在服务层，而不是模型里或视图里】
  · 放模型里 → 模型会和 Flask 的 request / flash / session 耦合，变难复用；
  · 放视图里 → 换个入口（API / 定时任务 / 脚本）就要复制一遍规则，
              复制就会有版本漂移，迟早出现"网页上校验、脚本里不校验"的漏洞。
  服务层是唯一合理的落点：纯业务逻辑，不依赖 Web 上下文。
"""

from datetime import date, timedelta

from ..extensions import db, raw_query
from ..models import FarmingLog, Planting

# 生育期日期的字段顺序。校验时按这个顺序检查单调性。
STAGE_FIELDS = [
    ('sow_date', '播种'),
    ('emerge_date', '出苗'),
    ('flower_date', '开花'),
    ('mature_date', '成熟'),
    ('harvest_date', '收获'),
]


class PlantingError(Exception):
    """业务校验失败。视图层捕获后转成 flash 提示。"""


def validate_stage_dates(sow, emerge, flower, mature, harvest):
    """
    校验生育期日期单调递增：播种 ≤ 出苗 ≤ 开花 ≤ 成熟 ≤ 收获。

    这个校验必须在应用层做，因为：
      · 5.7 **解析 CHECK 约束但静默忽略**（真正的 CHECK 是 8.0.16 才有），
        所以数据库层拦不住；
      · 日期逆序不会报错，只会让后续所有生育期分析（如出苗天数、
        全生育期长度）算出负数，属于"结果错了但不报错"的典型。
    """
    dates = [('播种', sow), ('出苗', emerge), ('开花', flower),
             ('成熟', mature), ('收获', harvest)]
    prev_name, prev_val = None, None
    for name, val in dates:
        if val is None:
            continue
        if prev_val is not None and val < prev_val:
            raise PlantingError(
                f'{name}日期（{val}）不能早于{prev_name}日期（{prev_val}）')
        prev_name, prev_val = name, val


def validate_area(plot, plant_area_mu):
    """种植面积不能超过地块总面积。"""
    if plant_area_mu is None or float(plant_area_mu) <= 0:
        raise PlantingError('种植面积必须大于 0')
    if plot and float(plant_area_mu) > float(plot.area_mu):
        raise PlantingError(
            f'种植面积（{plant_area_mu} 亩）不能超过地块总面积（{plot.area_mu} 亩）')


def change_status(planting, new_status, operator=None):
    """
    执行状态流转，并校验合法性。

    状态机：
        10 待播种 ──> 20 生长中 ──> 30 成熟待收 ──> 40 已收获
              └──────> 50 已废弃  └──────> 50 已废弃

    为什么需要显式状态机：农事流程**不可逆**。
    已经收获的批次退回"生长中"会让产量记录和批次状态不一致，
    后续统计口径全部错乱。用状态机把非法流转挡在入口。
    """
    if not planting.can_transition_to(new_status):
        from config import PLANTING_STATUS
        cur_name = PLANTING_STATUS.get(planting.status, ('未知',))[0]
        new_name = PLANTING_STATUS.get(new_status, ('未知',))[0]
        raise PlantingError(f'不允许的状态流转：{cur_name} → {new_name}')

    old_status = planting.status
    planting.status = new_status

    # 状态与日期保持一致：推进到"已收获"时必须补上收获日期
    if new_status == 40 and not planting.harvest_date:
        planting.harvest_date = date.today()

    return old_status, new_status


def compute_gdd(planting_id):
    """
    计算某批次从播种到成熟的累计有效积温。

    与 SQL 分析 1 用的是同一个公式，这里用 ORM 对象在 Python 侧算 ——
    两条路径互为验证：如果 Python 算出的和 SQL 算出的对不上，
    说明其中一处有问题（种子数据、时区、日期边界等）。
    """
    rows = raw_query("""
        SELECT ROUND(SUM(GREATEST(0, (w.temp_max + w.temp_min) / 2 - c.base_temp)), 1) AS gdd,
               COUNT(*) AS days
        FROM planting p
        JOIN plot pl ON pl.id = p.plot_id
        JOIN farm f  ON f.id  = pl.farm_id
        JOIN crop c  ON c.id  = p.crop_id
        JOIN weather_daily w ON w.region_code = f.region_code
                            AND w.obs_date BETWEEN p.sow_date
                                  AND COALESCE(p.mature_date, p.harvest_date)
        WHERE p.id = :pid
    """, {'pid': int(planting_id)})
    return rows[0] if rows else {'gdd': 0, 'days': 0}


def suggest_next_actions(planting):
    """
    根据当前状态和日期，给出建议的下一步农事操作。

    这是"领域知识写进代码"的体现：系统不只是存数据，
    还能基于作物生育期规律给出提示。
    """
    tips = []
    today = date.today()
    if planting.status == 10 and planting.sow_date <= today:
        tips.append('已到播种期，请及时播种并记录农事操作')
    if planting.status == 20:
        if planting.mature_date and (planting.mature_date - today).days <= 7:
            tips.append('接近成熟期，请准备收获并安排机械')
        g = compute_gdd(planting.id)
        if g['gdd'] and planting.crop and float(g['gdd']) < float(planting.crop.gdd_maturity) * 0.9:
            tips.append(f"积温仅达需求的 "
                        f"{float(g['gdd']) / float(planting.crop.gdd_maturity) * 100:.0f}%，"
                        f"可能推迟成熟")
    if planting.status == 30:
        tips.append('已成熟待收，建议尽快安排收获')
    if planting.status == 40 and not planting.yield_record:
        tips.append('批次已标记收获，但尚未录入产量记录')
    return tips
