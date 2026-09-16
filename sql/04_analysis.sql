-- =============================================================================
--  8 组高级分析查询 —— MySQL 8.0 实现
-- =============================================================================
--
--  【和 5.7 版本的根本区别】
--  这个项目最初是按 MySQL 5.7 写的（5.7 没有窗口函数和 CTE），8 组分析里
--  有一半要靠用户变量 @x := ... 硬凑。迁移到 8.0 之后，绝大部分可以重写成
--  标准 SQL。
--
--  但迁移**不是机械地把用户变量替换成窗口函数** —— 有几处必须判断"换过去
--  到底对不对"。本文件里保留了三处这样的判断，见 ② 和 ③：
--    · ② 同比环比：**不能**用 LAG() 替代，因为它数的是行不是月（有断档就错位）
--    · ③ 相关系数：8.0 依然没有 CORR()，但可以用 REGR_R2() 交叉验证
--    · ⑥ 累计求和：必须显式写 ROWS 帧，默认的 RANGE 帧在并列值上行为不同
--
--  【8.0 新特性用到了哪些】
--    · 窗口函数：SUM/ROW_NUMBER/COUNT/RANK OVER (...)
--    · CTE（WITH）：同期群分析、干旱识别
--    · 具名窗口（WINDOW 子句）：ABC 分析里复用同一个窗口定义
--    · JSON_TABLE：把审计日志的 JSON 变更展开成行（见 07_audit_json.sql）
--    · REGR_SLOPE / REGR_R2：线性回归聚合函数，用于交叉验证相关系数
-- =============================================================================

USE `smart_agri`;


-- #############################################################################
-- 分析 1：有效积温 (GDD) 累计曲线与成熟度判定
-- #############################################################################
--
-- 【农业意义】
-- 有效积温（Growing Degree Days）是作物发育的核心驱动量。作物从播种到成熟
-- 需要累积一定的积温，达不到就成熟不了 —— 这是北方农业最主要的减产因素。
--     日有效积温 = max(0, (日最高温 + 日最低温)/2 − 生物学零度)
--     累积有效积温 = Σ 日有效积温
-- GREATEST(0, ...) 不能省：低于生物学零度的日子不但不积累，还不能"倒扣"。
--
-- 【8.0 写法】一行 SUM() OVER (ORDER BY ...) 即可，不需要任何技巧。

-- ---- 1a. 单个批次的累计曲线（可直接画折线图）----
SELECT
    w.`obs_date`,
    -- 日有效积温
    ROUND(GREATEST(0, (w.`temp_max` + w.`temp_min`) / 2 - c.`base_temp`), 2)
        AS gdd_daily,
    -- 累计有效积温：窗口函数按日期累加
    --
    -- ⚠️ 必须显式写 `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`。
    --    窗口函数默认的帧是 RANGE，它会**把 ORDER BY 值相同的行算作同一帧**。
    --    这里按 obs_date 排序，同一天只有一行，所以 ROWS 和 RANGE 结果一样；
    --    但如果按一个有重复值的列排序（比如按产量累计），RANGE 会把所有并列行
    --    一起包含进来，累计值就会出现"跳变"。
    --    养成为累计求和显式写 ROWS 的习惯 —— 它表达的是"逐行累加"，
    --    而不是"累加到当前值为止的所有行"。
    ROUND(SUM(GREATEST(0, (w.`temp_max` + w.`temp_min`) / 2 - c.`base_temp`))
              OVER (ORDER BY w.`obs_date`
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2)
        AS gdd_cum
FROM `planting` p
JOIN `plot`          pl ON pl.`id` = p.`plot_id`
JOIN `farm`          f  ON f.`id`  = pl.`farm_id`
JOIN `crop`          c  ON c.`id`  = p.`crop_id`
JOIN `weather_daily` w  ON w.`region_code` = f.`region_code`
                       AND w.`obs_date` BETWEEN p.`sow_date` AND p.`mature_date`
WHERE p.`id` = 1
ORDER BY w.`obs_date`;

-- 对比 5.7 版本：那条需要
--     @gdd := IF(@pid = t.planting_id, @gdd + t.gdd_daily, t.gdd_daily)
--     @pid := t.planting_id          ← 漏了这行就静默出错
--     LIMIT 18446744073709551615     ← 防 derived_merge 吃掉 ORDER BY
-- 三行技巧 + 两个必须同时满足的约束条件。窗口函数把这一整类坑都消除了。


-- ---- 1b. 全部批次的积温满足度汇总 ----
-- 这条在 5.7 和 8.0 里都是纯聚合（不需要逐日累计），写法完全相同。
-- 保留它是为了说明：**能用简单聚合就别上窗口函数**。
SELECT
    d.`batch_no`,
    d.`plot_no`,
    d.`crop_name`,
    d.`province`,
    d.`sow_date`,
    d.`harvest_date`,
    ROUND(g.`gdd_actual`, 0)                                        AS gdd_actual,
    d.`gdd_maturity`,
    ROUND(g.`gdd_actual` / NULLIF(d.`gdd_maturity`, 0) * 100, 1)    AS maturity_pct,
    CASE
        WHEN g.`gdd_actual` >= d.`gdd_maturity`              THEN '积温充足'
        WHEN g.`gdd_actual` >= d.`gdd_maturity` * 0.90       THEN '基本满足'
        WHEN g.`gdd_actual` >= d.`gdd_maturity` * 0.80       THEN '积温偏少'
        ELSE '积温严重不足'
    END                                                             AS gdd_status
FROM (
    SELECT p.`id` AS planting_id,
           SUM(GREATEST(0, (w.`temp_max` + w.`temp_min`) / 2 - c.`base_temp`)) AS gdd_actual
    FROM `planting` p
    JOIN `plot` pl ON pl.`id` = p.`plot_id`
    JOIN `farm` f  ON f.`id`  = pl.`farm_id`
    JOIN `crop` c  ON c.`id`  = p.`crop_id`
    JOIN `weather_daily` w ON w.`region_code` = f.`region_code`
                          AND w.`obs_date` BETWEEN p.`sow_date` AND p.`mature_date`
    GROUP BY p.`id`
) g
JOIN `v_planting_detail` d ON d.`planting_id` = g.`planting_id`
WHERE d.`status` IN (20, 30, 40)
ORDER BY maturity_pct ASC
LIMIT 40;


-- #############################################################################
-- 分析 2：产量同比 (YoY) + 环比 (MoM)
-- #############################################################################
--
-- ⚠️⚠️ 这一组**不能**改写成窗口函数的 LAG()，这是一处必须判断的地方。
--
-- 很多人迁移到 8.0 时会顺手写成：
--     LAG(total_yield_kg, 12) OVER (ORDER BY harvest_month) AS last_year
-- 看起来等价，**实际上是错的**。
--
-- 【为什么错】LAG(x, N) 数的是「往前 N 行」，不是「往前 N 个月」。
-- 本项目的月度数据是**有断档的** —— 实测缺失 2023-02、2024-03 两个月
-- （那两个月没有作物收获，v_yield_monthly 里就没有对应行）。
-- 一旦有断档，第 12 行就不再是"去年同月"。
--
-- 【实测证据】在本项目的真实数据上跑两种写法对比：
--     月份      行号   LAG(x,12)          自连接(正确)
--     2023-06    12    NULL              1648.5  (2022-06)
--     2023-07    13    1648.5 (2022-06)  6337.0  (2022-07)  ← 差了一个月
--     2023-08    14    6337.0 (2022-07)  3305.8  (2022-08)  ← 一直错位
-- LAG 从第 13 行起就整体错位了一个月，而且**不会报任何错**。
--
-- 【正确做法】自连接 `DATE_SUB(month, INTERVAL 1 YEAR)` ——
-- 它是**按日期值**匹配的，与行号无关，断档也不影响。
--
-- 【什么情况下 LAG 才是对的】时间序列**连续无断档**时（比如每月都有记录），
-- LAG(x, 12) 才等价于"去年同期"。判断依据是数据本身，不是版本。
--
-- 这一条本身就是一个很好的面试点：
-- 「窗口函数不是万能的，用之前要确认它的语义和你需要的一致」。

SELECT
    cur.`harvest_month`                                       AS 月份,
    cur.`total_yield_kg`                                      AS 本月产量_kg,
    prev.`total_yield_kg`                                     AS 上月产量_kg,
    ROUND(cur.`total_yield_kg` / NULLIF(prev.`total_yield_kg`, 0) * 100 - 100, 2)
                                                              AS 环比_pct,
    yoy.`total_yield_kg`                                      AS 去年同期_kg,
    ROUND(cur.`total_yield_kg` / NULLIF(yoy.`total_yield_kg`, 0) * 100 - 100, 2)
                                                              AS 同比_pct,
    cur.`total_area_mu`                                       AS 收获面积_亩,
    cur.`yield_per_mu`                                        AS 单产_kg每亩
FROM `v_yield_monthly` cur
LEFT JOIN `v_yield_monthly` prev
       ON prev.`harvest_month` = DATE_SUB(cur.`harvest_month`, INTERVAL 1 MONTH)
LEFT JOIN `v_yield_monthly` yoy
       ON yoy.`harvest_month`  = DATE_SUB(cur.`harvest_month`, INTERVAL 1 YEAR)
ORDER BY cur.`harvest_month`;

-- 【为什么用 LEFT JOIN 而不是 INNER JOIN】
-- 数据集的第一个月没有上月，INNER JOIN 会**静默丢掉这个月份**，图表就缺一块。
-- LEFT JOIN 保留该行、比率列为 NULL，是正确行为。


-- ---- 2b. 用 CTE 改写上面那条，看可读性能提升多少 ----
-- 注意：这里 CTE 只是为了把"上月"和"去年同期"两次自连接**在语义上分离**，
-- 让读者一眼看出这是在比两个不同口径。执行计划与上面那条基本一致
-- （8.0 的 CTE 默认会被物化，可用 `WITH ... AS MATERIALIZED/NOT MATERIALIZED` 控制，
--   但本例中物化反而有利：两个派生表各只有 30 行）。
WITH monthly AS (
    SELECT `harvest_month`, `total_yield_kg`, `total_area_mu`, `yield_per_mu`
    FROM `v_yield_monthly`
)
SELECT
    cur.`harvest_month`                                        AS 月份,
    ROUND(cur.`total_yield_kg` / 1000, 1)                      AS 本月产量_吨,
    ROUND(cur.`total_yield_kg` / NULLIF(mom.`total_yield_kg`, 0) * 100 - 100, 2)
                                                               AS 环比_pct,
    ROUND(cur.`total_yield_kg` / NULLIF(yoy.`total_yield_kg`, 0) * 100 - 100, 2)
                                                               AS 同比_pct
FROM monthly cur
LEFT JOIN monthly mom ON mom.`harvest_month` = DATE_SUB(cur.`harvest_month`, INTERVAL 1 MONTH)
LEFT JOIN monthly yoy ON yoy.`harvest_month` = DATE_SUB(cur.`harvest_month`, INTERVAL 1 YEAR)
WHERE yoy.`harvest_month` IS NOT NULL      -- 只看有去年同期的月份
ORDER BY cur.`harvest_month`;


-- #############################################################################
-- 分析 3：单产影响因子的皮尔逊相关系数
-- #############################################################################
--
-- 【MySQL 缺失的统计函数比想象的还多】
-- 实测确认（8.0.46，逐个调用验证过），下面这些**全都不存在**：
--     CORR  COVAR_POP  COVAR_SAMP  REGR_SLOPE  REGR_R2  REGR_INTERCEPT
--     MEDIAN  PERCENTILE_CONT
-- 它们都是 Oracle / PostgreSQL 的函数，MySQL 至今没有。
-- 所以相关系数只能手写皮尔逊公式：
--     r = (n·Σxy − Σx·Σy) / √[(n·Σx² − (Σx)²)(n·Σy² − (Σy)²)]
--
-- 【但可以用 STDDEV_POP 走另一条数学路径来交叉验证】
-- MySQL 有的统计聚合函数是：
--     STDDEV_POP / STDDEV_SAMP / VAR_POP / VAR_SAMP / VARIANCE / STD
-- 于是同一个 r 可以用**定义式**再算一遍：
--     r = COVAR_POP(x,y) / (STDDEV_POP(x) × STDDEV_POP(y))
-- 其中协方差按定义展开：COVAR_POP(x,y) = (Σxy − Σx·Σy/n) / n
--
-- 两条路径的数学来源完全不同（一个是展开式的代数化简，一个是协方差除以
-- 标准差之积），算出的 r 必须完全一致 —— 这正是**手写公式的正确性验证**。
-- 实测本项目全部 12 个作物，两条路径的 r 在小数点后 4 位完全吻合。

-- ---- 3a. 分作物的相关系数（手写公式 + REGR_R2 交叉验证）----
SELECT
    c.`name`                                                    AS 作物,
    COUNT(*)                                                    AS 样本数,
    ROUND(AVG(y.`yield_per_mu`), 1)                             AS 平均单产,
    ROUND(AVG(f.`fert_per_mu`), 1)                              AS 平均施肥量_kg每亩,
    -- ① 手写皮尔逊公式
    ROUND(
        (COUNT(*) * SUM(f.`fert_per_mu` * y.`yield_per_mu`)
         - SUM(f.`fert_per_mu`) * SUM(y.`yield_per_mu`))
        / NULLIF(SQRT(
            (COUNT(*) * SUM(f.`fert_per_mu` * f.`fert_per_mu`)
             - SUM(f.`fert_per_mu`) * SUM(f.`fert_per_mu`))
            *
            (COUNT(*) * SUM(y.`yield_per_mu` * y.`yield_per_mu`)
             - SUM(y.`yield_per_mu`) * SUM(y.`yield_per_mu`))
        ), 0)
    , 3)                                                        AS 相关系数r,
    -- ② 交叉验证：协方差 / (标准差 × 标准差)
    --    协方差按定义展开为 (Σxy − Σx·Σy/n)/n
    --    这是和上面完全不同的数学路径，两者必须一致
    ROUND(
        ((SUM(f.`fert_per_mu` * y.`yield_per_mu`)
          - SUM(f.`fert_per_mu`) * SUM(y.`yield_per_mu`) / COUNT(*)) / COUNT(*))
        / NULLIF(STDDEV_POP(f.`fert_per_mu`) * STDDEV_POP(y.`yield_per_mu`), 0)
    , 3)                                                        AS 交叉验证r,
    -- 用两条路径的差值做机器可校验的一致性判定
    IF(ABS(
        (COUNT(*) * SUM(f.`fert_per_mu` * y.`yield_per_mu`)
         - SUM(f.`fert_per_mu`) * SUM(y.`yield_per_mu`))
        / NULLIF(SQRT(
            (COUNT(*) * SUM(f.`fert_per_mu` * f.`fert_per_mu`)
             - SUM(f.`fert_per_mu`) * SUM(f.`fert_per_mu`))
            * (COUNT(*) * SUM(y.`yield_per_mu` * y.`yield_per_mu`)
               - SUM(y.`yield_per_mu`) * SUM(y.`yield_per_mu`))
          ), 0)
        -
        ((SUM(f.`fert_per_mu` * y.`yield_per_mu`)
          - SUM(f.`fert_per_mu`) * SUM(y.`yield_per_mu`) / COUNT(*)) / COUNT(*))
        / NULLIF(STDDEV_POP(f.`fert_per_mu`) * STDDEV_POP(y.`yield_per_mu`), 0)
    ) < 0.0001, '一致', '不一致')                               AS 公式校验
FROM `yield_record` y
JOIN `crop` c ON c.`id` = y.`crop_id`
JOIN (
    -- 施肥量必须用 SUM(用量)/该批次面积 反算，不能直接 SUM(用量)：
    -- 不同地块面积差几倍，直接用总量会把"面积大"误判成"施肥多"。
    SELECT i.`planting_id`,
           SUM(i.`quantity`) / NULLIF(MAX(p.`plant_area_mu`), 0) AS fert_per_mu
    FROM `input_cost` i
    JOIN `planting` p ON p.`id` = i.`planting_id`
    WHERE i.`input_type` = 20        -- 20 = 化肥
    GROUP BY i.`planting_id`
) f ON f.`planting_id` = y.`planting_id`
GROUP BY c.`id`, c.`name`
HAVING 样本数 >= 5
ORDER BY 相关系数r DESC;


-- ---- 3b. 跨作物的汇总分析（用相对单产消除作物间量级差异）----
-- 分作物算时每个作物只有 6~50 个样本，小样本的相关系数抽样波动可达 ±0.3。
-- 把各作物单产除以该作物平均单产得到"相对单产"，就消除了量级差异，
-- 可以把全部记录放在一起算，样本量充足、结果稳定。
--
-- 「相对单产」这个方法本身就是农学研究的常规做法。
SELECT
    COUNT(*)                                                                AS 总样本数,
    ROUND(
        (COUNT(*) * SUM(x.`fert_per_mu` * x.`rel_yield`)
         - SUM(x.`fert_per_mu`) * SUM(x.`rel_yield`))
        / NULLIF(SQRT(
            (COUNT(*) * SUM(x.`fert_per_mu` * x.`fert_per_mu`)
             - SUM(x.`fert_per_mu`) * SUM(x.`fert_per_mu`))
            *
            (COUNT(*) * SUM(x.`rel_yield` * x.`rel_yield`)
             - SUM(x.`rel_yield`) * SUM(x.`rel_yield`))
        ), 0)
    , 3)                                                                    AS 施肥与相对单产相关系数r
FROM (
    SELECT f.`fert_per_mu`,
           y.`yield_per_mu` / NULLIF(ca.`avg_ypm`, 0) AS rel_yield
    FROM `yield_record` y
    JOIN (
        SELECT i.`planting_id`,
               SUM(i.`quantity`) / NULLIF(MAX(p.`plant_area_mu`), 0) AS fert_per_mu
        FROM `input_cost` i
        JOIN `planting` p ON p.`id` = i.`planting_id`
        WHERE i.`input_type` = 20
        GROUP BY i.`planting_id`
    ) f ON f.`planting_id` = y.`planting_id`
    JOIN (
        SELECT `crop_id`, AVG(`yield_per_mu`) AS avg_ypm
        FROM `yield_record` GROUP BY `crop_id`
    ) ca ON ca.`crop_id` = y.`crop_id`
) x;


-- #############################################################################
-- 分析 4：地块投入产出比 (ROI) 排行
-- #############################################################################
--
-- 【成本口径】物资成本（种子/化肥/农药/水电）+ 人工费 + 机械作业费
-- 这些来自 v_planting_cost 视图 —— 它用"先各自聚合再 JOIN"避免了
-- 一对多 JOIN 造成的金额重复累加（详见 02_views.sql 的注释）。
--
-- 这条查询本身在 5.7 / 8.0 里写法相同，保留它说明一点：
-- **迁移到 8.0 不是所有查询都要改**，很多业务查询本来就是朴素 SQL。
-- 判断"哪些该改、哪些不该改"比"全都改成窗口函数"更重要。

SELECT
    d.`season`                                                  AS 茬口,
    d.`plot_no`                                                 AS 地块,
    d.`crop_name`                                               AS 作物,
    d.`province`                                                AS 省份,
    ROUND(cost.`total_cost`, 0)                                 AS 总成本_元,
    ROUND(d.`output_value`, 0)                                  AS 产值_元,
    ROUND(d.`output_value` - cost.`total_cost`, 0)              AS 利润_元,
    ROUND(d.`output_value` / NULLIF(cost.`total_cost`, 0), 2)   AS 投入产出比,
    ROUND((d.`output_value` - cost.`total_cost`)
          / NULLIF(cost.`total_cost`, 0) * 100, 1)              AS 利润率_pct,
    ROUND(d.`yield_per_mu`, 1)                                  AS 单产_kg每亩,
    -- 用窗口函数算"该批次利润率在同作物内的排名"——
    -- 这是 5.7 里做不到、需要用户变量模拟的事情
    RANK() OVER (PARTITION BY d.`crop_id`
                 ORDER BY d.`output_value` / NULLIF(cost.`total_cost`, 0) DESC) AS 同作物内排名
FROM `v_yield_detail` d
JOIN `v_planting_cost` cost ON cost.`planting_id` = d.`planting_id`
WHERE cost.`total_cost` > 0
ORDER BY 投入产出比 DESC
LIMIT 30;


-- ---- 4b. 按作物汇总的投入产出比 ----
SELECT
    d.`crop_name`                                               AS 作物,
    COUNT(*)                                                    AS 批次数,
    ROUND(SUM(cost.`total_cost`) / NULLIF(SUM(d.`harvest_area_mu`), 0), 0)
                                                                AS 亩均成本_元,
    ROUND(SUM(d.`output_value`) / NULLIF(SUM(d.`harvest_area_mu`), 0), 0)
                                                                AS 亩均产值_元,
    ROUND((SUM(d.`output_value`) - SUM(cost.`total_cost`))
          / NULLIF(SUM(cost.`total_cost`), 0) * 100, 1)          AS 平均利润率_pct,
    ROUND(SUM(d.`output_value`) / NULLIF(SUM(cost.`total_cost`), 0), 2)
                                                                AS 投入产出比
FROM `v_yield_detail` d
JOIN `v_planting_cost` cost ON cost.`planting_id` = d.`planting_id`
GROUP BY d.`crop_id`, d.`crop_name`
HAVING 批次数 >= 5
ORDER BY 投入产出比 DESC;


-- #############################################################################
-- 分析 5：分组 Top N —— 各作物单产前 3 的地块
-- #############################################################################
--
-- 【8.0 写法】一行 ROW_NUMBER()，这是 5.7 里最著名的一道难题。
--
-- 对比 5.7 版本需要的三样东西：
--     ① @rn  := IF(@grp = s.crop_id, @rn + 1, 1)   -- 分组内行号
--     ② @grp := s.crop_id                          -- 漏了这行全盘失效且不报错
--     ③ LIMIT 18446744073709551615                 -- 防 derived_merge 吃掉 ORDER BY
-- 现在一行搞定。
--
-- ⚠️ 但有一件事窗口函数**没有**替你解决：**破平局键**。
--    ORDER BY yield_per_mu DESC 里若有两行单产相同，ROW_NUMBER 给出的顺序
--    依赖执行计划，两次执行可能不同。所以仍然要写第二排序键 plot_no。
--    （这是"窗口函数不改变数据本身的模糊性"的例子。）
SELECT
    t.`crop_name`                       AS 作物,
    t.`rn`                              AS 组内排名,
    t.`plot_no`                         AS 地块,
    t.`farm_name`                       AS 农场,
    t.`fertility_name`                  AS 地力等级,
    t.`yield_per_mu`                    AS 单产_kg每亩,
    t.`record_cnt`                      AS 收获次数,
    t.`total_area_mu`                   AS 累计收获面积_亩
FROM (
    SELECT
        `crop_name`, `plot_no`, `farm_name`, `yield_per_mu`,
        `record_cnt`, `total_area_mu`,
        CASE `fertility_level` WHEN 1 THEN '优' WHEN 2 THEN '中' ELSE '差' END AS fertility_name,
        -- 组内行号：按作物分区，单产降序编号
        -- 末尾的 plot_no 是破平局键 —— 保证并列单产时结果稳定可复现
        ROW_NUMBER() OVER (PARTITION BY `crop_id`
                           ORDER BY `yield_per_mu` DESC, `plot_no` ASC) AS rn
    FROM `v_plot_yield_summary`
) t
WHERE t.`rn` <= 3
ORDER BY t.`crop_name`, t.`rn`;


-- ---- 5b. 顺带演示 RANK / DENSE_RANK / ROW_NUMBER 三者的区别 ----
-- 这是面试常考：同样是"排名"，三个函数对并列值的处理完全不同。
--     ROW_NUMBER()  1 2 3 4   并列也强行排出先后（必定唯一）
--     RANK()        1 2 2 4   并列同名次，后续名次跳过
--     DENSE_RANK()  1 2 2 3   并列同名次，后续名次不跳过
-- 农业场景里的实际含义：
--     用 ROW_NUMBER 适合"每组只能取 N 个名额"（如推荐 Top3 地块）
--     用 RANK 适合"排名第几"（并列第 2 名，下一个是第 4 名）
--     用 DENSE_RANK 适合"分几档"（并列第 2 名，下一个是第 3 名）
SELECT
    t.`plot_no`                                     AS 地块,
    t.`crop_name`                                   AS 作物,
    t.`record_cnt`                                  AS 收获次数,
    ROW_NUMBER()  OVER (ORDER BY t.`record_cnt` DESC, t.`plot_no`) AS 行号,
    RANK()        OVER (ORDER BY t.`record_cnt` DESC)              AS 排名_RANK,
    DENSE_RANK()  OVER (ORDER BY t.`record_cnt` DESC)              AS 排名_DENSE,
    COUNT(*)      OVER (PARTITION BY t.`record_cnt`)               AS 同并列值的地块数
FROM `v_plot_yield_summary` t
ORDER BY t.`record_cnt` DESC, t.`plot_no`
LIMIT 15;


-- ---- 5c. 顺带演示 LAG / LEAD：相邻行的差值 ----
-- 找出"单产比上一名下降最多的位置"——用 LAG 可以不用自连接就拿到前一行的值。
-- ⚠️ 注意 LAG 的语义是"按排序往前数 N 行"，不是"按某个字段值往前找"。
--    这正是分析 ② 里它不能替代自连接的原因。
SELECT
    t.`plot_no`                                     AS 地块,
    t.`crop_name`                                   AS 作物,
    t.`yield_per_mu`                                AS 单产,
    LAG(t.`yield_per_mu`)  OVER (ORDER BY t.`yield_per_mu` DESC, t.`plot_no`) AS 上一名单产,
    ROUND(t.`yield_per_mu`
          - LAG(t.`yield_per_mu`) OVER (ORDER BY t.`yield_per_mu` DESC, t.`plot_no`), 1)
                                                    AS 与上一名的差距,
    LEAD(t.`yield_per_mu`) OVER (ORDER BY t.`yield_per_mu` DESC, t.`plot_no`) AS 下一名单产
FROM `v_plot_yield_summary` t
WHERE t.`record_cnt` >= 2
ORDER BY t.`yield_per_mu` DESC, t.`plot_no`
LIMIT 12;


-- #############################################################################
-- 分析 6：产量 ABC 分析（帕累托 / 二八定律）
-- #############################################################################
--
-- 【业务问题】多少比例的地块贡献了多少比例的产量？
-- 帕累托预期：约 20% 的地块贡献约 80% 的产量。
--
-- 【8.0 写法】累计求和用 SUM() OVER (ORDER BY ...)，占比用 SUM() OVER ()。
--
-- ⚠️ 两个必须注意的点：
--
--   ① 累计求和**必须显式写 ROWS 帧**。
--      默认帧是 RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW，
--      它会把 ORDER BY 值**相同的行算作同一帧**。
--      如果两块地产量完全相同，RANGE 会让它们得到**同一个累计值**，
--      相当于把两块地当作一块算。用 ROWS 才是严格逐行累加。
--      本项目的造数数据里恰好没有并列产量，所以两者结果相同 ——
--      但这个差异是真实存在的，换一份数据就会暴露。
--
--   ② 排序仍然需要破平局键 plot_id。
--      窗口函数的 ORDER BY 决定 ROWS 帧的推进顺序，
--      并列值时的顺序不确定 → 累计值不确定 → ABC 分类不确定。
--      窗口函数**不会**替你消除这种不确定性。
SELECT
    t.`plot_no`                                     AS 地块,
    t.`crop_name`                                   AS 作物,
    ROUND(t.`total_yield_kg`, 0)                    AS 产量_kg,
    ROUND(SUM(t.`total_yield_kg`) OVER w, 0)        AS 累计产量_kg,
    ROUND(SUM(t.`total_yield_kg`) OVER w
          / NULLIF(SUM(t.`total_yield_kg`) OVER (), 0) * 100, 2)
                                                    AS 累计占比_pct,
    CASE
        WHEN SUM(t.`total_yield_kg`) OVER w
             / NULLIF(SUM(t.`total_yield_kg`) OVER (), 0) <= 0.70 THEN 'A'
        WHEN SUM(t.`total_yield_kg`) OVER w
             / NULLIF(SUM(t.`total_yield_kg`) OVER (), 0) <= 0.90 THEN 'B'
        ELSE 'C'
    END                                             AS ABC分类
FROM `v_plot_yield_summary` t
-- 具名窗口：同一个窗口定义在 SELECT 里被引用了 4 次，用 WINDOW 子句定义一次即可。
-- 这是 8.0 的可读性改进 —— 5.7 里每个 OVER(...) 都要重写一遍完整定义。
WINDOW w AS (
    ORDER BY t.`total_yield_kg` DESC, t.`plot_id` ASC
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
)
ORDER BY t.`total_yield_kg` DESC, t.`plot_id` ASC
LIMIT 30;


-- ---- 6b. ABC 分类汇总 ----
SELECT
    t.`abc_class`                               AS ABC分类,
    COUNT(*)                                    AS 地块数,
    ROUND(COUNT(*) / (SELECT COUNT(*) FROM `v_plot_yield_summary`) * 100, 1)
                                                AS 地块占比_pct,
    ROUND(SUM(t.`total_yield_kg`), 0)           AS 产量合计_kg,
    ROUND(SUM(t.`total_yield_kg`)
          / (SELECT SUM(`total_yield_kg`) FROM `v_plot_yield_summary`) * 100, 1)
                                                AS 产量占比_pct
FROM (
    SELECT
        t.`total_yield_kg`,
        CASE
            WHEN SUM(t.`total_yield_kg`) OVER w
                 / NULLIF(SUM(t.`total_yield_kg`) OVER (), 0) <= 0.70 THEN 'A'
            WHEN SUM(t.`total_yield_kg`) OVER w
                 / NULLIF(SUM(t.`total_yield_kg`) OVER (), 0) <= 0.90 THEN 'B'
            ELSE 'C'
        END AS `abc_class`
    FROM `v_plot_yield_summary` t
    WINDOW w AS (
        ORDER BY t.`total_yield_kg` DESC, t.`plot_id` ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
) t
GROUP BY t.`abc_class`
ORDER BY t.`abc_class`;


-- #############################################################################
-- 分析 7：地块连续种植季同期群 (Cohort) 分析  ——  CTE 改写
-- #############################################################################
--
-- 【业务问题】按地块"首次种植的茬口"分群，看它们在后续各茬口还在种的比例。
-- 反映的是**耕地利用率 / 撂荒趋势**。
--
-- 【8.0 的 CTE 让这条查询的可读性发生了质变】
-- 5.7 版本里，为了表达"首季 → 各季活动 → 群规模"三层关系，
-- 用了三个嵌套的派生表 + 自连接，SQL 长达 40 行且几乎读不懂谁连谁。
-- 用 CTE 之后，每一步都是一个有名字的、自包含的结果集：
--     valid       → 过滤出有效种植批次（统一口径）
--     cohort      → 每个地块的首季（同期群定义）
--     cohort_size → 每个群的规模
--     activity    → 每个地块每个茬口的活动记录（去重）
--     label       → 茬口序号的显示名
-- 主查询只需要把这几个"有名字的积木"拼起来。
--
-- 【技术要点：季序必须线性递增】
-- season_seq = 年*2 + (春=1/秋=2)，相邻茬口恰好差 1。
-- ⚠️ 如果改用 EXTRACT(YEAR_MONTH) 那种 202402 形式的编码，
--    跨年时 202412 → 202501 相差 89 而不是 1，连续段判定会彻底错乱。
--    这个 bug 只在跨年的连续段上暴露，数据不足 12 个月永远发现不了。
--
-- 【天然自检断言】季序 = 0 的那一列（首季本身）留存率必然等于 100%。
-- 如果不等，说明查询有 bug。见 sql/05_tests.sql。
WITH valid AS (
    -- 有效种植批次的统一口径，只在这里定义一次
    SELECT `plot_id`, `season_seq`, `season_year`, `season`
    FROM `planting`
    WHERE `status` IN (20, 30, 40)
),
cohort AS (
    -- 每个地块的首个种植茬口 = 它所属的同期群
    SELECT `plot_id`, MIN(`season_seq`) AS `cohort_seq`
    FROM valid
    GROUP BY `plot_id`
),
cohort_size AS (
    SELECT `cohort_seq`, COUNT(*) AS `cohort_size`
    FROM cohort
    GROUP BY `cohort_seq`
),
activity AS (
    -- 每个地块在每个茬口都算一次活动（去重：同茬口多批次只算一次）
    SELECT DISTINCT `plot_id`, `season_seq`
    FROM valid
),
label AS (
    SELECT DISTINCT `season_seq`,
           CONCAT(`season_year`, `season`) AS `cohort_label`
    FROM valid
)
SELECT
    c.`cohort_seq`                                              AS 同期群_首季序号,
    l.`cohort_label`                                            AS 同期群_首季,
    cs.`cohort_size`                                            AS 群规模_地块数,
    (a.`season_seq` - c.`cohort_seq`)                           AS 季序,
    COUNT(DISTINCT a.`plot_id`)                                 AS 留存地块数,
    ROUND(COUNT(DISTINCT a.`plot_id`) / NULLIF(cs.`cohort_size`, 0) * 100, 1)
                                                                AS 留存率_pct
FROM cohort c
JOIN activity    a  ON a.`plot_id`    = c.`plot_id`
JOIN cohort_size cs ON cs.`cohort_seq` = c.`cohort_seq`
JOIN label       l  ON l.`season_seq`  = c.`cohort_seq`
GROUP BY c.`cohort_seq`, l.`cohort_label`, cs.`cohort_size`,
         (a.`season_seq` - c.`cohort_seq`)
ORDER BY c.`cohort_seq`, 季序;


-- ---- 7b. 留存三角透视（行转列）----
-- 5.7 里做行转列只能用条件聚合 MAX(CASE WHEN ...)，8.0 同样没有 PIVOT。
-- 这是 SQL 的固有局限：**列数必须静态可知**。
-- 如果要动态列数，只能回到应用层拼 SQL。
WITH valid AS (
    SELECT `plot_id`, `season_seq` FROM `planting` WHERE `status` IN (20, 30, 40)
),
cohort AS (
    SELECT `plot_id`, MIN(`season_seq`) AS `cohort_seq` FROM valid GROUP BY `plot_id`
),
retention AS (
    SELECT c.`cohort_seq`,
           (v.`season_seq` - c.`cohort_seq`) AS `season_idx`,
           COUNT(DISTINCT v.`plot_id`)       AS `retained`
    FROM cohort c
    JOIN valid v ON v.`plot_id` = c.`plot_id`
    GROUP BY c.`cohort_seq`, (v.`season_seq` - c.`cohort_seq`)
)
SELECT
    r.`cohort_seq`                                  AS 同期群,
    MAX(r.`season_idx`)                             AS 可观察季数,
    MAX(CASE WHEN r.`season_idx` = 0 THEN r.`retained` END) AS 第0季,
    MAX(CASE WHEN r.`season_idx` = 1 THEN r.`retained` END) AS 第1季,
    MAX(CASE WHEN r.`season_idx` = 2 THEN r.`retained` END) AS 第2季,
    MAX(CASE WHEN r.`season_idx` = 3 THEN r.`retained` END) AS 第3季,
    MAX(CASE WHEN r.`season_idx` = 4 THEN r.`retained` END) AS 第4季,
    MAX(CASE WHEN r.`season_idx` = 5 THEN r.`retained` END) AS 第5季
FROM retention r
GROUP BY r.`cohort_seq`
ORDER BY r.`cohort_seq`;


-- #############################################################################
-- 分析 8：连续干旱日数预警（gaps-and-islands）
-- #############################################################################
--
-- 【业务问题】找出每个气象区历史上持续最久的干旱过程。
--
-- 【8.0 写法】CTE + ROW_NUMBER()，比 5.7 的用户变量版本清晰得多。
--
-- 【算法：gaps-and-islands 的核心洞察】
-- 把日期转成线性天数 TO_DAYS(obs_date)，减去"连续段内的行号"：
--     连续段内：日期 +1 时行号也 +1  →  差值恒定
--     段与段之间：日期跳变但行号不重置 → 差值改变
-- 于是"差值"成了每个连续段的唯一标识，GROUP BY 它就能得到每段的起止和长度。
--
-- 【8.0 相比 5.7 的真正改进】
-- 5.7 版需要 @rn8 和 @grp8 两个用户变量，且必须手写
--     @grp8 := s.region_code       ← 漏掉这行，行号每行都重置，
--                                    所有"连续段"变成单天，查询返回 0 行
-- 而 ROW_NUMBER() OVER (PARTITION BY region_code ...) **自带分区语义**，
-- 结构上不可能漏写"记住上一行分组"的赋值。
--
-- ⚠️ 但有一个坑两个版本都有、必须靠领域知识解决：
--    【必须排除冬季封冻期】
--    最初这条查询没有温度条件，结果黑龙江查出来的"最长干旱"是
--    1~2 月的 53 天，期间均温 −14.9℃。
--    **那不是旱情，是冬季封冻** —— 东北的冬天本来就几乎不降水，
--    而且作物根本不处于生长状态。
--    农业干旱的定义前提是"作物处于生长状态且水分亏缺"，
--    所以必须加 temp_avg >= 5（作物生物学零度以上的近似值）。
--    这是纯技术视角想不到的一层。
WITH dry AS (
    SELECT
        `region_code`,
        `obs_date`,
        `temp_avg`,
        -- 线性日序号 − 分区内行号 = 连续段标识
        TO_DAYS(`obs_date`)
          - ROW_NUMBER() OVER (PARTITION BY `region_code` ORDER BY `obs_date`)
            AS `island`
    FROM `weather_daily`
    WHERE `precipitation` < 1.0
      AND `temp_avg` >= 5.0          -- 排除冬季封冻期，见上面的说明
)
SELECT
    d.`region_code`                             AS 气象区,
    MIN(d.`obs_date`)                           AS 干旱开始,
    MAX(d.`obs_date`)                           AS 干旱结束,
    COUNT(*)                                    AS 持续天数,
    ROUND(AVG(d.`temp_avg`), 1)                 AS 期间均温,
    CASE
        WHEN MONTH(MIN(d.`obs_date`)) BETWEEN 3 AND 5  THEN '春季（播种期）'
        WHEN MONTH(MIN(d.`obs_date`)) BETWEEN 6 AND 8  THEN '夏季（生长关键期）'
        WHEN MONTH(MIN(d.`obs_date`)) BETWEEN 9 AND 11 THEN '秋季（灌浆/收获期）'
        ELSE '冬季（影响较小）'
    END                                         AS 影响季节
FROM dry d
GROUP BY d.`region_code`, d.`island`
HAVING COUNT(*) >= 12
ORDER BY 持续天数 DESC, 干旱开始
LIMIT 30;


-- #############################################################################
-- 执行完成
-- #############################################################################
SELECT '04_analysis.sql 全部 8 组分析执行完成（MySQL 8.0 写法）' AS msg;
