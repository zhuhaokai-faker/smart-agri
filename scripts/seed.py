#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
造数脚本 —— 生成 2022-01 ~ 2024-12 共 3 年的智慧农业业务数据

【为什么用 Python 造数，而不是纯 SQL 或存储过程】
  纯 SQL      能造出"行"，造不出"关系"。产量要和施肥量、积温、降水、地力等级
              产生真实的统计关联，在 SQL 里做这个需要大量子查询，而且无法表达
              "30% 概率地块闲置"这类概率分布。
  存储过程    (1) RAND() 无法设种子 → 同一脚本两次执行结果不同，**不可复现**。
                  这对面试演示是致命的：demo 前重跑一次，数据和之前的截图对不上。
              (2) 几百行循环 + 逐行 INSERT + 每次 autocommit，性能极差。
              (3) 过程式代码难调试、难版本管理，面试官会觉得"为了炫技"。
  Python+批插 ← 采用。完全掌控概率分布、可设随机种子、可跨表引用真实 ID。
              random.seed(42) 保证任何人任何机器跑出来都是同一份数据。

【为什么数据分布必须精心设计】
如果所有数值都是均匀随机数，所有曲线都会变成直线：月度产量没有起伏、同比永远
是 0%、施肥量和产量毫无相关性、ABC 分析分不出 A/B/C。项目直接失去演示价值。
所以下面刻意植入了 7 类分布，见各生成函数的注释。

【为什么必须造满 3 年】
同比(YoY)需要跨 13 个月以上取样。只造 12 个月的话，"去年同期"永远落在数据范围
之外，同比列全是 NULL，功能形同虚设。连续干旱日数的分析也需要跨年数据才能暴露
"年月编码非连续"的坑。
"""

import argparse
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------- 环境预检
# ⚠️ 必须放在**所有第三方 import 之前**。
#    否则当依赖缺失时，解释器会先炸在 `import pymysql` 这一行，
#    抛出的 ModuleNotFoundError 指向这里而不是真正的原因（解释器选错了）。
#    检查逻辑见项目根的 env_check.py。
from env_check import check  # noqa: E402

check()

import pymysql               # noqa: E402

from config import DB_CONFIG  # noqa: E402

# Windows 控制台默认用 GBK 编码，直接 print 中文会乱码。
# 强制 stdout 走 UTF-8，保证在任何终端里都能正确显示。
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# =============================================================================
# 全局参数
# =============================================================================
SEED = 42
DATA_START = date(2022, 1, 1)
DATA_END = date(2024, 12, 31)

# 年份增长因子 —— 制造同比增长（② 同比增长 ~10%/年）。
# 不植入的话所有 YoY 都是 0%，同比功能演示不出来。
YEAR_TREND = {2022: 1.00, 2023: 1.10, 2024: 1.21}

# 地力等级 → 单产系数（③ 地块质量分层）。
# 优/中/差三档拉开约 35% 差距。不做分层的话所有地块单产差不多，
# 单产 TopN 排行和 ABC 帕累托分析都没有区分度。
#
# ⚠️ 为什么是 1.15/1.00/0.85 而不是更夸张的 1.30/1.00/0.70？
#    地力差异是一个**与施肥量无关的方差来源**，它会稀释"施肥量—单产"的
#    相关系数。若地力差异过大（CV 超过 15%），施肥的真实影响会被淹没，
#    相关系数掉到 0.2~0.3，散点图看起来就是一团噪声，分析页讲不出结论。
#    这里把地力 CV 控制在 10% 左右，与施肥量的方差量级相当，
#    相关系数才能落在 0.5~0.6 这个"显著但非完美"的真实区间。
FERTILITY_FACTOR = {1: 1.15, 2: 1.00, 3: 0.85}

# 每农场地块数。样本量直接决定相关系数的稳定性：
# 每个作物只有 4~6 条记录时，r 的抽样波动可以达到 ±0.4，
# 甚至出现"施肥越多产量越低"的负相关假象（纯粹是噪声）。
# 12 个地块 × 5 农场 × 6 茬口 ≈ 300 个批次，分摊到每个作物 20~40 条，
# 相关系数才收敛到可信区间。
PLOTS_PER_FARM = 12

# ⑥ 地区差异：各地区光温水土条件不同，产量系数略有差异
REGION_FACTOR = {0: 1.02, 1: 1.00, 2: 0.98, 3: 1.01, 4: 0.99}


# =============================================================================
# 基础数据定义
# =============================================================================

# 5 个气象区 / 农场。annual_mean + amplitude 决定该地区的气温年曲线。
REGIONS = [
    # code,   省,       市,      县,      农场名,                 经度,    纬度,  年均温, 振幅, 年降水
    ('R2301', '黑龙江省', '绥化市', '北林区', '绥化万丰农机合作社',   126.98, 46.65,  4.0, 20.0,  520),
    ('R3201', '江苏省',   '盐城市', '射阳县', '射阳沃野家庭农场',     120.16, 33.35, 15.0, 12.0, 1050),
    ('R3701', '山东省',   '潍坊市', '寿光市', '寿光绿丰蔬菜基地',     119.16, 36.71, 13.0, 13.0,  660),
    ('R4101', '河南省',   '周口市', '扶沟县', '扶沟金穗种植合作社',   114.65, 33.62, 15.0, 13.0,  750),
    ('R5101', '四川省',   '成都市', '崇州市', '崇州天府粮仓农场',     104.07, 30.67, 16.0,  9.0,  900),
]

# 作物品种（含作物生理参数 —— GDD 计算的基础）
#
# 【gdd_maturity 是怎么来的（重要）】
# 最初这里填的是文献值（玉米 2400℃·d 之类），但实测发现各作物的
# "实际积温 / 所需积温"中位数只有 0.1~0.7 —— **所有作物在有生之年都积累不到
# 成熟所需积温**。原因是文献值对应另一套基数温度和更长的生育期。
# 后果：积温系数长期卡在保底区间剧烈波动，把施肥信号完全淹没，
#       相关系数掉到 0.2 甚至负数。
# 现在的值由 scripts/calibrate_gdd.py 按本项目的实际气候**标定**得出：
# 取该作物在适宜地区、真实播期窗口内，生育期长度上能积累的 GDD 中位数 × 0.95。
# 这也是真实农艺做法 —— 用多年平均积温确定品种熟期。
# 使典型年份刚好成熟、冷年份略不足、暖年份有余，"积温充足度"才有区分度。
#
# 字段：code, 名称, 品种, 类别, 生物学零度, 所需积温, 生育期天数,
#       基准单产(kg/亩), 基准价(元/kg), 播期窗口, 适宜地区
#
# 【播期窗口 sow_win = {茬口: (始播月, 始播日, 可延天数)}】
# 必须按作物真实农时给，不能所有秋茬都笼统地"9~10月播种"：
#   冬小麦/油菜是秋播越冬作物（9~10 月播，次年 5~6 月收）
#   白菜是夏末秋初播种（7~8 月播，10~11 月收）
# 【为什么秋茬蔬菜的播期窗口开得宽（7 月 ~ 10 月初）】
# 江苏、四川冬季温和，可以种**秋冬茬蔬菜**：9~10 月播种、12 月到次年 1 月收获。
# 如果秋茬窗口只开到 8 月初，那么最晚 11 月就全部收完 ——
# 12 月 31 日的快照里**不会有任何批次处于"30 成熟待收"状态**，
# 那个状态会变成一条永远查不出数据的分支，状态机也就从未被完整验证过。
#
# 如果把白菜也按 9~10 月播，它在低温下积累的积温极低，
# 标定出来的"所需积温"只有 270℃·d —— 数字自洽了，但和真实白菜差一个量级，
# 懂行的面试官一眼看出问题。
CROPS = [
    # code,   名称,  品种,        类别, 零度,  所需积温, 生育期, 基准单产, 基准价, 播期窗口,                                 适宜地区
    ('CR001', '玉米', '郑单958',     1, 10.0, 1320.0, 120,  600.0, 2.40, {'春': (3, 20, 50)},                        (0, 2, 3, 4)),
    ('CR002', '水稻', '南粳9108',    1, 10.0, 1780.0, 140,  550.0, 2.80, {'春': (4,  1, 50)},                        (1, 4)),
    ('CR003', '小麦', '济麦22',      1,  0.0, 1870.0, 230,  420.0, 2.50, {'秋': (9, 25, 30)},                        (1, 2, 3)),
    ('CR004', '大豆', '黑农84',      1, 10.0, 1440.0, 120,  180.0, 5.20, {'春': (4, 20, 35)},                        (0, 3)),
    ('CR005', '番茄', '粉果将军',    2, 10.0,  950.0, 110, 4000.0, 3.60, {'春': (3, 15, 35), '秋': (7,  5, 95)},     (2, 4)),
    ('CR006', '黄瓜', '津优1号',     2, 12.0,  620.0,  95, 5000.0, 3.20, {'春': (3, 20, 35), '秋': (7, 10, 90)},     (2,)),
    ('CR007', '白菜', '北京新3号',   2,  5.0,  950.0,  85, 3500.0, 1.60, {'秋': (7, 15, 75)},                        (0, 2, 3)),
    ('CR008', '辣椒', '螺丝椒',      2, 12.0,  980.0, 110, 2500.0, 5.00, {'春': (3, 20, 35)},                        (2, 4)),
    ('CR009', '苹果', '红富士',      3,  5.0, 2990.0, 200, 2500.0, 6.00, {'春': (3,  1, 30)},                        (2, 3)),
    ('CR010', '葡萄', '巨峰',        3, 10.0, 2020.0, 180, 1800.0, 8.00, {'春': (3, 20, 30)},                        (2, 3)),
    ('CR011', '棉花', '中棉所49',    4, 12.0, 1840.0, 150,  120.0, 18.00, {'春': (4, 10, 30)},                       (3,)),
    ('CR012', '油菜', '中油杂19',    4,  5.0,  650.0, 180,  180.0, 5.50, {'秋': (9, 20, 30)},                        (1, 4)),
]

# 「成熟 → 收获」的滞后天数（按作物类别）。
# ⚠️ 不能设成固定 2~9 天。真实生产中作物成熟后往往要等天气窗口
#    （谷物等籽粒干燥、果蔬等收购商档期），滞后一两周很常见。
#    设得太短还有个副作用：状态「30 成熟待收」要求数据截止日恰好落在
#    "成熟后、收获前"这段窗口内 —— 窗口只有几天时，快照几乎不可能拍到
#    任何处于该状态的批次，那个状态在系统里形同虚设。
HARVEST_LAG_DAYS = {1: (3, 18), 2: (1, 6), 3: (2, 12), 4: (4, 15)}

# 播种密度（株/亩）
DENSITY = {1: (3500, 5000), 2: (2200, 3200), 3: (80, 160), 4: (3500, 5500)}
# 生育期需水量（mm），用于降水满足度的钟形曲线
WATER_NEED = {1: 450.0, 2: 380.0, 3: 520.0, 4: 480.0}

# =============================================================================
# 成本模型 —— 全部按「元/亩」定价，再乘以地块面积
# =============================================================================
#
# ⚠️ 成本必须**按亩计价再乘面积**，不能给一个与面积无关的固定值。
#    最初人工费写成 uniform(0.5, 4.0) 工时（与面积无关），结果 300 亩的地块
#    只算了 2 小时人工，亩均成本只有 250 元 —— 而真实农业成本是粮食约 800 元/亩、
#    设施蔬菜 5000~8000 元/亩。成本低估 20 倍，直接导致投入产出比算出 103 倍、
#    利润率 10250% 这种荒谬结果。
#
#    这个 bug 的危险之处在于**查询本身完全正确**，错的是输入数据 ——
#    做数据分析时，"结果看起来不合理"往往要先怀疑数据口径而不是 SQL。
#
# 【数值依据】按作物类别的真实成本结构（元/亩）：
#   粮食作物：机械化程度高、人工占比低（约 20%），亩均总成本 800~1200
#   蔬菜：    高度依赖人工（约 60%），亩均总成本 5000~9000
#   水果：    人工占比最高（约 60%+，修剪/疏果/套袋），亩均总成本 6000~10000
#   经济作物：介于粮食和蔬菜之间，亩均总成本 1200~2500
PER_MU_COST = {
    # 类别: (人工, 机械, 农药, 水电, 种子)
    1: {'labor': (150, 400),   'machine': (120, 200), 'pesticide': (40, 80),
        'water': (30, 80),     'seed': (80, 150)},
    2: {'labor': (3000, 7000), 'machine': (80, 150),  'pesticide': (150, 400),
        'water': (40, 100),    'seed': (400, 900)},
    3: {'labor': (3500, 8000), 'machine': (60, 120),  'pesticide': (200, 500),
        'water': (40, 100),    'seed': (300, 800)},
    4: {'labor': (400, 1000),  'machine': (100, 180), 'pesticide': (60, 150),
        'water': (30, 80),     'seed': (60, 120)},
}
# 人工折算率（元/工时）。按 8 小时一工日、日薪 120~180 元换算。
LABOR_RATE = (15.0, 22.0)
# 机械作业折算率（元/工时）
MACHINE_RATE = (80.0, 140.0)


def season_seq(year: int, season: str) -> int:
    """
    茬口序号 = 年*2 + (春=1 / 秋=2)，线性递增，相邻茬口恰好差 1。

    ⚠️ 这是同期群分析和连续种植判定的前提。不能用 '2024春' 这类字符串
       （相减无意义），也不能只用年份（丢失同年春秋茬的区别），
       更不能把年月编码成 202402 这种数字（相邻月差 89 而不是 1）。
    """
    return year * 2 + (1 if season == '春' else 2)


# =============================================================================
# 气象数据生成
# =============================================================================

def gen_weather(rng, region):
    """
    生成某气象区 3 年逐日气象。

    气温 = 年均温 + 振幅 × sin(2π(doy-110)/365) + 随机扰动
    相位 110 使最高温落在 7 月中旬，符合北半球实际。

    降水用季节概率分布，并**人工植入干旱过程**（⑤ 气象影响）。
    植入干旱的原因：纯随机降水下，"连续干旱日数"分析永远查不出有意思的结果，
    而且"某年因旱减产"这个故事必须能从数据里读出来 —— 否则积温/降水分析
    和产量数据互相打架。
    """
    _, _, _, _, _, _, _, annual_mean, amplitude, _precip = region

    # 每个区每年植入 2~3 段干旱，长度 15~40 天，主要落在生长季
    dry_spells = []
    for year in (2022, 2023, 2024):
        for _ in range(rng.randint(2, 3)):
            start = date(year, 1, 1) + timedelta(days=rng.randint(95, 250) - 1)
            dry_spells.append((start, start + timedelta(days=rng.randint(15, 40) - 1)))

    def in_dry(d):
        return any(s <= d <= e for s, e in dry_spells)

    rows = []
    d = DATA_START
    while d <= DATA_END:
        doy = d.timetuple().tm_yday
        seasonal = annual_mean + amplitude * math.sin(2 * math.pi * (doy - 110) / 365.0)
        temp_avg = seasonal + rng.gauss(0, 2.2)
        diurnal = rng.uniform(6.0, 12.0)
        temp_max, temp_min = temp_avg + diurnal / 2, temp_avg - diurnal / 2

        m = d.month
        if m in (6, 7, 8):
            rain_prob, rain_scale = 0.42, 18.0
        elif m in (4, 5, 9, 10):
            rain_prob, rain_scale = 0.28, 11.0
        elif m in (3, 11):
            rain_prob, rain_scale = 0.16, 6.0
        else:
            rain_prob, rain_scale = 0.10, 4.0

        if in_dry(d):
            precip, sunshine, humidity = 0.0, rng.uniform(8.0, 11.5), rng.uniform(30.0, 48.0)
        elif rng.random() < rain_prob:
            precip = round(rng.expovariate(1.0 / rain_scale), 2)
            sunshine, humidity = max(0.0, rng.gauss(3.5, 2.0)), rng.uniform(62.0, 90.0)
        else:
            precip, sunshine, humidity = 0.0, rng.uniform(5.0, 10.0), rng.uniform(40.0, 70.0)

        solar = max(0.0, 6.0 + 12.0 * math.sin(2 * math.pi * (doy - 100) / 365.0)
                    + rng.gauss(0, 1.5))

        rows.append((region[0], d.isoformat(), round(temp_max, 2), round(temp_min, 2),
                     round(temp_avg, 2), round(precip, 2),
                     round(min(sunshine, 14.0), 2), round(humidity, 2), round(solar, 2)))
        d += timedelta(days=1)
    return rows


# =============================================================================
# 产量模型 —— 造数脚本的核心
# =============================================================================

def fertilizer_response(fert_kg_per_mu):
    """
    施肥量 → 产量响应系数，体现**边际报酬递减**。

    用对数 y = a + b·ln(1+x) 而不是线性，因为：
      (1) 线性不符合农学实际 —— 施肥超过一定量后增产变缓甚至减产；
      (2) 对数曲线的"先陡后平"恰好刻画边际报酬递减；
      (3) 相关分析才有讲头：如果产量和施肥量是完全线性关系（r≈1），
          反而显得假 —— 真实农业数据的相关系数落在 0.5~0.8 的
          "显著但非完美"区间。做到 r≈1 面试官一眼看出是编的。

    标定：施肥量 15→95 kg/亩 时响应系数 1.12→1.59，约 42% 跨度。
    这个跨度是照真实氮肥效应定的 —— 施氮量从低到高带来 30~50% 的增产
    是农学教科书里的标准数字。跨度太小的话施肥量本身方差不足，
    相关系数会被地力、降水等无关方差稀释到 0.3 以下。

    【为什么最终的相关系数只有 0.45 左右，而不是 0.8】
    这是**真实存在的统计现象，不是数据缺陷**：
    皮尔逊相关系数只度量**线性**关联，而这里真实关系是对数型（边际报酬递减，
    曲线是凹的）。用线性指标去度量非线性关系，r 会被系统性低估。
    所以在分析 3 里，相关系数偏低本身就是"关系是非线性的"证据 ——
    这比造出一个 r≈0.9 的完美线性假数据更有说服力，也更经得起追问。
    """
    # 截距 0.25 而非 0.40：斜率决定**方差**（进而决定相关系数），
    # 截距决定**水平**（进而决定平均单产是否落在真实区间）。
    # 把截距降下来，可以在保住相关系数的同时，让各作物单产回到
    # 真实水平（玉米约 700 kg/亩、水稻约 800 kg/亩、小麦约 580 kg/亩）。
    return 0.25 + 0.26 * math.log1p(fert_kg_per_mu)


def gdd_factor(gdd_actual, gdd_required):
    """
    积温满足度 → 产量系数。

    积温不足是北方农业最主要的减产因素（霜冻前没成熟）。刻意做成非线性：
    达到需求后继续增温只带来很小收益，而不足则大幅减产。
    这样"某年积温偏低 → 减产"的故事才成立。
    """
    ratio = gdd_actual / gdd_required if gdd_required > 0 else 1.0
    if ratio >= 1.0:
        return 1.0 + min(0.08, (ratio - 1.0) * 0.35)
    return max(0.45, 0.55 + 0.45 * ratio)


def water_factor(precip_actual, precip_need):
    """
    降水满足度 → 产量系数，用**钟形曲线**而非线性。

    因为降水对产量的影响是"两端都减产"：干旱减产，渍涝同样减产（根系缺氧）。
    线性模型会得出"降水越多越好"的荒谬结论。高斯钟形的最优点是降水恰好满足需水。

    系数 0.70 + 0.30 而非 0.60 + 0.40：降水同样是稀释相关系数的方差来源。
    真实农业生产中灌溉会部分抵消降水波动，所以降水的边际影响不应过大。
    """
    if precip_need <= 0:
        return 1.0
    ratio = precip_actual / precip_need
    return 0.70 + 0.30 * math.exp(-((ratio - 1.0) ** 2) / (2 * 0.38 ** 2))


# =============================================================================
# 各表数据生成
# =============================================================================

def gen_users():
    from werkzeug.security import generate_password_hash
    return [
        ('admin',       generate_password_hash('admin123'), '系统管理员', 'admin@smartagri.local',  '13800000001', 1),
        ('agronomist',  generate_password_hash('agri123'),  '张农艺',     'zhang@smartagri.local',  '13800000002', 2),
        ('agronomist2', generate_password_hash('agri123'),  '李农艺',     'li@smartagri.local',     '13800000003', 2),
        ('viewer',      generate_password_hash('view123'),  '访客',       'viewer@smartagri.local', '13800000004', 3),
    ]


def gen_farms():
    rows = []
    for i, (code, prov, city, county, name, lon, lat, *_rest) in enumerate(REGIONS, start=1):
        rows.append((f'FARM{i:03d}', name, prov, city, county, code, lon, lat,
                     0.0, f'{name[:4]}负责人', f'1390000{i:04d}', 1))
    return rows


def gen_plots(rng, farm_ids):
    """
    每农场 6 个地块，共 30 个。

    地力等级分布 优30% / 中50% / 差20%（③ 地块质量分层）——
    这是产量差异的主要来源，也是单产 TopN 和 ABC 分析有意义的前提。
    """
    plots, pno = [], 0
    for farm_idx, farm_id in enumerate(farm_ids):
        center_lon, center_lat = REGIONS[farm_idx][5], REGIONS[farm_idx][6]
        for k in range(PLOTS_PER_FARM):
            pno += 1
            plots.append((
                f'PLOT{pno:03d}',
                f'{REGIONS[farm_idx][4][:2]}{k + 1:02d}号地',
                farm_id,
                round(rng.uniform(60, 480), 2),
                rng.choice([1, 2, 3, 4]),
                rng.choice([1, 2, 3, 4]),
                rng.choices([1, 2, 3], weights=[0.30, 0.50, 0.20])[0],
                round(center_lon + rng.uniform(-0.05, 0.05), 6),
                round(center_lat + rng.uniform(-0.05, 0.05), 6),
                rng.randint(8, 320),
                1,
                '',
            ))
    return plots


def gen_plantings(rng, plots, crops):
    """
    生成种植批次。

    1. 不是每个地块每一茬都种 —— 每茬约 18% 概率闲置/轮休。
       既符合轮作实际，也让同期群分析（Cohort）有"地块退出"的现象可分析。
       如果所有地块每茬都满种，留存率永远 100%，分析就没意义了。
    2. 作物按地区适宜性和茬口季节筛选。
    3. 播种日期在季节窗口内随机，产生年际波动。
    4. 生育期各节点日期严格单调递增（⑦ 状态机自洽）。
    """
    plantings = []
    bno = 0
    for year in (2022, 2023, 2024):
        for season in ('春', '秋'):
            for plot in plots:
                # gen_plots 的列顺序：0=plot_no 1=name 2=farm_id 3=area_mu
                #                      4=soil 5=irrigation 6=fertility ...
                plot_no, farm_id, area, fertility = plot[0], plot[2], plot[3], plot[6]
                farm_idx = farm_id - 1

                if rng.random() < 0.18:       # 轮休
                    continue

                candidates = [c for c in crops if season in c[9] and farm_idx in c[10]]
                if not candidates:
                    continue
                crop = rng.choice(candidates)
                (crop_code, _n, _v, category, base_temp,
                 gdd_req, growth_days, base_yield, base_price, sow_win, _) = crop

                # 播期按作物真实农时窗口抽取，而不是所有春茬/秋茬共用同一窗口
                wm, wd, wspan = sow_win[season]
                sow = date(year, wm, wd) + timedelta(days=rng.randint(0, wspan))

                grow = int(growth_days * rng.uniform(0.92, 1.12))
                emerge = sow + timedelta(days=rng.randint(6, 14))
                flower = sow + timedelta(days=int(grow * rng.uniform(0.45, 0.55)))
                mature = sow + timedelta(days=grow)
                harvest = mature + timedelta(days=rng.randint(*HARVEST_LAG_DAYS[category]))

                # 状态由"数据截止日"倒推，保证与日期自洽（⑦）
                # 这样 ~15% 的批次不是终态，"有效批次"的过滤条件才有实际作用。
                # 若 100% 都是"已收获"，面试官会问"你这个 WHERE 过滤了什么？"
                if harvest <= DATA_END:
                    status = 50 if rng.random() < 0.03 else 40
                elif mature <= DATA_END:
                    status = 30
                else:
                    status = 20

                bno += 1
                plantings.append({
                    'batch_no': f'B{year}{1 if season == "春" else 2}-{plot_no[-3:]}-{bno:04d}',
                    'plot_no': plot_no, 'farm_idx': farm_idx,
                    'fertility': fertility, 'area': area,
                    'crop_code': crop_code, 'category': category,
                    'season': f'{year}{season}', 'season_year': year,
                    'season_seq': season_seq(year, season),
                    'sow': sow, 'emerge': emerge, 'flower': flower,
                    'mature': mature, 'harvest': harvest, 'status': status,
                    'plant_area_mu': round(area * rng.uniform(0.75, 1.0), 2),
                    'base_temp': base_temp, 'gdd_req': gdd_req, 'gdd_maturity': gdd_req,
                    'base_yield': base_yield, 'base_price': base_price,
                    'density': rng.randint(*DENSITY[category]),
                    'water_need': WATER_NEED[category],
                    'region_code': REGIONS[farm_idx][0],
                })
    # ------------------------------------------------------------------
    # 追加：下一季（2025 春）的**种植计划**（status=10 待播种）
    #
    # 【为什么必须有这批数据】
    #   一个持续运行的农场管理系统里，必然存在"已排定但还没到播种期"的批次。
    #   如果数据里全是已收获的批次，状态机里的 10(待播种) 就从来不会被用到，
    #   UI 上那个筛选项永远查不出东西，10 → 20 的流转路径也从未被数据验证过。
    #   （本项目最初就漏了这批，导致"待播种"和"成熟待收"两个状态零数据。）
    #
    # 【口径一致】这批的播种期在未来，所以没有产量记录 ——
    #   与 config.py 里"有效种植批次 = status IN (20,30,40)"的定义吻合。
    # ------------------------------------------------------------------
    plan_year = DATA_END.year + 1        # 2025
    for plot in plots:
        plot_no, farm_id, area, fertility = plot[0], plot[2], plot[3], plot[6]
        farm_idx = farm_id - 1

        if rng.random() < 0.35:          # 不是所有地块都提前排了计划
            continue
        candidates = [c for c in crops if '春' in c[9] and farm_idx in c[10]]
        if not candidates:
            continue

        crop = rng.choice(candidates)
        (crop_code, _n, _v, category, base_temp, gdd_req,
         growth_days, base_yield, base_price, sow_win, _) = crop

        wm, wd, wspan = sow_win['春']
        sow = date(plan_year, wm, wd) + timedelta(days=rng.randint(0, wspan))
        grow = int(growth_days * rng.uniform(0.92, 1.12))
        emerge = sow + timedelta(days=rng.randint(6, 14))
        flower = sow + timedelta(days=int(grow * rng.uniform(0.45, 0.55)))
        mature = sow + timedelta(days=grow)
        harvest = mature + timedelta(days=rng.randint(*HARVEST_LAG_DAYS[category]))

        bno += 1
        plantings.append({
            'batch_no': f'B{plan_year}1-{plot_no[-3:]}-{bno:04d}',
            'plot_no': plot_no, 'farm_idx': farm_idx,
            'fertility': fertility, 'area': area,
            'crop_code': crop_code, 'category': category,
            'season': f'{plan_year}春', 'season_year': plan_year,
            'season_seq': season_seq(plan_year, '春'),
            'sow': sow, 'emerge': emerge, 'flower': flower,
            'mature': mature, 'harvest': harvest,
            'status': 10,                      # 待播种
            'plant_area_mu': round(area * rng.uniform(0.75, 1.0), 2),
            'base_temp': base_temp, 'gdd_req': gdd_req, 'gdd_maturity': gdd_req,
            'base_yield': base_yield, 'base_price': base_price,
            'density': rng.randint(*DENSITY[category]),
            'water_need': WATER_NEED[category],
            'region_code': REGIONS[farm_idx][0],
        })

    return plantings


# 各作业类型的人工/机械投入权重（收获和除草最费工，打药最省工）
OP_WEIGHT = {10: 1.0, 20: 1.2, 30: 0.8, 40: 0.7, 50: 1.5, 60: 2.0, 70: 1.5, 90: 0.5}
# 需要机械作业的类型
MACHINE_OPS = {10, 30, 60, 70}


def make_user_picker(ids, seed=SEED + 1000):
    """
    返回一个"随机挑一个用户"的函数，使用**独立于主 rng 的随机源**。

    ⚠️ 这是一个必须独立的地方，原因很隐蔽：

      `random.choice(seq)` 内部用 `_randbelow(len(seq))` 实现，
      而 `_randbelow` 是**拒绝采样** —— 消耗多少个底层随机位，
      取决于列表长度和抽到的值（可能重抽）。

      于是如果这里共用主 rng，`rng.choice(用户列表)` 的消耗量就会随
      **用户表里有多少个账号**而变化。而 user 表是会变的（有人注册就多几个）。

      后果：数据库里多了一个注册用户，重跑造数就会得到**完全不同的产量数据** ——
      同一个 seed 却算出不同的相关系数。
      实测过：账号从 2 个变到 9 个，玉米的相关系数从 0.52 变成 0.16。

      这直接破坏了"固定种子必然可复现"这个核心性质，
      而可复现性对演示是刚需（重跑一次数据和之前的截图对不上）。

    解法：给用户选择配一个独立的 Random 实例。
    这样无论 user 表怎么变，业务数据的随机序列都不受影响。

    （第一次写的时候我想用"检验 n=2/4/9 三者的后续随机数是否相同"来排除这个可能，
      但只抽了 5 次就下结论 —— 那时三者碰巧一致，掩盖了问题。
      正确的检验要抽几百次，因为拒绝采样是概率性的。）
    """
    r = random.Random(seed)

    def pick():
        return r.choice(ids)

    return pick


def gen_farming_logs(rng, plantings, pick_user):
    """
    农事作业流水。

    【关键：人工费和机械费必须先按"元/亩"定总额，再分摊到各次作业】
    不能给每次作业一个与面积无关的固定工时 —— 那样 300 亩的地块和 3 亩的
    地块花一样多的人工，亩均成本会随面积急剧下降，投入产出比彻底失真。
    正确做法：批次人工总额 = 面积 × 亩均人工费率，再按作业权重分摊。
    """
    logs = []
    for p in plantings:
        sow, span = p['sow'], max(1, (p['harvest'] - p['sow']).days)
        area = max(p['plant_area_mu'], 1.0)
        cfg = PER_MU_COST[p['category']]

        ops = [(70, sow - timedelta(days=rng.randint(4, 12))),   # 整地
               (10, sow)]                                        # 播种
        for i in range(rng.choices([2, 3, 4], weights=[0.35, 0.45, 0.20])[0]):
            ops.append((20, sow + timedelta(days=int(span * (i + 1) / 4) + rng.randint(-4, 4))))
        for _ in range(rng.choices([1, 2, 3, 4, 5], weights=[0.20, 0.30, 0.25, 0.15, 0.10])[0]):
            ops.append((30, sow + timedelta(days=rng.randint(10, max(11, span - 5)))))
        for _ in range(rng.choices([1, 2, 3, 4], weights=[0.25, 0.35, 0.30, 0.10])[0]):
            ops.append((40, sow + timedelta(days=rng.randint(20, max(21, span - 5)))))
        if rng.random() < 0.45:
            ops.append((50, sow + timedelta(days=rng.randint(15, max(16, span - 10)))))
        if p['status'] == 40:
            ops.append((60, p['harvest']))

        # 只保留落在数据范围内的作业，再按它们分摊成本
        ops = [(t, d) for t, d in ops if DATA_START <= d <= DATA_END]
        if not ops:
            continue

        total_labor = area * rng.uniform(*cfg['labor'])
        total_machine = area * rng.uniform(*cfg['machine'])
        w_sum = sum(OP_WEIGHT[t] for t, _ in ops)
        m_sum = sum(OP_WEIGHT[t] for t, _ in ops if t in MACHINE_OPS) or 1

        for op_type, d in ops:
            w = OP_WEIGHT[op_type]
            labor_cost = round(total_labor * w / w_sum * rng.uniform(0.85, 1.15), 2)
            labor_hours = round(labor_cost / rng.uniform(*LABOR_RATE), 2)
            if op_type in MACHINE_OPS:
                machine_cost = round(total_machine * w / m_sum * rng.uniform(0.85, 1.15), 2)
                machine_hours = round(machine_cost / rng.uniform(*MACHINE_RATE), 2)
            else:
                machine_cost = machine_hours = 0.0
            logs.append((
                p['batch_no'], op_type, d.isoformat(),
                f"{p['plot_no'][-3:]}号地{OP_LABEL[op_type]}作业",
                labor_hours, machine_hours, labor_cost, machine_cost,
                pick_user(),
            ))
    return logs


OP_LABEL = {10: '播种', 20: '施肥', 30: '灌溉', 40: '打药',
            50: '除草', 60: '收获', 70: '整地', 90: '其他'}


def gen_input_costs(rng, plantings):
    """
    农资投入明细。这里同时埋下 ④ 施肥响应 的自变量。

    关键：**每亩施肥量**在 20~80 kg 区间大幅波动。这样后面产量与施肥量的
    相关系数才有足够的变化范围。若各地块施肥量都差不多，相关系数趋近 0，
    分析 3 直接废掉。
    """
    costs = []
    for p in plantings:
        area = max(p['plant_area_mu'], 1.0)
        span = max(1, (p['harvest'] - p['sow']).days)
        cfg = PER_MU_COST[p['category']]

        # --- 种子：先按亩定总价，再反推单价 ---
        # 用量按亩给（1.5~4.5 kg/亩），单价由"亩均种子成本"反推 ——
        # 这样既保证了用量是合理的农艺数字，又保证了成本是合理的财务数字。
        seed_qty = round(area * rng.uniform(1.5, 4.5), 3)
        seed_price = round(area * rng.uniform(*cfg['seed']) / max(seed_qty, 0.001), 4)
        costs.append((p['batch_no'], 10, f"{p['crop_code'][-3:]}号种子",
                      seed_qty, 'kg', seed_price, p['sow'].isoformat()))

        # --- 化肥：用量由 fert_per_mu 决定（这是产量模型的自变量）---
        # 每亩总量 15~95kg，分 2~4 次施用。
        # 区间宽是为了让施肥量这个自变量有足够方差，相关系数才有分辨力。
        # ⚠️ 这里是**唯一不从"亩均成本"倒推用量的投入品** ——
        #    因为用量是我们要分析的自变量，必须保持它自身的合理分布，
        #    单价则按真实化肥价格区间给（1.8~4.2 元/kg）。
        p['fert_per_mu'] = rng.uniform(15.0, 95.0)
        n = rng.randint(2, 4)
        for i in range(n):
            # 每次施用的用量有 ±6% 的浮动（分次施用不可能精确等分）。
            # ⚠️ 这个浮动不能太大：SQL 分析里"施肥量"是用
            #    SUM(quantity)/面积 反算出来的，分次施用的随机浮动会变成
            #    **自变量的测量误差**。而测量误差会系统性地**衰减**相关系数
            #    （回归稀释/attenuation bias）——这是真实的统计学现象，
            #    但浮动给到 ±15% 时衰减过于严重，会把 r 从 0.5 压到 0.35。
            qty = round(area * p['fert_per_mu'] / n * rng.uniform(0.94, 1.06), 3)
            d = min(p['sow'] + timedelta(days=int(span * (i + 1) / (n + 1))), DATA_END)
            costs.append((p['batch_no'], 20,
                          rng.choice(['尿素', '磷酸二铵', '复合肥', '硫酸钾', '有机肥']),
                          qty, 'kg', round(rng.uniform(1.8, 4.2), 4), d.isoformat()))

        # --- 农药：总用量按亩给，单价由"亩均农药成本"反推，再分次施用 ---
        pest_total_cost = area * rng.uniform(*cfg['pesticide'])
        pest_total_qty = area * rng.uniform(0.05, 0.30)
        pest_price = round(pest_total_cost / max(pest_total_qty, 0.001), 4)
        n_pest = rng.randint(1, 4)
        for _ in range(n_pest):
            d = min(p['sow'] + timedelta(days=rng.randint(15, max(16, span - 5))), DATA_END)
            costs.append((p['batch_no'], 30,
                          rng.choice(['杀虫剂', '杀菌剂', '除草剂']),
                          round(pest_total_qty / n_pest, 3), 'L',
                          pest_price, d.isoformat()))

        # --- 灌溉水电 ---
        water_qty = round(area * rng.uniform(8, 35), 2)
        water_price = round(area * rng.uniform(*cfg['water']) / max(water_qty, 0.01), 4)
        costs.append((p['batch_no'], 50, '灌溉水电', water_qty, '度',
                      water_price, p['sow'].isoformat()))

        # ⚠️ 这里**不再产生"机械作业费"(type=70)** —— 机械费已经在
        #    farming_log.machine_cost 里按作业逐笔记账了。
        #    如果再在这里记一笔，v_planting_cost 会把机械成本算两遍，
        #    总成本虚高。同一笔成本只能有一个记账位置，这是成本核算的底线。
    return costs


def gen_yield_records(rng, plantings, weather_index):
    """
    产量记录 —— 项目最核心的事实表，也是把 7 类分布"焊"在一起的地方。

        单产 = 基准单产
             × 地力系数      (③ 优1.20/中1.00/差0.80)
             × 地区系数      (⑥ 各地区光温水土不同)
             × 施肥响应      (④ 边际报酬递减)
             × 积温满足度    (⑤ 从真实气象累计出来的 GDD)
             × 降水满足度    (⑤ 钟形曲线)
             × 年际增长      (② ~10%/年)
             × 随机噪声

    每一项都有明确农学含义，且**积温/降水是从真实生成的气象数据算出来的**，
    所以 SQL 里独立算出的 GDD 能解释产量差异 —— 分析模块之间才自洽。
    如果产量直接随机生成，积温分析和相关性分析会得出"积温与产量无关"的荒谬结论，
    项目的几个分析页会互相打架。

    只对 status=40（已收获）的批次生成 —— 与 config.py 的统一口径一致。
    """
    records = []
    for p in plantings:
        if p['status'] != 40:
            continue

        # 逐日累计有效积温与降水量（与 SQL 分析 1 的算法完全一致）
        gdd = precip = 0.0
        d = p['sow']
        while d <= p['harvest']:
            w = weather_index.get((p['region_code'], d))
            if w:
                gdd += max(0.0, (w[0] + w[1]) / 2.0 - p['base_temp'])
                precip += w[2]
            d += timedelta(days=1)

        f_fert = fertilizer_response(p['fert_per_mu'])
        f_soil = FERTILITY_FACTOR[p['fertility']]
        f_gdd = gdd_factor(gdd, p['gdd_req'])
        f_water = water_factor(precip, p['water_need'])
        f_trend = YEAR_TREND[p['season_year']]
        f_region = REGION_FACTOR[p['farm_idx']]

        # 噪声用对数正态（保证为正），CV≈7%。
        # 不能太大 —— 否则真实信号被淹没，相关系数掉到 0.2 以下，
        # "施肥显著影响产量"这个结论就讲不出来了。
        # 也不能太小 —— 否则 r 逼近 1，一眼假。
        noise = math.exp(rng.gauss(0, 0.07))

        # 【因子收缩 (shrinkage) —— 防止连乘导致极值失控】
        # 因子连乘时，一旦全部有利，乘积可以到 2.5 倍以上，算出
        # 大豆 408 kg/亩（6.1 吨/公顷，超过世界纪录）这种不可能的极值。
        # 现实中各因子并非完全独立 —— 地力差的地块往往会被投入更多管理来补偿，
        # 而不存在"所有条件同时最优"的田块。
        # 这里对**非施肥类**因子做收缩：f → 1 + (f − 1) × 0.75，压掉 25% 的方差。
        # ⚠️ 刻意不收缩施肥因子 —— 它是我们要分析的自变量，
        #    压缩它会人为削弱相关系数，让分析 3 失去意义。
        other = 1.0 + (f_soil * f_gdd * f_water * f_trend * f_region * noise - 1.0) * 0.75

        ypm = p['base_yield'] * f_fert * other
        if p['status'] == 50:                 # 绝收
            ypm *= 0.15

        harvest_area = round(p['plant_area_mu'] * rng.uniform(0.94, 1.0), 2)
        yield_kg = round(ypm * harvest_area, 2)

        grade = rng.choices([1, 2, 3], weights=[0.35, 0.45, 0.20])[0]
        unit_price = round(p['base_price'] * {1: 1.15, 2: 1.00, 3: 0.82}[grade]
                           * rng.uniform(0.92, 1.08), 4)

        records.append((
            p['batch_no'], p['plot_id'], p['crop_id'], p['harvest'].isoformat(),
            harvest_area, yield_kg, grade, unit_price,
            round(rng.uniform(11.5, 15.5), 2),
            rng.choice(['', '', '', '品质良好', '部分霉变', '规格整齐']),
            rng.choice([1, 2]),
        ))
    return records


# =============================================================================
# 入库
# =============================================================================

def gen_audit_logs(rng, plots, plantings, pick_user):
    """
    生成操作审计记录。

    【为什么要造这批数据】
    审计日志如果只有登录记录、没有带有 before/after 变更快照的条目，
    那么 app/services/analytics_service.py 里那两个 **JSON_TABLE** 查询
    （字段级变更明细、最常被修改的字段）在页面上就是空的 ——
    而 JSON_TABLE 正好是本项目 8.0 迁移的一个重点展示。

    【数据设计】
    每条变更记录都包含 detail JSON：
        {"before": {...}, "after": {...}, "changed_fields": "..."}
    其中 before/after 只放**该次真正改动到的字段**，
    changed_fields 用逗号连接（与生成列同名，但这里由应用层写入）。

    刻意让某些字段比其他字段更容易被改（比如 remark、area_mu），
    这样"最常被修改的字段"那个分析才有区分度。
    """
    FIELD_POOL = [
        # (字段名, 生成随机值的函数, 权重)
        ('area_mu',         lambda: str(round(rng.uniform(50, 500), 2)), 18),
        ('fertility_level', lambda: str(rng.choice([1, 2, 3])),          12),
        ('irrigation_type', lambda: str(rng.choice([1, 2, 3, 4])),       10),
        ('soil_type',       lambda: str(rng.choice([1, 2, 3, 4])),        8),
        ('remark',          lambda: rng.choice(['已平整', '修了排水沟', '', '土壤板结',
                                                '增加有机肥', '安装了滴灌']), 22),
        ('name',            lambda: rng.choice(['东区', '西区', '南片', '北片',
                                                '试验田', '示范田']),       10),
    ]
    plt_weight = [f[2] for f in FIELD_POOL]

    logs = []
    n_plot_edits = rng.randint(60, 90)

    for _ in range(n_plot_edits):
        plot = rng.choice(plots)
        plot_no, area, fert = plot[0], plot[3], plot[6]
        # 一次修改通常只动 1~3 个字段
        k = rng.choices([1, 2, 3], weights=[0.55, 0.32, 0.13])[0]
        chosen = set()
        while len(chosen) < k:
            chosen.add(rng.choices(FIELD_POOL, weights=plt_weight)[0][0])

        before, after = {}, {}
        for fname, gen, _w in FIELD_POOL:
            if fname not in chosen:
                continue
            if fname == 'area_mu':
                old_v = area
                new_v = round(float(area) * rng.uniform(0.85, 1.20), 2)
            elif fname == 'fertility_level':
                old_v = fert
                new_v = rng.choice([v for v in (1, 2, 3) if v != fert])
            elif fname == 'name':
                old_v, new_v = gen(), gen()
            else:
                old_v, new_v = gen(), gen()
            before[fname] = str(old_v)
            after[fname] = str(new_v)

        detail = {
            'before': before,
            'after': after,
            'changed_fields': ','.join(sorted(chosen)),
        }
        logs.append((pick_user(), 'UPDATE', 'plot',
                     plots.index(plot) + 1, detail, '127.0.0.1'))

    # 批次状态流转（changed_fields 只有 status）
    for p_ in rng.sample(plantings, min(30, len(plantings))):
        if p_['status'] == 10:
            continue
        old = 20 if p_['status'] in (30, 40) else 10
        logs.append((pick_user(), 'STATUS_CHANGE', 'planting',
                     None, {'before': {'status': str(old)},
                            'after': {'status': str(p_['status'])},
                            'changed_fields': 'status'}, '192.168.1.' + str(rng.randint(10, 99))))

    # 产量录入
    for _ in range(25):
        logs.append((pick_user(), 'CREATE', 'yield_record', None,
                     {'after': {'yield_kg': str(round(rng.uniform(5000, 90000), 1)),
                                'unit_price': str(round(rng.uniform(1.5, 8), 2))},
                      'changed_fields': 'unit_price,yield_kg'}, '127.0.0.1'))

    rng.shuffle(logs)
    return logs


def clear_all(cur):
    """
    清空**业务数据**表，但**保留 `user` 表**。

    【为什么 user 表不能被清空】
    项目支持公开注册，注册进来的用户是**真实数据**，不是造出来的演示数据。
    如果这里把 user 一起 TRUNCATE 掉：
      · 用户下次登录会发现账号没了，而且没有任何提示；
      · 管理员手动提过的权限全部丢失；
      · 审计日志里那些 user_id 外键会变成悬挂引用
        （虽然 fk_audit_user 是 ON DELETE SET NULL，但 TRUNCATE 绕过外键约束，
         会留下指向不存在用户的日志）。
    一句话：**造数脚本的职责是生成演示数据，不是销毁真实数据。**

    其余表都是纯演示数据，可以放心重建 —— 而且必须重建，
    因为造数用固定随机种子，只有从空表开始才能保证结果可复现。
    """
    cur.execute('SET FOREIGN_KEY_CHECKS = 0')
    for t in ('audit_log', 'yield_record', 'input_cost', 'farming_log',
              'planting', 'weather_daily', 'crop', 'plot', 'farm'):
        cur.execute(f'TRUNCATE TABLE `{t}`')
    cur.execute('SET FOREIGN_KEY_CHECKS = 1')


def ensure_demo_users(cur, demo_rows):
    """
    确保 4 个演示账号存在，但**不修改已存在的**。

    【为什么用 "存在就跳过" 而不是 "存在就覆盖"】
      upsert 会把已有账号的角色重置回演示值 —— 听起来像是"恢复演示状态"，
      但如果管理员刚把某个账号提权，重跑一次造数就被改回去了，很意外。
      密码同理：重置成 admin123 会让人以为自己改的密码丢了。

      所以策略是：**只补缺失的，不碰已有的**。
      如果确实想恢复演示账号到初始状态，手动删掉再跑一遍即可。

    `ON DUPLICATE KEY UPDATE username = username` 是 MySQL 里
    "insert if not exists" 的标准写法 —— 比 INSERT IGNORE 精确，
    因为 INSERT IGNORE 会把所有错误都吞掉（包括数据类型错误），
    而这里只处理唯一键冲突这一种情况。
    """
    cur.executemany(
        'INSERT INTO `user` (username, password_hash, real_name, email, phone, role) '
        'VALUES (%s, %s, %s, %s, %s, %s) '
        'ON DUPLICATE KEY UPDATE username = username',
        demo_rows)


def main():
    ap = argparse.ArgumentParser(description='生成智慧农业演示数据')
    ap.add_argument('--keep', action='store_true', help='保留现有数据（默认先清空）')
    args = ap.parse_args()

    rng = random.Random(SEED)
    print('=' * 72)
    print(f'  智慧农业演示数据生成   数据范围 {DATA_START} ~ {DATA_END}   种子 {SEED}')
    print('=' * 72)

    conn = pymysql.connect(database='smart_agri', **DB_CONFIG)
    cur = conn.cursor()

    if not args.keep:
        print('[0/8] 清空已有数据 ...')
        clear_all(cur)
        conn.commit()

    # ------------------------------------------------------------ 用户
    # ⚠️ 不清空 user 表，只补缺失的演示账号（注册进来的用户必须保留）
    print('[1/8] 演示账号 ...')
    cur.execute('SELECT COUNT(*) FROM `user`')
    users_before = cur.fetchone()[0]
    ensure_demo_users(cur, gen_users())
    conn.commit()
    cur.execute('SELECT COUNT(*) FROM `user`')
    users_after = cur.fetchone()[0]
    if users_before:
        print(f'       -> 保留了原有 {users_before} 个账号，'
              f'新增 {users_after - users_before} 个演示账号'
              f'（共 {users_after} 个）')
    else:
        print(f'       -> 创建 {users_after} 个演示账号')

    # 农事/产量记录的操作人必须**按角色查**，不能用 "user_ids[1:3]" 这种位置假设 ——
    # 一旦有注册用户混进来（或者演示账号被删过），位置就错位了，
    # 会把操作人记成只读用户，甚至索引越界。
    cur.execute('SELECT id FROM `user` WHERE role = 2 ORDER BY id')
    agronomist_ids = [r[0] for r in cur.fetchall()]
    if not agronomist_ids:
        # 兜底：万一农艺师被删光了，退而用所有启用的账号，
        # 保证造数不会因为没人可用而中断
        cur.execute('SELECT id FROM `user` WHERE status = 1 ORDER BY id')
        agronomist_ids = [r[0] for r in cur.fetchall()]
    if not agronomist_ids:
        print('  ⚠️ user 表里没有任何可用账号，无法生成农事/产量记录的操作人')
        return 1

    # ⚠️ 用**独立的随机源**挑操作人。
    #    绝不能共用主 rng —— 否则 rng.choice 的消耗量会随用户账号数变化，
    #    导致业务数据的随机序列整体偏移（详见 make_user_picker 的说明）。
    pick_user = make_user_picker(agronomist_ids)

    # ------------------------------------------------------------ 农场
    print('[2/8] 农场 ...')
    cur.executemany('INSERT INTO `farm` (farm_no, name, province, city, county, '
                    'region_code, longitude, latitude, total_area_mu, owner_name, '
                    'contact_phone, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    gen_farms())
    conn.commit()
    cur.execute('SELECT id FROM `farm` ORDER BY id')
    farm_ids = [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------ 气象
    print('[3/8] 逐日气象（5 区 × 1095 天）...')
    weather_rows, weather_index = [], {}
    for region in REGIONS:
        rows = gen_weather(rng, region)
        weather_rows.extend(rows)
        for r in rows:
            weather_index[(r[0], date.fromisoformat(r[1]))] = (r[2], r[3], r[5])
    cur.executemany('INSERT INTO `weather_daily` (region_code, obs_date, temp_max, '
                    'temp_min, temp_avg, precipitation, sunshine_hours, humidity, '
                    'solar_radiation) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)', weather_rows)
    conn.commit()
    print(f'       -> {len(weather_rows)} 行')

    # ------------------------------------------------------------ 作物
    print('[4/8] 作物品种 ...')
    cur.executemany('INSERT INTO `crop` (crop_code, name, variety, category, base_temp, '
                    'gdd_maturity, growth_days, status) VALUES (%s,%s,%s,%s,%s,%s,%s,1)',
                    [c[:7] for c in CROPS])
    conn.commit()
    cur.execute('SELECT crop_code, id FROM `crop`')
    crop_ids = {r[0]: r[1] for r in cur.fetchall()}

    # ------------------------------------------------------------ 地块
    print('[5/8] 地块 ...')
    plots = gen_plots(rng, farm_ids)
    cur.executemany('INSERT INTO `plot` (plot_no, name, farm_id, area_mu, soil_type, '
                    'irrigation_type, fertility_level, longitude, latitude, altitude_m, '
                    'status, remark) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', plots)
    conn.commit()
    cur.execute('SELECT plot_no, id FROM `plot`')
    plot_ids = {r[0]: r[1] for r in cur.fetchall()}
    # 回填农场总面积
    cur.execute('UPDATE `farm` f SET total_area_mu = (SELECT COALESCE(SUM(p.area_mu),0) '
                'FROM `plot` p WHERE p.farm_id = f.id)')
    conn.commit()

    # ------------------------------------------------------------ 种植批次
    print('[6/8] 种植批次 ...')
    plantings = gen_plantings(rng, plots, CROPS)
    # 把业务编号解析成真实的数据库自增 ID
    for p in plantings:
        p['crop_id'] = crop_ids[p['crop_code']]
        p['plot_id'] = plot_ids[p['plot_no']]
    cur.executemany(
        'INSERT INTO `planting` (batch_no, plot_id, crop_id, season, season_year, '
        'season_seq, plant_area_mu, density_per_mu, sow_date, emerge_date, flower_date, '
        'mature_date, harvest_date, status, agronomist_id, remark) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        [(p['batch_no'], p['plot_id'], p['crop_id'], p['season'], p['season_year'],
          p['season_seq'], p['plant_area_mu'], p['density'], p['sow'].isoformat(),
          p['emerge'].isoformat(), p['flower'].isoformat(), p['mature'].isoformat(),
          p['harvest'].isoformat(), p['status'], pick_user(), '')
         for p in plantings])
    conn.commit()
    cur.execute('SELECT batch_no, id FROM `planting`')
    batch_id = {r[0]: r[1] for r in cur.fetchall()}
    print(f'       -> {len(plantings)} 个批次')

    # ------------------------------------------------------------ 从属表
    print('[7/8] 农事记录 / 农资投入 / 产量记录 ...')
    logs = gen_farming_logs(rng, plantings, pick_user)
    cur.executemany('INSERT INTO `farming_log` (planting_id, op_type, op_date, description, '
                    'labor_hours, machine_hours, labor_cost, machine_cost, operator_id) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    [(batch_id[r[0]], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8])
                     for r in logs])
    conn.commit()
    print(f'       -> 农事记录 {len(logs)} 行')

    costs = gen_input_costs(rng, plantings)
    cur.executemany('INSERT INTO `input_cost` (planting_id, input_type, item_name, quantity, '
                    'unit, unit_price, record_date) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    [(batch_id[r[0]], r[1], r[2], r[3], r[4], r[5], r[6]) for r in costs])
    conn.commit()
    print(f'       -> 农资投入 {len(costs)} 行')

    # ⚠️ 顺序不能换：gen_yield_records 依赖 gen_input_costs 写入的 fert_per_mu
    yields = gen_yield_records(rng, plantings, weather_index)
    cur.executemany('INSERT INTO `yield_record` (planting_id, plot_id, crop_id, harvest_date, '
                    'harvest_area_mu, yield_kg, grade, unit_price, moisture_content, '
                    'quality_note, recorder_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    [(batch_id[r[0]], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10])
                     for r in yields])
    conn.commit()
    print(f'       -> 产量记录 {len(yields)} 行')

    # ------------------------------------------------------------ 审计日志
    log_rows = gen_audit_logs(rng, plots, plantings, pick_user)
    cur.executemany(
        'INSERT INTO `audit_log` (user_id, action, target_table, target_id, detail, ip) '
        'VALUES (%s, %s, %s, %s, %s, %s)',
        [(u, a, t, tid, json.dumps(d, ensure_ascii=False), ip)
         for u, a, t, tid, d, ip in log_rows])
    conn.commit()
    print(f'       -> 审计日志 {len(log_rows)} 条（含 before/after 变更快照）')

    # ------------------------------------------------------------ 自检
    print('[8/8] 数据自检（施肥量 vs 单产的相关系数，目标 0.5~0.8）')
    print('-' * 72)
    cur.execute("""
        SELECT c.name,
               COUNT(*) AS n,
               ROUND(AVG(y.yield_per_mu), 1),
               ROUND((COUNT(*) * SUM(ft.fpm * y.yield_per_mu) - SUM(ft.fpm) * SUM(y.yield_per_mu))
                     / NULLIF(SQRT((COUNT(*) * SUM(ft.fpm * ft.fpm) - SUM(ft.fpm) * SUM(ft.fpm))
                                 * (COUNT(*) * SUM(y.yield_per_mu * y.yield_per_mu)
                                    - SUM(y.yield_per_mu) * SUM(y.yield_per_mu))), 0), 3)
        FROM yield_record y
        JOIN crop c ON c.id = y.crop_id
        JOIN (SELECT i.planting_id, SUM(i.quantity) / NULLIF(MAX(p.plant_area_mu),0) AS fpm
              FROM input_cost i JOIN planting p ON p.id = i.planting_id
              WHERE i.input_type = 20 GROUP BY i.planting_id) ft
          ON ft.planting_id = y.planting_id
        GROUP BY c.id, c.name HAVING n >= 3 ORDER BY 3 DESC
    """)
    print(f"{'作物':<6}{'样本':>5}{'平均单产':>11}{'施肥-单产相关系数 r':>21}")
    for row in cur.fetchall():
        flag = '  <-- 偏低' if row[3] is not None and row[3] < 0.3 else ''
        print(f'{row[0]:<6}{row[1]:>5}{row[2]:>11}{str(row[3]):>21}{flag}')

    print('-' * 72)
    cur.execute("""SELECT (SELECT COUNT(*) FROM farm), (SELECT COUNT(*) FROM plot),
        (SELECT COUNT(*) FROM crop), (SELECT COUNT(*) FROM planting),
        (SELECT COUNT(*) FROM farming_log), (SELECT COUNT(*) FROM input_cost),
        (SELECT COUNT(*) FROM weather_daily), (SELECT COUNT(*) FROM yield_record)""")
    r = cur.fetchone()
    print(f'农场 {r[0]} | 地块 {r[1]} | 作物 {r[2]} | 种植批次 {r[3]}')
    print(f'农事记录 {r[4]} | 农资投入 {r[5]} | 气象 {r[6]} | 产量记录 {r[7]}')
    print('=' * 72)
    print('造数完成。下一步：python scripts/gen_geojson.py  （它要读地块坐标，必须在本步之后）')
    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
