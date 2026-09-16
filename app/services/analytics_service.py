# -*- coding: utf-8 -*-
"""
分析服务层 —— 封装全部原生 SQL 分析查询（MySQL 8.0 实现）。

【为什么单独一层，不直接写在 blueprint 里】
  1. 蓝图只负责"取数据 + 渲染"，不该混着几百行 SQL；
  2. 服务层可以被脚本、测试、定时任务直接调用（比如导出 Excel 报表）；
  3. 同一个分析被多个页面复用时只写一次。

【为什么分析查询用原生 SQL 而不是 ORM】
  窗口函数、CTE、JSON_TABLE 这些能力**没有任何一个能用 ORM 表达**。
  ORM 擅长的是 CRUD 和关联加载；分析查询必须落到 SQL 层。
  面试里能主动说清"什么时候该用 ORM、什么时候必须写 SQL"，
  比只会其中一种要强得多。

【MySQL 8.0 带来的变化（相比本项目最初的 5.7 版本）】
  · 逐日累计、分组 TopN、ABC 累计这类查询不再需要用户变量 @x := ...
  · 同期群、干旱识别这类多层嵌套改用 CTE，可读性质变
  · 审计日志的 JSON 可以用 JSON_TABLE 展开成行（5.7 做不到）
  · CHECK 约束真正生效（见 sql/01_schema.sql）

【一个刻意保留的"非窗口函数"实现】
  同比环比（yield_yoy_mom）**没有**改成 LAG()，因为月度数据有断档，
  而 LAG 数的是行数不是月数 —— 详见该函数内的说明。
  迁移版本不等于把能改的都改成新语法，"判断哪个该改"才是重点。
"""

from ..extensions import raw_query, raw_scalar

# 全项目统一的"有效种植批次"口径。所有分析必须用这一个定义，
# 否则同一个指标在不同页面会算出不同数字。
VALID_STATUS = '20, 30, 40'


# =============================================================================
# SQL 注册表 —— 保证"页面上展示的 SQL" 就是 "实际执行的 SQL"
# =============================================================================
# 【为什么需要这个】
#   分析页要把 SQL 原文展示给用户（这是本项目的核心卖点之一）。
#   天真的做法是在模板或文档里再抄一份 SQL 用于展示 ——
#   那样只要改了查询忘了改副本，页面显示的就不是真实执行的语句，
#   而且这种错误**不会报错**，只会让人在面试现场被问穿。
#
#   这里的做法：所有被展示的查询都通过 q(name, sql) 调用，
#   它把 sql 原文登记进 SQL 字典后再执行。
#   页面显示 SQL['gdd_curve'] —— 与真正跑的是同一个字符串对象，
#   结构上不可能漂移。
SQL = {}


def q(name, sql, params=None):
    """执行查询并登记其 SQL 原文（供页面展示）。"""
    SQL[name] = sql
    return raw_query(sql, params)


# =============================================================================
# 看板数据
# =============================================================================

def dashboard_summary():
    """看板顶部的一组 KPI 数字。"""
    return raw_query("""
        SELECT
            (SELECT COUNT(*) FROM farm)                        AS farm_cnt,
            (SELECT COUNT(*) FROM plot)                        AS plot_cnt,
            (SELECT ROUND(SUM(area_mu), 1) FROM plot)          AS total_area_mu,
            (SELECT COUNT(*) FROM planting WHERE status IN (20,30,40)) AS planting_cnt,
            (SELECT COUNT(*) FROM yield_record)                AS yield_record_cnt,
            (SELECT ROUND(SUM(yield_kg) / 1000, 1) FROM yield_record)  AS total_yield_ton,
            (SELECT ROUND(SUM(output_value) / 10000, 1) FROM yield_record) AS total_value_wan,
            (SELECT ROUND(SUM(yield_kg) / NULLIF(SUM(harvest_area_mu), 0), 1)
               FROM yield_record)                              AS avg_yield_per_mu
    """)[0]


def yield_trend_monthly():
    """月度产量 + 产值趋势（双轴图）。"""
    return raw_query("""
        SELECT harvest_month,
               ROUND(total_yield_kg / 1000, 1)   AS yield_ton,
               ROUND(total_output_value / 10000, 1) AS value_wan,
               ROUND(yield_per_mu, 1)            AS yield_per_mu,
               total_area_mu,
               record_cnt
        FROM v_yield_monthly
        ORDER BY harvest_month
    """)


def crop_output_share():
    """各作物产量与产值占比（饼图 / 玫瑰图）。"""
    return raw_query("""
        SELECT c.name AS crop_name,
               c.category,
               COUNT(*)                                   AS record_cnt,
               ROUND(SUM(y.yield_kg) / 1000, 1)           AS yield_ton,
               ROUND(SUM(y.output_value) / 10000, 1)      AS value_wan,
               ROUND(SUM(y.yield_kg) / NULLIF(SUM(y.harvest_area_mu), 0), 1) AS yield_per_mu
        FROM yield_record y
        JOIN crop c ON c.id = y.crop_id
        GROUP BY c.id, c.name, c.category
        ORDER BY yield_ton DESC
    """)


def region_output():
    """地区产量分布（柱状图 / 地图）。"""
    return raw_query("""
        SELECT f.province, f.city, f.name AS farm_name,
               COUNT(DISTINCT pl.id)                              AS plot_cnt,
               COUNT(*)                                           AS record_cnt,
               ROUND(SUM(y.yield_kg) / 1000, 1)                   AS yield_ton,
               ROUND(SUM(y.output_value) / 10000, 1)              AS value_wan,
               ROUND(SUM(y.yield_kg) / NULLIF(SUM(y.harvest_area_mu), 0), 1) AS yield_per_mu
        FROM yield_record y
        JOIN plot pl ON pl.id = y.plot_id
        JOIN farm f  ON f.id = pl.farm_id
        GROUP BY f.id, f.province, f.city, f.name
        ORDER BY yield_ton DESC
    """)


def top_plots(limit=10):
    """单产最高的地块（横向柱状图）。"""
    return raw_query("""
        SELECT s.plot_no, s.plot_name, s.crop_name, s.farm_name,
               s.fertility_level, s.yield_per_mu, s.record_cnt,
               ROUND(s.total_area_mu, 1) AS total_area_mu
        FROM v_plot_yield_summary s
        WHERE s.record_cnt >= 2
        ORDER BY s.yield_per_mu DESC, s.plot_no
        LIMIT :limit
    """, {'limit': int(limit)})


def cost_structure():
    """成本结构（堆叠柱状图 / 饼图）。"""
    return raw_query("""
        SELECT c.name AS crop_name,
               ROUND(SUM(pc.material_cost) / 10000, 1) AS material_wan,
               ROUND(SUM(pc.labor_cost) / 10000, 1)    AS labor_wan,
               ROUND(SUM(pc.machine_cost) / 10000, 1)  AS machine_wan,
               ROUND(SUM(pc.total_cost) / 10000, 1)    AS total_wan
        FROM v_planting_cost pc
        JOIN planting p ON p.id = pc.planting_id
        JOIN crop c ON c.id = p.crop_id
        JOIN yield_record y ON y.planting_id = pc.planting_id
        GROUP BY c.id, c.name
        ORDER BY total_wan DESC
    """)


# =============================================================================
# 分析 1：有效积温 (GDD) 累计曲线
# =============================================================================

def gdd_curve(planting_id):
    """
    某批次的逐日有效积温累计曲线（折线图）。

    【8.0 写法】一行 SUM() OVER (ORDER BY obs_date) 即可。
    对比 5.7 版本：需要用户变量 @gdd/@pid 逐行累加，
    还得加 LIMIT 18446744073709551615 防止 derived_merge 吃掉 ORDER BY。
    窗口函数把这一整类坑都消除了。

    ⚠️ 但 `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` 必须显式写。
       窗口函数默认帧是 RANGE，它把 ORDER BY 值**相同的行算作同一帧**；
       对累计求和来说，有并列值时两者结果不同。显式写 ROWS 表达的才是
       "严格逐行累加"这个意图。
    """
    return q('gdd_curve', """
        SELECT
            w.obs_date,
            ROUND(GREATEST(0, (w.temp_max + w.temp_min) / 2 - c.base_temp), 2) AS gdd_daily,
            ROUND(SUM(GREATEST(0, (w.temp_max + w.temp_min) / 2 - c.base_temp))
                      OVER (ORDER BY w.obs_date
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2) AS gdd_cum
        FROM planting p
        JOIN plot          pl ON pl.id = p.plot_id
        JOIN farm          f  ON f.id  = pl.farm_id
        JOIN crop          c  ON c.id  = p.crop_id
        JOIN weather_daily w  ON w.region_code = f.region_code
                             AND w.obs_date BETWEEN p.sow_date AND p.mature_date
        WHERE p.id = :pid
        ORDER BY w.obs_date
    """, {'pid': int(planting_id)})


def gdd_maturity_list(limit=300):
    """
    各批次的积温满足度（散点图 / 表格）。

    这条**不需要窗口函数** —— 只求整个生育期的总积温，纯聚合即可。
    能用简单方法时不要硬套新特性，判断力本身比技巧重要。
    """
    return q('gdd_maturity', """
        SELECT d.batch_no, d.plot_no, d.crop_name, d.province, d.season,
               d.sow_date, d.mature_date, d.harvest_date,
               d.gdd_maturity, d.plant_area_mu,
               ROUND(g.gdd_actual, 0)                                      AS gdd_actual,
               ROUND(g.gdd_actual / NULLIF(d.gdd_maturity, 0) * 100, 1)    AS maturity_pct,
               CASE
                   WHEN g.gdd_actual >= d.gdd_maturity            THEN '积温充足'
                   WHEN g.gdd_actual >= d.gdd_maturity * 0.90     THEN '基本满足'
                   WHEN g.gdd_actual >= d.gdd_maturity * 0.80     THEN '积温偏少'
                   ELSE '积温严重不足'
               END                                                         AS gdd_status
        FROM (
            SELECT p.id AS planting_id,
                   SUM(GREATEST(0, (w.temp_max + w.temp_min) / 2 - c.base_temp)) AS gdd_actual
            FROM planting p
            JOIN plot pl ON pl.id = p.plot_id
            JOIN farm f  ON f.id  = pl.farm_id
            JOIN crop c  ON c.id  = p.crop_id
            JOIN weather_daily w ON w.region_code = f.region_code
                                AND w.obs_date BETWEEN p.sow_date AND p.mature_date
            GROUP BY p.id
        ) g
        JOIN v_planting_detail d ON d.planting_id = g.planting_id
        WHERE d.status IN (20, 30, 40)
        ORDER BY maturity_pct ASC
        LIMIT :limit
    """, {'limit': int(limit)})


# =============================================================================
# 分析 2：产量同比 (YoY) + 环比 (MoM)
# =============================================================================

def yield_yoy_mom():
    """
    月度同比环比。

    ⚠️⚠️ 这条查询**刻意没有**改成窗口函数的 LAG()，这是一个必须判断的地方。

    很多人迁移到 8.0 时会顺手把自连接换成：
        LAG(total_yield_kg, 12) OVER (ORDER BY harvest_month) AS last_year
    看起来等价，**实际上是错的**。

    【为什么错】LAG(x, N) 数的是「往前 N 行」，不是「往前 N 个月」。
    本项目的月度数据**有断档**（实测缺失 2023-02、2024-03 两个月，
    那两个月没有作物收获，v_yield_monthly 里就没有对应行）。
    一旦有断档，第 12 行就不再是"去年同月"。

    【实测证据】在本项目的真实数据上，两种写法有 18 个月结果不同：
        月份      行号   LAG(x,12)          自连接(正确)
        2023-06    12    NULL              1648.5  (2022-06)
        2023-07    13    1648.5 (2022-06)  6337.0  (2022-07)  ← 差了一个月
        2023-08    14    6337.0 (2022-07)  3305.8  (2022-08)  ← 一直错位
    而且**不会报任何错**。sql/05_tests.sql 的断言 8 把这个差异固化了下来。

    【正确做法】自连接 `DATE_SUB(month, INTERVAL 1 YEAR)` ——
    它按**日期值**匹配，与行号无关，断档也不影响。

    结论：迁移版本不等于"把能改的都改成新语法"，
    用之前要确认新写法的语义和你需要的**一致**。
    """
    return q('yoy_mom', """
        SELECT
            cur.harvest_month                                   AS month,
            ROUND(cur.total_yield_kg / 1000, 1)                 AS yield_ton,
            ROUND(prev.total_yield_kg / 1000, 1)                AS prev_ton,
            ROUND(cur.total_yield_kg / NULLIF(prev.total_yield_kg, 0) * 100 - 100, 2)
                                                                AS mom_pct,
            ROUND(yoy.total_yield_kg / 1000, 1)                 AS yoy_ton,
            ROUND(cur.total_yield_kg / NULLIF(yoy.total_yield_kg, 0) * 100 - 100, 2)
                                                                AS yoy_pct,
            ROUND(cur.total_output_value / 10000, 1)            AS value_wan,
            cur.yield_per_mu
        FROM v_yield_monthly cur
        LEFT JOIN v_yield_monthly prev
               ON prev.harvest_month = DATE_SUB(cur.harvest_month, INTERVAL 1 MONTH)
        LEFT JOIN v_yield_monthly yoy
               ON yoy.harvest_month  = DATE_SUB(cur.harvest_month, INTERVAL 1 YEAR)
        ORDER BY cur.harvest_month
    """)


def lag_vs_selfjoin_demo():
    """
    演示 "自连接 vs LAG(x,12)" 在同一份数据上的差异。

    这条查询的意义在于**把结论变成可复现的证据**：
    页面上直接列出两种写法的结果和"是否一致"，
    读者不用相信我的说法，自己看数据即可。
    """
    return q('lag_demo', """
        WITH m AS (
            SELECT harvest_month, total_yield_kg,
                   LAG(total_yield_kg, 12) OVER (ORDER BY harvest_month) AS lag12
            FROM v_yield_monthly
        )
        SELECT
            m.harvest_month                                     AS month,
            ROUND(m.total_yield_kg / 1000, 1)                   AS this_month,
            ROUND(m.lag12 / 1000, 1)                            AS lag12_value,
            ROUND(sj.total_yield_kg / 1000, 1)                  AS selfjoin_value,
            CASE WHEN m.lag12 <=> sj.total_yield_kg
                 THEN '一致' ELSE '不一致' END                   AS is_match
        FROM m
        LEFT JOIN v_yield_monthly sj
               ON sj.harvest_month = DATE_SUB(m.harvest_month, INTERVAL 1 YEAR)
        ORDER BY m.harvest_month
    """)


# =============================================================================
# 分析 3：单产影响因子的皮尔逊相关系数
# =============================================================================

# MySQL **没有任何**相关/回归聚合函数（实测确认 8.0.46）：
#     CORR / COVAR_POP / COVAR_SAMP / REGR_SLOPE / REGR_R2 全都不存在
# 所以只能手写。这里给出两条数学路径，互为验证。
_PEARSON_EXPANDED = """
    (COUNT(*) * SUM({x} * {y}) - SUM({x}) * SUM({y}))
    / NULLIF(SQRT(
        (COUNT(*) * SUM({x} * {x}) - SUM({x}) * SUM({x}))
      * (COUNT(*) * SUM({y} * {y}) - SUM({y}) * SUM({y}))
    ), 0)
"""

# 路径②：定义式 —— 协方差 / (标准差 × 标准差)
# 用 MySQL 内置的 STDDEV_POP，数学来源与展开式完全不同
_PEARSON_DEFINITION = """
    ((SUM({x} * {y}) - SUM({x}) * SUM({y}) / COUNT(*)) / COUNT(*))
    / NULLIF(STDDEV_POP({x}) * STDDEV_POP({y}), 0)
"""

# 施肥量必须用 SUM(用量)/该批次面积 反算，不能直接 SUM(用量)：
# 不同地块面积差几倍，直接用总量会把"面积大"误判成"施肥多"。
_FERT_SUBQ = """
    SELECT i.planting_id,
           SUM(i.quantity) / NULLIF(MAX(p.plant_area_mu), 0) AS fert_per_mu
    FROM input_cost i
    JOIN planting p ON p.id = i.planting_id
    WHERE i.input_type = 20
    GROUP BY i.planting_id
"""


def fertilizer_correlation_by_crop():
    """
    分作物的"施肥量—单产"相关系数。

    【为什么必须分作物算】
    跨作物汇总相关系数没有意义：番茄单产 5000+ kg/亩、大豆 200 kg/亩，
    而两者施肥量范围差不多。混在一起算，作物间的**量级差异**会主导结果，
    把施肥的真实影响淹没（辛普森悖论的另一种表现）。

    【8.0 的改进：两条数学路径交叉验证】
    手写公式写错的概率不低，而算错的 r 仍然是一个"看起来合理的小数"，
    不会报错。所以这里同时用**展开式**和**协方差/标准差定义式**各算一遍，
    两者数学来源完全不同，结果必须吻合。
    """
    return q('corr_by_crop', f"""
        SELECT c.name AS crop_name,
               COUNT(*)                        AS sample_n,
               ROUND(AVG(y.yield_per_mu), 1)   AS avg_yield,
               ROUND(AVG(f.fert_per_mu), 1)    AS avg_fert,
               ROUND({_PEARSON_EXPANDED.format(x='f.fert_per_mu', y='y.yield_per_mu')}, 3)
                                               AS pearson_r,
               ROUND({_PEARSON_DEFINITION.format(x='f.fert_per_mu', y='y.yield_per_mu')}, 3)
                                               AS pearson_r_alt,
               IF(ABS(
                   {_PEARSON_EXPANDED.format(x='f.fert_per_mu', y='y.yield_per_mu')}
                 - {_PEARSON_DEFINITION.format(x='f.fert_per_mu', y='y.yield_per_mu')}
               ) < 0.0001, '一致', '不一致')    AS formula_check
        FROM yield_record y
        JOIN crop c ON c.id = y.crop_id
        JOIN ({_FERT_SUBQ}) f ON f.planting_id = y.planting_id
        GROUP BY c.id, c.name
        HAVING sample_n >= 5
        ORDER BY pearson_r DESC
    """)


def fertilizer_correlation_pooled():
    """
    跨作物的汇总相关系数：用**相对单产**消除量级差异。

    分作物算时每个作物只有 6~50 个样本，小样本的相关系数抽样波动可达 ±0.3。
    把各作物单产除以该作物平均单产得到"相对单产"，就能把全部记录放在一起算，
    样本量充足、结果稳定。这也是农学研究的常规做法。
    """
    return q('corr_pooled', f"""
        SELECT COUNT(*) AS sample_n,
               ROUND({_PEARSON_EXPANDED.format(x='x.fert_per_mu', y='x.rel_yield')}, 3)
                       AS pearson_r,
               ROUND({_PEARSON_DEFINITION.format(x='x.fert_per_mu', y='x.rel_yield')}, 3)
                       AS pearson_r_alt
        FROM (
            SELECT f.fert_per_mu,
                   y.yield_per_mu / NULLIF(ca.avg_ypm, 0) AS rel_yield
            FROM yield_record y
            JOIN ({_FERT_SUBQ}) f ON f.planting_id = y.planting_id
            JOIN (SELECT crop_id, AVG(yield_per_mu) AS avg_ypm
                  FROM yield_record GROUP BY crop_id) ca ON ca.crop_id = y.crop_id
        ) x
    """)[0]


def yield_scatter(crop_id, limit=200):
    """施肥量 vs 单产的散点图数据（单作物）。"""
    return raw_query(f"""
        SELECT y.id,
               ROUND(f.fert_per_mu, 1)  AS fert_per_mu,
               ROUND(y.yield_per_mu, 1) AS yield_per_mu,
               d.plot_no, d.province, d.season,
               d.fertility_level
        FROM yield_record y
        JOIN ({_FERT_SUBQ}) f ON f.planting_id = y.planting_id
        JOIN v_yield_detail d ON d.yield_id = y.id
        WHERE y.crop_id = :cid
        ORDER BY fert_per_mu
        LIMIT :limit
    """, {'cid': int(crop_id), 'limit': int(limit)})


# =============================================================================
# 分析 4：投入产出比 (ROI)
# =============================================================================

def roi_ranking(limit=50):
    """
    地块级投入产出比排行。

    8.0 新增了一列"同作物内排名" —— 用 RANK() 窗口函数算，
    这是 5.7 里要用用户变量模拟的事情。
    """
    return q('roi_ranking', """
        SELECT d.season, d.plot_no, d.crop_name, d.province, d.farm_name,
               ROUND(c.total_cost, 0)                             AS total_cost,
               ROUND(d.output_value, 0)                           AS output_value,
               ROUND(d.output_value - c.total_cost, 0)            AS profit,
               ROUND(d.output_value / NULLIF(c.total_cost, 0), 2) AS roi_ratio,
               ROUND((d.output_value - c.total_cost) / NULLIF(c.total_cost, 0) * 100, 1)
                                                                  AS roi_pct,
               ROUND(d.yield_per_mu, 1)                           AS yield_per_mu,
               ROUND(c.material_cost / NULLIF(c.total_cost, 0) * 100, 1) AS material_pct,
               ROUND(c.labor_cost    / NULLIF(c.total_cost, 0) * 100, 1) AS labor_pct,
               ROUND(c.machine_cost  / NULLIF(c.total_cost, 0) * 100, 1) AS machine_pct,
               ROUND(c.total_cost / NULLIF(d.harvest_area_mu, 0), 0)     AS cost_per_mu,
               -- 窗口函数：该批次在**同作物内**的投入产出比排名
               RANK() OVER (PARTITION BY d.crop_id
                            ORDER BY d.output_value / NULLIF(c.total_cost, 0) DESC)
                                                                  AS rank_in_crop
        FROM v_yield_detail d
        JOIN v_planting_cost c ON c.planting_id = d.planting_id
        WHERE c.total_cost > 0
        ORDER BY roi_ratio DESC
        LIMIT :limit
    """, {'limit': int(limit)})


def roi_by_crop():
    """按作物汇总的投入产出比。"""
    return q('roi_by_crop', """
        SELECT d.crop_name,
               COUNT(*)                                       AS batch_cnt,
               ROUND(SUM(c.total_cost) / 10000, 1)            AS cost_wan,
               ROUND(SUM(d.output_value) / 10000, 1)          AS value_wan,
               ROUND((SUM(d.output_value) - SUM(c.total_cost))
                     / NULLIF(SUM(c.total_cost), 0) * 100, 1)  AS roi_pct,
               ROUND(SUM(d.output_value) / NULLIF(SUM(c.total_cost), 0), 2) AS roi_ratio,
               ROUND(SUM(c.total_cost) / NULLIF(SUM(d.harvest_area_mu), 0), 0) AS cost_per_mu,
               ROUND(SUM(d.output_value) / NULLIF(SUM(d.harvest_area_mu), 0), 0) AS value_per_mu
        FROM v_yield_detail d
        JOIN v_planting_cost c ON c.planting_id = d.planting_id
        GROUP BY d.crop_id, d.crop_name
        HAVING batch_cnt >= 5
        ORDER BY roi_ratio DESC
    """)


# =============================================================================
# 分析 5：分组 Top N
# =============================================================================

def topn_by_crop(top_n=3):
    """
    各作物单产前 N 的地块。

    【8.0 写法】一行 ROW_NUMBER() OVER (PARTITION BY crop_id ORDER BY ...)，
    对比 5.7 需要用户变量 @rn/@grp 模拟 + LIMIT 强制物化。

    ⚠️ 但窗口函数**没有**替你解决破平局问题：若两行单产相同，
       ROW_NUMBER 给出的顺序依赖执行计划。所以末尾的 plot_no 第二排序键
       仍然必须写 —— 窗口函数不改变数据本身的模糊性。
    """
    return q('topn', """
        SELECT t.crop_name, t.rn, t.plot_no, t.farm_name, t.fertility_name,
               t.yield_per_mu, t.record_cnt, t.total_area_mu
        FROM (
            SELECT
                crop_name, plot_no, farm_name, yield_per_mu,
                record_cnt, total_area_mu,
                CASE fertility_level WHEN 1 THEN '优' WHEN 2 THEN '中' ELSE '差' END
                    AS fertility_name,
                ROW_NUMBER() OVER (PARTITION BY crop_id
                                   ORDER BY yield_per_mu DESC, plot_no ASC) AS rn
            FROM v_plot_yield_summary
        ) t
        WHERE t.rn <= :n
        ORDER BY t.crop_name, t.rn
    """, {'n': int(top_n)})


def topn_by_crop_deterministic(top_n=3):
    """
    分组 TopN 的**确定性实现**（相关子查询，结果永远正确）。
    用来交叉验证上面那份，也是面试时可以拿出来对比的第二种解法。
    """
    return q('topn_deterministic', """
        SELECT s.crop_name, s.plot_no, s.yield_per_mu
        FROM v_plot_yield_summary s
        WHERE (
            SELECT COUNT(*) FROM v_plot_yield_summary s2
            WHERE s2.crop_id = s.crop_id
              AND (s2.yield_per_mu > s.yield_per_mu
                   OR (s2.yield_per_mu = s.yield_per_mu AND s2.plot_no < s.plot_no))
        ) < :n
        ORDER BY s.crop_name, s.yield_per_mu DESC
    """, {'n': int(top_n)})


def rank_functions_demo(limit=15):
    """
    演示 ROW_NUMBER / RANK / DENSE_RANK 三者的区别 —— 面试常考。

        ROW_NUMBER()  1 2 3 4   并列也强行排出先后（必定唯一）
        RANK()        1 2 2 4   并列同名次，后续名次跳过
        DENSE_RANK()  1 2 2 3   并列同名次，后续名次不跳过

    业务含义：
        ROW_NUMBER  → "每组只能取 N 个名额"（如推荐 Top3 地块）
        RANK        → "排名第几"（并列第 2，下一个是第 4）
        DENSE_RANK  → "分几档"（并列第 2，下一个是第 3）
    """
    return q('rank_demo', """
        SELECT t.plot_no, t.crop_name, t.record_cnt,
               ROW_NUMBER() OVER (ORDER BY t.record_cnt DESC, t.plot_no) AS rn,
               RANK()       OVER (ORDER BY t.record_cnt DESC)            AS rnk,
               DENSE_RANK() OVER (ORDER BY t.record_cnt DESC)            AS dense_rnk,
               COUNT(*)     OVER (PARTITION BY t.record_cnt)             AS ties
        FROM v_plot_yield_summary t
        ORDER BY t.record_cnt DESC, t.plot_no
        LIMIT :limit
    """, {'limit': int(limit)})


# =============================================================================
# 分析 6：ABC 帕累托
# =============================================================================

# 累计求和 + 占比，用窗口函数。
# ⚠️ 必须显式写 ROWS 帧（默认 RANGE 会把并列值算作同一帧）。
_ABC_WINDOW = """
    SUM(total_yield_kg) OVER w
    / NULLIF(SUM(total_yield_kg) OVER (), 0)
"""
_ABC_WINDOW_DEF = """
    WINDOW w AS (
        ORDER BY total_yield_kg DESC, plot_id ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
"""


def abc_analysis(limit=40):
    """地块产量 ABC 分类。"""
    return q('abc', f"""
        SELECT t.plot_no, t.crop_name,
               ROUND(t.total_yield_kg, 0)                       AS yield_kg,
               ROUND(SUM(t.total_yield_kg) OVER w, 0)           AS cum_yield,
               ROUND({_ABC_WINDOW} * 100, 2)                    AS cum_pct,
               CASE
                   WHEN {_ABC_WINDOW} <= 0.70 THEN 'A'
                   WHEN {_ABC_WINDOW} <= 0.90 THEN 'B'
                   ELSE 'C'
               END                                              AS abc_class
        FROM v_plot_yield_summary t
        {_ABC_WINDOW_DEF}
        ORDER BY t.total_yield_kg DESC, t.plot_id ASC
        LIMIT :limit
    """, {'limit': int(limit)})


def abc_summary():
    """ABC 分类汇总（三类各多少地块、贡献多少产量）。"""
    return q('abc_summary', f"""
        SELECT abc.abc_class,
               COUNT(*)             AS plot_cnt,
               ROUND(COUNT(*) / (SELECT COUNT(*) FROM v_plot_yield_summary) * 100, 1)
                                    AS plot_pct,
               ROUND(SUM(abc.yield_kg), 0) AS yield_kg,
               ROUND(SUM(abc.yield_kg) / (SELECT SUM(total_yield_kg)
                                          FROM v_plot_yield_summary) * 100, 1)
                                    AS yield_pct
        FROM (
            SELECT t.total_yield_kg AS yield_kg,
                   CASE
                       WHEN {_ABC_WINDOW} <= 0.70 THEN 'A'
                       WHEN {_ABC_WINDOW} <= 0.90 THEN 'B'
                       ELSE 'C'
                   END AS abc_class
            FROM v_plot_yield_summary t
            {_ABC_WINDOW_DEF}
        ) abc
        GROUP BY abc.abc_class
        ORDER BY abc.abc_class
    """)


# =============================================================================
# 分析 7：地块连续种植季同期群 (Cohort)  ——  CTE 改写
# =============================================================================

def cohort_retention():
    """
    按地块首次种植茬口分群，看后续各茬口还在种的比例。

    【8.0 的 CTE 让这条查询的可读性发生了质变】
    5.7 版本为了表达"首季 → 各季活动 → 群规模"三层关系，
    用了三个嵌套派生表 + 自连接，SQL 长达 40 行且很难读懂谁连谁。
    用 CTE 之后每一步都是有名字、自包含的结果集，主查询只负责把它们拼起来。

    季序 = 0 的那一列留存率必然 100%（首季本身），这是天然的自检断言。
    """
    return q('cohort', f"""
        WITH valid AS (
            -- 有效种植批次的统一口径，只在这里定义一次
            SELECT plot_id, season_seq, season_year, season
            FROM planting
            WHERE status IN ({VALID_STATUS})
        ),
        cohort AS (
            SELECT plot_id, MIN(season_seq) AS cohort_seq
            FROM valid GROUP BY plot_id
        ),
        cohort_size AS (
            SELECT cohort_seq, COUNT(*) AS cohort_size
            FROM cohort GROUP BY cohort_seq
        ),
        activity AS (
            SELECT DISTINCT plot_id, season_seq FROM valid
        ),
        label AS (
            SELECT DISTINCT season_seq,
                   CONCAT(season_year, season) AS cohort_label
            FROM valid
        )
        SELECT c.cohort_seq, l.cohort_label, cs.cohort_size,
               (a.season_seq - c.cohort_seq) AS season_idx,
               COUNT(DISTINCT a.plot_id)     AS retained,
               ROUND(COUNT(DISTINCT a.plot_id) / NULLIF(cs.cohort_size, 0) * 100, 1)
                                             AS retention_pct
        FROM cohort c
        JOIN activity    a  ON a.plot_id     = c.plot_id
        JOIN cohort_size cs ON cs.cohort_seq = c.cohort_seq
        JOIN label       l  ON l.season_seq  = c.cohort_seq
        GROUP BY c.cohort_seq, l.cohort_label, cs.cohort_size, a.season_seq
        ORDER BY c.cohort_seq, season_idx
    """)


# =============================================================================
# 分析 8：连续干旱日数预警（gaps-and-islands）
# =============================================================================

def drought_events(min_days=10, limit=40):
    """
    各气象区持续时间最长的干旱过程。

    【8.0 相比 5.7 的真正改进】
    5.7 版需要 @rn8 和 @grp8 两个用户变量，且必须手写
        @grp8 := s.region_code     ← 漏掉这行，行号每行都重置，
                                     所有"连续段"变成单天，查询返回 0 行
    而 ROW_NUMBER() OVER (PARTITION BY region_code ...) **自带分区语义**，
    结构上不可能漏写"记住上一行分组"的赋值。

    ⚠️ 但有一个坑两个版本都有、必须靠领域知识解决：
        **必须排除冬季封冻期**（temp_avg >= 5）。
        最初这条查询没有温度条件，结果黑龙江查出来的"最长干旱"是
        1~2 月的 53 天、期间均温 −14.9℃ —— 那不是旱情，是冬季封冻。
        农业干旱的定义前提是"作物处于生长状态且水分亏缺"。
        这是纯技术视角想不到的一层。
    """
    return q('drought', """
        WITH dry AS (
            SELECT
                region_code, obs_date, temp_avg,
                -- 线性日序号 − 分区内行号 = 连续段标识
                TO_DAYS(obs_date)
                  - ROW_NUMBER() OVER (PARTITION BY region_code ORDER BY obs_date)
                    AS island
            FROM weather_daily
            WHERE precipitation < 1.0
              AND temp_avg >= 5.0
        )
        SELECT
            d.region_code, MIN(d.obs_date) AS start_date, MAX(d.obs_date) AS end_date,
            COUNT(*) AS dry_days, ROUND(AVG(d.temp_avg), 1) AS avg_temp,
            CASE
                WHEN MONTH(MIN(d.obs_date)) BETWEEN 3 AND 5  THEN '春季（播种期）'
                WHEN MONTH(MIN(d.obs_date)) BETWEEN 6 AND 8  THEN '夏季（生长关键期）'
                WHEN MONTH(MIN(d.obs_date)) BETWEEN 9 AND 11 THEN '秋季（灌浆/收获期）'
                ELSE '冬季（影响较小）'
            END AS impact_season
        FROM dry d
        GROUP BY d.region_code, d.island
        HAVING COUNT(*) >= :min_days
        ORDER BY dry_days DESC, start_date
        LIMIT :limit
    """, {'min_days': int(min_days), 'limit': int(limit)})


def monthly_weather(region_code, year):
    """某气象区某年的月度气温与降水（气象页图表）。"""
    return raw_query("""
        SELECT obs_month,
               ROUND(AVG(temp_avg), 1)   AS avg_temp,
               ROUND(MAX(temp_max), 1)   AS max_temp,
               ROUND(MIN(temp_min), 1)   AS min_temp,
               ROUND(SUM(precipitation), 1) AS precipitation,
               ROUND(SUM(GREATEST(0, (temp_max + temp_min) / 2 - :base_temp)), 0) AS gdd
        FROM weather_daily
        WHERE region_code = :region AND YEAR(obs_date) = :year
        GROUP BY obs_month
        ORDER BY obs_month
    """, {'region': region_code, 'year': int(year), 'base_temp': 10.0})


# =============================================================================
# 地图
# =============================================================================

def map_plot_metrics():
    """
    地图着色数据：每个地块的单产、产值、地力。

    这里可以做一次 8.0 的优化：用窗口函数取"每个地块最近一次收获"
    会比 GROUP BY 全部历史更贴合"这块地现在种得怎么样"的语义。
    """
    return raw_query("""
        SELECT s.plot_no,
               s.crop_name,
               s.yield_per_mu,
               s.output_per_mu,
               s.fertility_level,
               s.record_cnt,
               ROUND(s.total_area_mu, 1) AS total_area_mu
        FROM v_plot_yield_summary s
    """)


# =============================================================================
# MySQL 8.0 独有：JSON_TABLE 审计分析
# =============================================================================

_AUDIT_JSON_JOIN = """
    JOIN JSON_TABLE(
             JSON_KEYS(a.detail, '$.before'), '$[*]'
             COLUMNS (`field_name` VARCHAR(64) PATH '$')
         ) AS jt
"""

# ⚠️ 这里路径是动态拼接的，所以不能用 ->> 语法糖（它只接受字符串字面量），
#    必须写完整的 JSON_EXTRACT + JSON_UNQUOTE。
_AUDIT_BEFORE = "JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.before.', jt.field_name)))"
_AUDIT_AFTER = "JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.after.',  jt.field_name)))"


def audit_field_changes(limit=100):
    """
    【MySQL 8.0 独有】把审计日志的 JSON 变更快照展开成"字段级"的行。

    5.7 有 JSON 类型和 JSON_EXTRACT，但**没有 JSON_TABLE**，
    无法把 JSON 里的键展开成行 —— 只能整块读出来给应用层解析。
    所以 5.7 时代的设计原则是"JSON 只当不可查询的附件"，
    需要检索的字段必须抽成独立列（本项目用生成列 changed_fields 做了这件事）。

    8.0 有了 JSON_TABLE 之后，这个限制被打破：
    "这次修改动了哪几个字段、每个字段从什么变成了什么"变成了一条普通 SELECT。
    """
    return q('audit_field_changes', f"""
        SELECT
            a.id                                        AS log_id,
            a.created_at,
            u.real_name                                 AS operator,
            a.action,
            a.target_table,
            a.target_id,
            jt.field_name,
            {_AUDIT_BEFORE}                             AS old_value,
            {_AUDIT_AFTER}                              AS new_value,
            CASE
                WHEN ({_AUDIT_BEFORE}) <=> ({_AUDIT_AFTER}) THEN '未变化'
                ELSE '已变更'
            END                                         AS really_changed,
            a.ip
        FROM audit_log a
        LEFT JOIN `user` u ON u.id = a.user_id
        {_AUDIT_JSON_JOIN}
        WHERE a.detail IS NOT NULL AND JSON_VALID(a.detail)
        ORDER BY a.id DESC, jt.field_name
        LIMIT :limit
    """, {'limit': int(limit)})


def audit_hot_fields(limit=30):
    """
    【MySQL 8.0 独有】统计"哪些字段最常被修改"。

    这是 5.7 时代**做不到**的分析 —— 需要先把 JSON 展开成行才能分组计数。
    业务价值：某个字段被频繁修改，往往说明录入环节或业务规则有问题。
    """
    return q('audit_hot_fields', f"""
        WITH field_changes AS (
            SELECT a.target_table, jt.field_name
            FROM audit_log a
            {_AUDIT_JSON_JOIN}
            WHERE a.detail IS NOT NULL
              AND NOT (({_AUDIT_BEFORE}) <=> ({_AUDIT_AFTER}))
        )
        SELECT
            target_table,
            field_name,
            COUNT(*)                                            AS change_cnt,
            RANK() OVER (PARTITION BY target_table
                         ORDER BY COUNT(*) DESC, field_name)    AS rank_in_table,
            ROUND(COUNT(*) * 100.0
                  / SUM(COUNT(*)) OVER (PARTITION BY target_table), 1) AS pct
        FROM field_changes
        GROUP BY target_table, field_name
        ORDER BY target_table, change_cnt DESC
        LIMIT :limit
    """, {'limit': int(limit)})


def audit_json_stats():
    """审计日志里有多少条是可解析的 JSON（JSON_TABLE 的前提）。"""
    return raw_query("""
        SELECT COUNT(*)                                              AS total,
               SUM(CASE WHEN detail IS NOT NULL AND JSON_VALID(detail)
                        THEN 1 ELSE 0 END)                           AS parseable,
               SUM(CASE WHEN detail IS NOT NULL AND JSON_VALID(detail)
                         AND JSON_KEYS(detail, '$.before') IS NOT NULL
                        THEN 1 ELSE 0 END)                           AS has_before
        FROM audit_log
    """)[0]
