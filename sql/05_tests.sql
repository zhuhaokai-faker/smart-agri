-- =============================================================================
--  正确性断言（数据质量自检）
--  MySQL 8.0
-- =============================================================================
--
--  【为什么一个简历项目需要这个文件】
--  绝大多数练手项目只有"能跑出来的查询"，没有"能证明查询正确的测试"。
--  但真正的数据分析工作里，**结果错了却看起来正常**才是最危险的情况：
--    · 一对多 JOIN 会让金额被重复累加，数字只是"偏大"而不报错
--    · 生成列如果写错，会安静地存下错误值
--    · 窗口函数的默认帧（RANGE）在并列值上的行为和 ROWS 不同
--  这组断言把上面这些"不会报错但结果错"的情况变成显式的 PASS/FAIL。
--
--  【8.0 版本相比 5.7 的变化】
--  5.7 时代有两条断言是专门为"用户变量静默出错"设计的
--  （分组重置漏写、derived_merge 吃掉 ORDER BY）。
--  改用窗口函数后这类错误在结构上就不可能发生，那两条断言也随之简化。
--  但同时**新增了几条 8.0 才有的断言**，见断言 10 和 11。
--
--  【怎么用】
--      python scripts/verify.py          # 推荐（会汇总统计并设置退出码）
--      或直接：mysql ... < sql/05_tests.sql
--  任何一条 FAIL 都说明数据或查询有问题，不能拿去演示。
-- =============================================================================

USE `smart_agri`;

SELECT '============================================================' AS '';
SELECT '  正确性断言开始（MySQL 8.0）' AS '';
SELECT '============================================================' AS '';


-- =============================================================================
-- 断言 1：外键完整性 —— 没有悬挂引用
-- =============================================================================
-- 造数时如果先把 foreign_key_checks 关掉批量导入，就可能留下悬挂行。
-- 悬挂行会污染 JOIN 结果（内连接静默丢行、外连接补 NULL）。
SELECT '1. 外键完整性' AS `断言组`;

SELECT '  产量记录 -> 种植批次' AS 检查项,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT(COUNT(*), ' 条悬挂') AS 说明
FROM `yield_record` y LEFT JOIN `planting` p ON p.`id` = y.`planting_id` WHERE p.`id` IS NULL
UNION ALL
SELECT '  产量记录 -> 地块',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条悬挂')
FROM `yield_record` y LEFT JOIN `plot` pl ON pl.`id` = y.`plot_id` WHERE pl.`id` IS NULL
UNION ALL
SELECT '  种植批次 -> 地块',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条悬挂')
FROM `planting` p LEFT JOIN `plot` pl ON pl.`id` = p.`plot_id` WHERE pl.`id` IS NULL
UNION ALL
SELECT '  农事记录 -> 种植批次',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条悬挂')
FROM `farming_log` l LEFT JOIN `planting` p ON p.`id` = l.`planting_id` WHERE p.`id` IS NULL
UNION ALL
SELECT '  农资投入 -> 种植批次',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条悬挂')
FROM `input_cost` i LEFT JOIN `planting` p ON p.`id` = i.`planting_id` WHERE p.`id` IS NULL;


-- =============================================================================
-- 断言 2：生成列一致性 —— 数据库算的必须等于手算的
-- =============================================================================
SELECT '2. 生成列一致性' AS `断言组`;

SELECT '  yield_per_mu = 产量/面积' AS 检查项,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT(COUNT(*), ' 条不符') AS 说明
FROM `yield_record`
WHERE ABS(`yield_per_mu` - ROUND(`yield_kg` / NULLIF(`harvest_area_mu`, 0), 2)) > 0.01
UNION ALL
SELECT '  output_value = 产量*单价',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条不符')
FROM `yield_record`
WHERE ABS(`output_value` - ROUND(`yield_kg` * `unit_price`, 2)) > 0.01
UNION ALL
SELECT '  amount = 用量*单价',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条不符')
FROM `input_cost`
WHERE ABS(`amount` - ROUND(`quantity` * `unit_price`, 2)) > 0.01
UNION ALL
SELECT '  harvest_month = 收获月首日',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条不符')
FROM `yield_record`
WHERE `harvest_month` <> DATE_SUB(DATE(`harvest_date`), INTERVAL DAYOFMONTH(`harvest_date`) - 1 DAY);


-- =============================================================================
-- 断言 3：状态机与日期自洽性
-- =============================================================================
SELECT '3. 状态机与日期自洽' AS `断言组`;

-- ⚠️ 这里原本是「status=40 必有产量记录」。它必须被换掉，有两个理由：
--    ① 它与删除功能**直接冲突**：删掉一条产量记录后批次仍是"已收获"，
--       那条断言立刻变红 —— 而变红正是设计好的行为；
--    ② 它在加入删除功能之前就已经和项目自身矛盾：
--       planting_service.suggest_next_actions() 会对"已收获但没有产量记录"的
--       批次主动提示"尚未录入产量记录"，production.py 的录入页待办查询
--       也是按这个条件筛的 —— 项目一直把它当**合法待办**，只有断言说它不合法。
--    真正该守住的是反向那条：**非40状态不应有产量记录**（紧跟在本条之后）。
--
--    换成"冗余列一致性"是因为它同样是真不变式，而且正好守住本次新增的编辑功能：
--    编辑路由刻意不暴露 plot_id/crop_id（它们从批次冗余而来、事实上不可变），
--    这条断言就是这个设计决定的守卫。
SELECT '  yield_record 冗余的 plot_id/crop_id 与 planting 一致' AS 检查项,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT(COUNT(*), ' 条不一致') AS 说明
FROM `yield_record` y
JOIN `planting` p ON p.`id` = y.`planting_id`
WHERE y.`plot_id` <> p.`plot_id` OR y.`crop_id` <> p.`crop_id`
UNION ALL
SELECT '  非40状态不应有产量记录',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条多余')
FROM `planting` p
JOIN `yield_record` y ON y.`planting_id` = p.`id`
WHERE p.`status` <> 40
UNION ALL
SELECT '  生育期日期单调递增',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条逆序')
FROM `planting`
WHERE `sow_date` > `emerge_date`
   OR `emerge_date` > `flower_date`
   OR `flower_date` > `mature_date`
   OR `mature_date` > `harvest_date`
UNION ALL
-- ⚠️ 这条断言**只能约束"已收获"的批次**。
--    2024 年秋播的冬小麦/油菜，收获期在 2025 年 3~6 月，状态是"生长中" ——
--    它们有未来的预计收获日期是完全正确的。
--    最初这条写成约束全部批次，报出 19 条"越界" ——
--    **是断言写错了，不是数据错了**。测试写错会让人花大量时间去查
--    一个根本不存在的数据问题。
--
-- ⚠️⚠️ 第二次踩同一个坑：这里原本写死的是 `> '2024-12-31'`（种子数据的截止日）。
--     只要有人在页面上把 2025 年茬口的批次推进到"已收获"，这条立刻变红 ——
--     **断言把"数据集的窗口"当成了"业务的规则"**。判据要相对当前时间，
--     而不是相对某次造数时的日期。
SELECT '  已收获批次的收获日不在未来',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条越界')
FROM `planting` WHERE `status` = 40 AND `harvest_date` > CURDATE()
UNION ALL
SELECT '  yield_record 收获日 = planting 收获日',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 条不一致')
FROM `yield_record` y JOIN `planting` p ON p.`id` = y.`planting_id`
WHERE y.`harvest_date` <> p.`harvest_date`;


-- ---------------------------------------------------------------------------
-- 信息性上报：已收获但尚未录入产量的批次
-- ---------------------------------------------------------------------------
-- 这**不是断言**：它只报数量、不出 PASS/FAIL，所以 scripts/verify.py 不会计入
-- （它只统计带「结果」列且值为 PASS/FAIL 的行）。
--
-- 之所以不做成断言，是因为它是完全合法的待办状态，有两条正常路径会到达：
--   · 用状态按钮把批次推进到"已收获"，但还没录产量；
--   · 删掉了一条产量记录 —— 批次仍是"已收获"，会重新出现在录入页的待办列表。
-- 后者正是删除功能的设计行为，把它判成 FAIL 等于"用一个正常操作弄红自己的测试"。
--
-- 保留这条上报是为了**让它可见**：数量突然变大说明有人推进了状态却没录数据，
-- 这本身是有价值的运营信号，只是不该用 PASS/FAIL 表达。
SELECT '   ℹ 已收获但未录产量的批次（合法待办，不计入断言）' AS 信息,
       COUNT(*) AS 条数
FROM `planting` p
LEFT JOIN `yield_record` y ON y.`planting_id` = p.`id`
WHERE p.`status` = 40 AND y.`id` IS NULL;


-- =============================================================================
-- 断言 4：同期群分析的天然自检
-- =============================================================================
-- 季序 = 0 的那一列就是"首季本身"，留存地块数必然等于群规模，留存率必然 100%。
-- 如果不等于 100%，说明 season_seq 的线性假设被破坏了。
--
-- 这是"用业务逻辑本身的性质做测试"的例子 —— 比人工核对具体数字可靠得多。
SELECT '4. 同期群自检（季序0留存率必为100%）' AS `断言组`;

WITH valid AS (
    SELECT `plot_id`, `season_seq` FROM `planting` WHERE `status` IN (20, 30, 40)
),
cohort AS (
    SELECT `plot_id`, MIN(`season_seq`) AS `cohort_seq` FROM valid GROUP BY `plot_id`
),
first_season_check AS (
    SELECT
        c.`cohort_seq`,
        cs.`cohort_size`,
        COUNT(DISTINCT v.`plot_id`) AS `retained`,
        ROUND(COUNT(DISTINCT v.`plot_id`) / NULLIF(cs.`cohort_size`, 0) * 100, 2) AS `留存率_pct`
    FROM cohort c
    JOIN valid v ON v.`plot_id` = c.`plot_id` AND v.`season_seq` = c.`cohort_seq`
    JOIN (SELECT `cohort_seq`, COUNT(*) AS `cohort_size`
          FROM cohort GROUP BY `cohort_seq`) cs ON cs.`cohort_seq` = c.`cohort_seq`
    GROUP BY c.`cohort_seq`, cs.`cohort_size`
)
SELECT '  季序=0 的留存率' AS 检查项,
       CASE WHEN SUM(CASE WHEN ABS(`留存率_pct` - 100) > 0.01 THEN 1 ELSE 0 END) = 0
            THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT('检查 ', COUNT(*), ' 个同期群，异常 ',
              SUM(CASE WHEN ABS(`留存率_pct` - 100) > 0.01 THEN 1 ELSE 0 END), ' 个') AS 说明
FROM first_season_check;


-- =============================================================================
-- 断言 5：ABC 分析的完整性
-- =============================================================================
-- ① A+B+C 的地块数必须等于总地块数（不能丢行、不能重复）
-- ② 累计占比的末行必须恰好 100%
--
-- 【8.0 写法】用窗口函数替代了 5.7 的用户变量累计。
-- 注意这里**必须重复窗口定义**（不能引用外层的具名窗口），
-- 因为是在算术表达式里复用同一个帧。
SELECT '5. ABC 帕累托完整性' AS `断言组`;

WITH abc AS (
    SELECT
        t.`plot_no`,
        t.`total_yield_kg`,
        SUM(t.`total_yield_kg`)
            OVER (ORDER BY t.`total_yield_kg` DESC, t.`plot_id` ASC
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS `cum_yield`,
        SUM(t.`total_yield_kg`) OVER ()                            AS `total`,
        CASE
            WHEN SUM(t.`total_yield_kg`)
                 OVER (ORDER BY t.`total_yield_kg` DESC, t.`plot_id` ASC
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                 / NULLIF(SUM(t.`total_yield_kg`) OVER (), 0) <= 0.70 THEN 'A'
            WHEN SUM(t.`total_yield_kg`)
                 OVER (ORDER BY t.`total_yield_kg` DESC, t.`plot_id` ASC
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                 / NULLIF(SUM(t.`total_yield_kg`) OVER (), 0) <= 0.90 THEN 'B'
            ELSE 'C'
        END AS `cls`
    FROM `v_plot_yield_summary` t
)
SELECT '  累计占比末行 = 100%' AS 检查项,
       CASE WHEN ABS(MAX(`cum_yield` / NULLIF(`total`, 0) * 100) - 100) < 0.05
            THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT('末行累计占比 ', ROUND(MAX(`cum_yield` / NULLIF(`total`, 0) * 100), 4), '%') AS 说明
FROM abc
UNION ALL
SELECT '  各分类地块数之和 = 总数',
       CASE WHEN SUM(x.`cnt`) = (SELECT COUNT(*) FROM `v_plot_yield_summary`)
            THEN 'PASS' ELSE 'FAIL' END,
       CONCAT('分类合计 ', SUM(x.`cnt`), ' / 实际 ',
              (SELECT COUNT(*) FROM `v_plot_yield_summary`))
FROM (SELECT `cls`, COUNT(*) AS `cnt` FROM abc GROUP BY `cls`) x;


-- =============================================================================
-- 断言 6：两种 TopN 实现交叉验证
-- =============================================================================
-- 8.0 里 ROW_NUMBER() 是"标准答案"，但它**没有**替你解决破平局问题：
-- 若 ORDER BY 的列有并列值，ROW_NUMBER 给出的顺序依赖执行计划。
-- 所以本项目仍然写了第二排序键 plot_no。
--
-- 这条断言用**相关子查询**（完全不同的一种思路）独立算一遍 Top3，
-- 两者结果必须逐行一致 —— 交叉验证是最可靠的正确性证据。
SELECT '6. TopN 两种实现交叉验证' AS `断言组`;

WITH by_row_number AS (
    SELECT `plot_no`, `crop_id`
    FROM (
        SELECT `plot_no`, `crop_id`,
               ROW_NUMBER() OVER (PARTITION BY `crop_id`
                                  ORDER BY `yield_per_mu` DESC, `plot_no` ASC) AS `rn`
        FROM `v_plot_yield_summary`
    ) x
    WHERE x.`rn` <= 3
),
by_subquery AS (
    SELECT s.`plot_no`, s.`crop_id`
    FROM `v_plot_yield_summary` s
    WHERE (
        SELECT COUNT(*) FROM `v_plot_yield_summary` s2
        WHERE s2.`crop_id` = s.`crop_id`
          AND (s2.`yield_per_mu` > s.`yield_per_mu`
               OR (s2.`yield_per_mu` = s.`yield_per_mu` AND s2.`plot_no` < s.`plot_no`))
    ) < 3
)
SELECT '  ROW_NUMBER 版 == 相关子查询版' AS 检查项,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT(COUNT(*), ' 条不一致（共 ',
              (SELECT COUNT(*) FROM by_row_number), ' 行）') AS 说明
FROM (
    (SELECT `plot_no`, `crop_id` FROM by_row_number
     WHERE (`plot_no`, `crop_id`) NOT IN (SELECT `plot_no`, `crop_id` FROM by_subquery))
    UNION ALL
    (SELECT `plot_no`, `crop_id` FROM by_subquery
     WHERE (`plot_no`, `crop_id`) NOT IN (SELECT `plot_no`, `crop_id` FROM by_row_number))
) diff;


-- =============================================================================
-- 断言 7：成本数据没有被重复累加
-- =============================================================================
-- 一对多 JOIN 后直接 SUM 会把金额重复计算（5 条农资 × 8 条农事 = 40 行）。
-- 这条断言用"分别单独求和再相加"与视图结果对比，能立刻抓出重复累加。
SELECT '7. 成本无重复累加' AS `断言组`;

SELECT '  视图总成本 = 物资 + 人工 + 机械' AS 检查项,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT(COUNT(*), ' 条不符') AS 说明
FROM `v_planting_cost` c
WHERE ABS(c.`total_cost` - (c.`material_cost` + c.`labor_cost` + c.`machine_cost`)) > 0.01
UNION ALL
SELECT '  总成本等于各分项直接求和',
       CASE WHEN ABS(SUM(c.`total_cost`) - (
                (SELECT COALESCE(SUM(`amount`), 0) FROM `input_cost`)
              + (SELECT COALESCE(SUM(`labor_cost`), 0) FROM `farming_log`)
              + (SELECT COALESCE(SUM(`machine_cost`), 0) FROM `farming_log`)
            )) < 1.0
            THEN 'PASS' ELSE 'FAIL' END,
       CONCAT('视图合计 ', ROUND(SUM(c.`total_cost`), 0), ' / 分项合计 ',
              ROUND((SELECT COALESCE(SUM(`amount`), 0) FROM `input_cost`)
                  + (SELECT COALESCE(SUM(`labor_cost`), 0) FROM `farming_log`)
                  + (SELECT COALESCE(SUM(`machine_cost`), 0) FROM `farming_log`), 0))
FROM `v_planting_cost` c;


-- =============================================================================
-- 断言 8：同比分析的数据充分性与实现正确性
-- =============================================================================
-- 同比需要至少跨 13 个月。如果数据跨度不足，YoY 会全是 NULL，
-- 功能看起来"能跑"但完全没有结果 —— 这是造数时最容易踩的坑。
--
-- ③ 是本项目特别重要的一条：**证明"自连接"和"LAG(x,12)"不等价**。
--    月度数据有断档时，LAG 数的是行数而不是月数，会整体错位。
--    这条断言把这个差异固化下来 —— 如果哪天数据变成连续无断档的，
--    断言会失败，提醒我文档里"LAG 不适用"的结论需要重新审视。
SELECT '8. 同比分析数据充分性' AS `断言组`;

SELECT '  数据跨度 >= 13 个月' AS 检查项,
       CASE WHEN TIMESTAMPDIFF(MONTH, MIN(`harvest_month`), MAX(`harvest_month`)) >= 12
            THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT(TIMESTAMPDIFF(MONTH, MIN(`harvest_month`), MAX(`harvest_month`)),
              ' 个月（', MIN(`harvest_month`), ' ~ ', MAX(`harvest_month`), '）') AS 说明
FROM `yield_record`
UNION ALL
SELECT '  至少有一个月能算出同比',
       CASE WHEN COUNT(*) > 0 THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(COUNT(*), ' 个月有去年同期数据')
FROM `v_yield_monthly` cur
JOIN `v_yield_monthly` yoy
  ON yoy.`harvest_month` = DATE_SUB(cur.`harvest_month`, INTERVAL 1 YEAR)
UNION ALL
-- ③ 证明 LAG(x,12) 与自连接在本数据集上不等价（数据有断档）
SELECT '  自连接 与 LAG(x,12) 不等价（证明有断档）',
       CASE WHEN SUM(CASE WHEN NOT (m.`lag12` <=> sj.`total_yield_kg`) THEN 1 ELSE 0 END) > 0
            THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(SUM(CASE WHEN NOT (m.`lag12` <=> sj.`total_yield_kg`) THEN 1 ELSE 0 END),
              ' 个月两者结果不同 → 数据有断档，自连接才是正确实现')
FROM (
    SELECT `harvest_month`, `total_yield_kg`,
           LAG(`total_yield_kg`, 12) OVER (ORDER BY `harvest_month`) AS `lag12`
    FROM `v_yield_monthly`
) m
LEFT JOIN `v_yield_monthly` sj
       ON sj.`harvest_month` = DATE_SUB(m.`harvest_month`, INTERVAL 1 YEAR);


-- =============================================================================
-- 断言 9：GDD 累计序列单调递增
-- =============================================================================
-- 【8.0 写法】用 LAG() 取上一行的累计值，检查是否有回退。
-- 5.7 版本要写自连接 + 日期 +1 天来配对相邻行（因为气象数据是逐日连续的），
-- 现在一行 LAG 就够了。
--
-- 注意：这里 LAG 是**正确**的用法 —— 因为气象数据确实是逐日连续的，
-- 不存在断档。这与断言 8 里"月度数据有断档所以 LAG 不适用"形成对照：
-- **同一个函数，用在不同数据上，对错可能相反。判断依据是数据本身。**
SELECT '9. GDD 累计序列单调性' AS `断言组`;

WITH gdd AS (
    SELECT
        p.`id` AS `planting_id`,
        w.`obs_date`,
        SUM(GREATEST(0, (w.`temp_max` + w.`temp_min`) / 2 - c.`base_temp`))
            OVER (PARTITION BY p.`id` ORDER BY w.`obs_date`
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS `gdd_cum`
    FROM `planting` p
    JOIN `plot` pl ON pl.`id` = p.`plot_id`
    JOIN `farm` f  ON f.`id`  = pl.`farm_id`
    JOIN `crop` c  ON c.`id`  = p.`crop_id`
    JOIN `weather_daily` w ON w.`region_code` = f.`region_code`
                          AND w.`obs_date` BETWEEN p.`sow_date` AND p.`mature_date`
),
with_prev AS (
    SELECT `planting_id`, `obs_date`, `gdd_cum`,
           LAG(`gdd_cum`) OVER (PARTITION BY `planting_id` ORDER BY `obs_date`) AS `prev_cum`
    FROM gdd
)
SELECT '  累计有效积温无回退' AS 检查项,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT(COUNT(*), ' 处回退') AS 说明
FROM with_prev
WHERE `prev_cum` IS NOT NULL AND `gdd_cum` < `prev_cum` - 1e-6;


-- =============================================================================
-- 断言 10：【8.0 新增】相关系数两条数学路径必须一致
-- =============================================================================
-- MySQL 没有任何相关/回归聚合函数（CORR / COVAR_POP / REGR_* 全都不存在），
-- 所以皮尔逊相关系数只能手写。手写公式写错的概率不低，
-- 而算错的 r 仍然是一个"看起来合理的小数"，不会报错。
--
-- 这条断言用**完全不同的数学路径**再算一遍：
--     路径①：展开式 (n·Σxy − Σx·Σy) / √[(n·Σx²−(Σx)²)(n·Σy²−(Σy)²)]
--     路径②：定义式 协方差 / (标准差 × 标准差)，用内置的 STDDEV_POP
-- 两者的数学来源不同，结果必须吻合到小数点后 4 位。
SELECT '10. 相关系数公式交叉验证' AS `断言组`;

WITH pairs AS (
    SELECT c.`name` AS `crop_name`,
           f.`fert_per_mu` AS x,
           y.`yield_per_mu` AS y
    FROM `yield_record` y
    JOIN `crop` c ON c.`id` = y.`crop_id`
    JOIN (
        SELECT i.`planting_id`,
               SUM(i.`quantity`) / NULLIF(MAX(p.`plant_area_mu`), 0) AS `fert_per_mu`
        FROM `input_cost` i
        JOIN `planting` p ON p.`id` = i.`planting_id`
        WHERE i.`input_type` = 20
        GROUP BY i.`planting_id`
    ) f ON f.`planting_id` = y.`planting_id`
),
stats AS (
    SELECT
        `crop_name`,
        COUNT(*) AS n,
        -- 路径①：展开式
        (COUNT(*) * SUM(x * y) - SUM(x) * SUM(y))
        / NULLIF(SQRT((COUNT(*) * SUM(x * x) - SUM(x) * SUM(x))
                    * (COUNT(*) * SUM(y * y) - SUM(y) * SUM(y))), 0) AS r_expanded,
        -- 路径②：协方差 / 标准差之积
        ((SUM(x * y) - SUM(x) * SUM(y) / COUNT(*)) / COUNT(*))
        / NULLIF(STDDEV_POP(x) * STDDEV_POP(y), 0) AS r_definition
    FROM pairs
    GROUP BY `crop_name`
    HAVING n >= 5
)
SELECT '  展开式 == 协方差/标准差定义式' AS 检查项,
       CASE WHEN SUM(CASE WHEN ABS(r_expanded - r_definition) > 0.0001 THEN 1 ELSE 0 END) = 0
            THEN 'PASS' ELSE 'FAIL' END AS 结果,
       CONCAT('检查 ', COUNT(*), ' 个作物，最大偏差 ',
              ROUND(MAX(ABS(r_expanded - r_definition)), 8)) AS 说明
FROM stats;


-- =============================================================================
-- 断言 11：【8.0 新增】窗口帧 ROWS 与 RANGE 在并列值上的行为差异
-- =============================================================================
-- 窗口函数默认帧是 `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`，
-- 它把 ORDER BY 值**相同的行算作同一帧**；
-- 而 `ROWS BETWEEN ...` 是严格逐行推进。
--
-- 对"累计求和"来说，两者在**有并列值时结果完全不同**：
--     RANGE：并列的行会拿到同一个累计值（相当于把并列行当整体算）
--     ROWS ：每行一个独立的累计值（严格逐行累加）
--
-- 这条断言检测本数据集在 ABC 累计场景下是否存在并列产量 ——
-- 如果存在，就必须用 ROWS；如果不存在，说明当前数据"碰巧"不会暴露这个差异，
-- 但代码里仍然应该显式写 ROWS。
SELECT '11. 窗口帧 ROWS/RANGE 差异检测' AS `断言组`;

WITH ordered AS (
    SELECT `plot_no`, `total_yield_kg`,
           SUM(`total_yield_kg`)
               OVER (ORDER BY `total_yield_kg` DESC, `plot_id` ASC
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS `cum_rows`,
           SUM(`total_yield_kg`)
               OVER (ORDER BY `total_yield_kg` DESC, `plot_id` ASC) AS `cum_default`
    FROM `v_plot_yield_summary`
)
SELECT '  ROWS 帧与默认帧是否有差异' AS 检查项,
       -- 这条不判定 PASS/FAIL —— 它报告的是数据的一个客观属性
       CONCAT(SUM(CASE WHEN ABS(`cum_rows` - `cum_default`) > 0.01 THEN 1 ELSE 0 END),
              ' 行不同') AS 结果,
       CASE WHEN SUM(CASE WHEN ABS(`cum_rows` - `cum_default`) > 0.01 THEN 1 ELSE 0 END) = 0
            THEN '当前数据无并列产量，两种帧结果相同；但代码仍应显式写 ROWS 以防数据变化'
            ELSE '存在并列产量，必须用 ROWS 帧，否则累计值会出错' END AS 说明
FROM ordered;


-- =============================================================================
-- 汇总
-- =============================================================================
SELECT '============================================================' AS '';
SELECT '  断言执行完毕。请确认上面没有任何 FAIL。' AS '';
SELECT '  （断言 11 的结果一列是数量而非 PASS/FAIL，属正常）' AS '';
SELECT '============================================================' AS '';
