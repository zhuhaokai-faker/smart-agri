-- =============================================================================
--  公共视图层
--  MySQL 8.0
-- =============================================================================
--
--  【为什么要有视图层】
--  8 组分析查询里有 6 组都要"产量记录 JOIN 种植批次 JOIN 地块 JOIN 农场 JOIN 作物"。
--  把这串 JOIN 抽成视图有三个收益：
--    1. 口径统一：所有分析用同一份连接逻辑，"有效批次"的定义不会各写各的；
--    2. 可读性：分析查询只表达业务逻辑，不被 5 层 JOIN 淹没（SQL 才讲得清楚）；
--    3. 可维护：改了表结构只需改视图，不用改 8 个分析查询。
--
--  【视图的性能真相（面试常问，8.0 并没有改变这一点）】
--  MySQL 会把视图展开成派生表（derived table），**视图本身不存储数据**，
--  每次查询都会重新执行。所以：
--    - 视图 ≠ 性能优化手段，它只是"保存下来的查询"；
--    - 视图里如果有 GROUP BY，就无法被 derived_merge 合并，
--      会被物化成临时表 —— 嵌套多层视图会带来实打实的性能问题；
--    - 真正要提升性能，得用 sql/03_optimize.sql 里的**汇总表**（物化）。
--  本项目刻意保持视图扁平（最多一层聚合），就是为了避免这个陷阱。
--
--  【8.0 的一个相关变化】
--  8.0 新增了 derived_condition_pushdown 优化（默认开启），
--  它能把外层的 WHERE 条件下推到派生表里，减少物化行数。
--  但这**不能替代**扁平化设计 —— 下推只减少行数，不消除物化本身。
-- =============================================================================

USE `smart_agri`;

DROP VIEW IF EXISTS `v_yield_detail`;
DROP VIEW IF EXISTS `v_planting_detail`;
DROP VIEW IF EXISTS `v_planting_cost`;
DROP VIEW IF EXISTS `v_yield_monthly`;
DROP VIEW IF EXISTS `v_plot_yield_summary`;
DROP VIEW IF EXISTS `v_crop_region_yield`;


-- =============================================================================
-- v_planting_detail — 种植批次的全维度展开（分析的基础表）
-- =============================================================================
CREATE OR REPLACE VIEW `v_planting_detail` AS
SELECT
    p.`id`              AS planting_id,
    p.`batch_no`,
    p.`season`,
    p.`season_year`,
    p.`season_seq`,
    p.`sow_date`,
    p.`emerge_date`,
    p.`flower_date`,
    p.`mature_date`,
    p.`harvest_date`,
    p.`status`,
    p.`plant_area_mu`,
    p.`density_per_mu`,
    pl.`id`             AS plot_id,
    pl.`plot_no`,
    pl.`name`           AS plot_name,
    pl.`area_mu`,
    pl.`fertility_level`,
    pl.`soil_type`,
    pl.`irrigation_type`,
    pl.`longitude`,
    pl.`latitude`,
    f.`id`              AS farm_id,
    f.`name`            AS farm_name,
    f.`province`,
    f.`city`,
    f.`region_code`,
    c.`id`              AS crop_id,
    c.`name`            AS crop_name,
    c.`variety`,
    c.`category`,
    c.`base_temp`,
    c.`gdd_maturity`,
    c.`growth_days`
FROM `planting` p
JOIN `plot`  pl ON pl.`id` = p.`plot_id`
JOIN `farm`  f  ON f.`id`  = pl.`farm_id`
JOIN `crop`  c  ON c.`id`  = p.`crop_id`;


-- =============================================================================
-- v_yield_detail — 产量记录 + 全维度（分析 2/3/4/5/6 的基础）
-- =============================================================================
-- 注意这里**没有**从 v_planting_detail 再 JOIN 一层，而是重新写了一遍 JOIN。
-- 原因：视图嵌套会让 MySQL 展开成多层派生表。带聚合或多层的派生表
-- 无法被 derived_merge 合并进外层，会逐层物化成临时表。
-- 扁平化一点点代码冗余，换来的是执行计划可预测 —— 这是值得的取舍。
--
-- （8.0 的 CTE 让这种"多层嵌套"有了更清晰的写法，但视图定义里
--   不能使用 CTE —— 所以视图这一层仍然是扁平化最省事。）
CREATE OR REPLACE VIEW `v_yield_detail` AS
SELECT
    y.`id`              AS yield_id,
    y.`harvest_date`,
    y.`harvest_month`,
    y.`harvest_area_mu`,
    y.`yield_kg`,
    y.`yield_per_mu`,
    y.`grade`,
    y.`unit_price`,
    y.`output_value`,
    y.`moisture_content`,
    p.`id`              AS planting_id,
    p.`batch_no`,
    p.`season`,
    p.`season_year`,
    p.`season_seq`,
    p.`sow_date`,
    p.`plant_area_mu`,
    pl.`id`             AS plot_id,
    pl.`plot_no`,
    pl.`name`           AS plot_name,
    pl.`fertility_level`,
    f.`id`              AS farm_id,
    f.`name`            AS farm_name,
    f.`province`,
    f.`city`,
    f.`region_code`,
    c.`id`              AS crop_id,
    c.`name`            AS crop_name,
    c.`variety`,
    c.`category`,
    c.`base_temp`,
    c.`gdd_maturity`
FROM `yield_record` y
JOIN `planting` p  ON p.`id`  = y.`planting_id`
JOIN `plot`     pl ON pl.`id` = y.`plot_id`
JOIN `farm`     f  ON f.`id`  = pl.`farm_id`
JOIN `crop`     c  ON c.`id`  = y.`crop_id`;


-- =============================================================================
-- v_planting_cost — 单批次成本结构（分析 4 投入产出比的基础）
-- =============================================================================
-- 农资投入（物资成本 + 施肥量）和农事记录（人工费、机械费）来自两张表，
-- 各自先聚合再 LEFT JOIN —— 这是**避免 JOIN 后聚合重复计数**的标准做法。
--
-- ⚠️ 如果直接写成：
--      FROM planting p
--      LEFT JOIN input_cost i ON i.planting_id = p.id
--      LEFT JOIN farming_log l ON l.planting_id = p.id
--    一个批次有 5 条农资 + 8 条农事记录时，会产生 5×8=40 行的笛卡尔积，
--    然后 SUM(i.amount) 会把每条农资金额重复算 8 遍。这是数据聚合最经典的
--    错误之一，而且**结果看起来只是"数字偏大"，不会报错**，极难发现。
--    正确做法就是下面这样：先在子查询里各自聚合到 planting 粒度，再 JOIN。
CREATE OR REPLACE VIEW `v_planting_cost` AS
SELECT
    p.`id`                  AS planting_id,
    COALESCE(ic.material_cost, 0)  AS material_cost,
    COALESCE(fl.labor_cost, 0)     AS labor_cost,
    COALESCE(fl.machine_cost, 0)   AS machine_cost,
    COALESCE(ic.material_cost, 0) + COALESCE(fl.labor_cost, 0)
        + COALESCE(fl.machine_cost, 0)             AS total_cost,
    COALESCE(ic.fert_qty, 0)                       AS fert_qty,
    COALESCE(ic.seed_cost, 0)                      AS seed_cost,
    COALESCE(ic.fert_cost, 0)                      AS fert_cost,
    COALESCE(ic.pesticide_cost, 0)                 AS pesticide_cost,
    COALESCE(fl.op_count, 0)                       AS op_count,
    p.`plant_area_mu`
FROM `planting` p
LEFT JOIN (
    SELECT `planting_id`,
           SUM(`amount`)                                                  AS material_cost,
           SUM(CASE WHEN `input_type` = 20 THEN `quantity` ELSE 0 END)    AS fert_qty,
           SUM(CASE WHEN `input_type` = 10 THEN `amount`   ELSE 0 END)    AS seed_cost,
           SUM(CASE WHEN `input_type` = 20 THEN `amount`   ELSE 0 END)    AS fert_cost,
           SUM(CASE WHEN `input_type` = 30 THEN `amount`   ELSE 0 END)    AS pesticide_cost
    FROM `input_cost`
    GROUP BY `planting_id`
) ic ON ic.`planting_id` = p.`id`
LEFT JOIN (
    SELECT `planting_id`,
           SUM(`labor_cost`)   AS labor_cost,
           SUM(`machine_cost`) AS machine_cost,
           COUNT(*)            AS op_count
    FROM `farming_log`
    GROUP BY `planting_id`
) fl ON fl.`planting_id` = p.`id`;


-- =============================================================================
-- v_yield_monthly — 月度产量汇总（分析 2 同比环比的基础）
-- =============================================================================
-- 【关键点：单产必须用 SUM(产量)/SUM(面积)，不能用 AVG(单产)】
-- 这是一个非常隐蔽的错误。举例：
--     A 号地：100 亩，单产 500  → 总产 50000
--     B 号地：  1 亩，单产 900  → 总产   900
--   正确总单产 = (50000+900)/(100+1) = 503.96
--   错误 AVG   = (500+900)/2         = 700
-- 两者差了 39%。原因是大面积地块应该被赋予更大权重。
-- 直接对"比率"取平均是**辛普森悖论**的典型表现 ——
-- 面试官问"平均单产怎么算"时，答 AVG(yield_per_mu) 立刻暴露数据敏感度不足。
CREATE OR REPLACE VIEW `v_yield_monthly` AS
SELECT
    y.`harvest_month`,
    SUM(y.`yield_kg`)                                    AS total_yield_kg,
    SUM(y.`output_value`)                                AS total_output_value,
    SUM(y.`harvest_area_mu`)                             AS total_area_mu,
    ROUND(SUM(y.`yield_kg`) / NULLIF(SUM(y.`harvest_area_mu`), 0), 2)
                                                         AS yield_per_mu,
    COUNT(*)                                             AS record_cnt,
    COUNT(DISTINCT y.`plot_id`)                          AS plot_cnt,
    COUNT(DISTINCT y.`crop_id`)                          AS crop_cnt
FROM `yield_record` y
GROUP BY y.`harvest_month`;


-- =============================================================================
-- v_plot_yield_summary — 地块级产量汇总（分析 4/5/6 与地图着色的基础）
-- =============================================================================
-- 再次强调 yield_per_mu 用 SUM/SUM 而非 AVG（同上）。
-- 每亩产值 output_per_mu 同理。
CREATE OR REPLACE VIEW `v_plot_yield_summary` AS
SELECT
    pl.`id`                 AS plot_id,
    pl.`plot_no`,
    pl.`name`               AS plot_name,
    pl.`area_mu`,
    pl.`fertility_level`,
    f.`id`                  AS farm_id,
    f.`name`                AS farm_name,
    f.`province`,
    f.`city`,
    p.`crop_id`,
    c.`name`                AS crop_name,
    COUNT(*)                                    AS record_cnt,
    SUM(y.`yield_kg`)                           AS total_yield_kg,
    SUM(y.`output_value`)                       AS total_output_value,
    SUM(y.`harvest_area_mu`)                    AS total_area_mu,
    ROUND(SUM(y.`yield_kg`) / NULLIF(SUM(y.`harvest_area_mu`), 0), 2)
                                                AS yield_per_mu,
    ROUND(SUM(y.`output_value`) / NULLIF(SUM(y.`harvest_area_mu`), 0), 2)
                                                AS output_per_mu
FROM `yield_record` y
JOIN `plot`     pl ON pl.`id` = y.`plot_id`
JOIN `farm`     f  ON f.`id`  = pl.`farm_id`
JOIN `planting` p  ON p.`id`  = y.`planting_id`
JOIN `crop`     c  ON c.`id`  = y.`crop_id`
GROUP BY pl.`id`, pl.`plot_no`, pl.`name`, pl.`area_mu`, pl.`fertility_level`,
         f.`id`, f.`name`, f.`province`, f.`city`, p.`crop_id`, c.`name`;


-- =============================================================================
-- v_crop_region_yield — 作物 × 地区 交叉汇总（分析 3 与地区对比的基础）
-- =============================================================================
CREATE OR REPLACE VIEW `v_crop_region_yield` AS
SELECT
    c.`id`              AS crop_id,
    c.`name`            AS crop_name,
    c.`category`,
    f.`id`              AS farm_id,
    f.`province`,
    f.`city`,
    f.`region_code`,
    COUNT(*)                                    AS record_cnt,
    ROUND(AVG(y.`yield_per_mu`), 2)             AS avg_yield_per_mu,
    ROUND(SUM(y.`yield_kg`) / NULLIF(SUM(y.`harvest_area_mu`), 0), 2)
                                                AS weighted_yield_per_mu,
    SUM(y.`yield_kg`)                           AS total_yield_kg,
    SUM(y.`output_value`)                       AS total_output_value
FROM `yield_record` y
JOIN `crop` c ON c.`id` = y.`crop_id`
JOIN `plot` pl ON pl.`id` = y.`plot_id`
JOIN `farm` f ON f.`id` = pl.`farm_id`
GROUP BY c.`id`, c.`name`, c.`category`, f.`id`, f.`province`, f.`city`, f.`region_code`;


-- =============================================================================
-- 视图自检
-- =============================================================================
SELECT '02_views.sql 执行完成' AS msg;
SELECT 'v_planting_detail' AS view_name, COUNT(*) AS rows_cnt FROM `v_planting_detail`
UNION ALL SELECT 'v_yield_detail',         COUNT(*) FROM `v_yield_detail`
UNION ALL SELECT 'v_planting_cost',        COUNT(*) FROM `v_planting_cost`
UNION ALL SELECT 'v_yield_monthly',        COUNT(*) FROM `v_yield_monthly`
UNION ALL SELECT 'v_plot_yield_summary',   COUNT(*) FROM `v_plot_yield_summary`
UNION ALL SELECT 'v_crop_region_yield',    COUNT(*) FROM `v_crop_region_yield`;
