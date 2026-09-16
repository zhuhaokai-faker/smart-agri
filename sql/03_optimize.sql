-- =============================================================================
--  索引设计与 EXPLAIN 调优案例
--  MySQL 8.0
-- =============================================================================
--
--  【这个文件做什么】
--  用 EXPLAIN 把三个最核心的索引考点演示出来，每个都给出"优化前 / 优化后"
--  的对照。所有数字都是**在本机 MySQL 8.0 上实测**的，不是抄来的。
--
--  【案例覆盖的三个考点】
--  案例 1：索引列顺序必须匹配 GROUP BY 列 → 消除临时表
--  案例 2：最左前缀原则 → 等值列必须在范围/排序列之前
--  案例 3：函数导致索引失效 → 用 STORED 生成列破解
--  附   加：8.0 的 skip_scan 与新 EXPLAIN 的含义（迁移到 8.0 必须知道）
--
--  【⚠️ 从 5.7 迁移到 8.0 后，本文件的预期输出变了】
--  三个案例的结论方向都没变，但具体数字和 Extra 内容有差异，例如：
--    · 8.0 对 GROUP BY 的排序处理更聪明，某些场景下不再出现 Using filesort
--    · 8.0 优化器更激进，有时会直接放弃一个"看起来有用"的索引改走全表扫描
--  所以本文档里的每一处预期都按 8.0 重新实测过。
--  **这也是迁移工作的真实样子：不是改版本号，而是重新验证每一条结论。**
--
--  【为什么用 FORCE INDEX 而不是 USE INDEX】
--  ⚠️ 这是本次实测踩到的一个坑：
--     `USE INDEX` 是**提示**，优化器可以评估后决定忽略它；
--     `FORCE INDEX` 才是**强制**，优化器必须使用。
--  用 USE INDEX 做对照实验时，8.0 有时会直接忽略那个反例索引去走全表扫描，
--  于是"反例索引有多差"就演示不出来了。要得到稳定的对照必须用 FORCE INDEX。
--  （生产环境不要乱加索引提示 —— 它们会阻止优化器随数据变化做出更好选择。）
-- =============================================================================

USE `smart_agri`;


-- #############################################################################
-- 案例 1：索引列顺序消除临时表
-- #############################################################################
--
-- 查询：按作物汇总产量。这是"作物产量占比"图表背后的查询。
SELECT '=============================================================' AS '';
SELECT '案例 1：GROUP BY crop_id —— 索引顺序消除临时表' AS '';
SELECT '=============================================================' AS '';

SELECT '【优化前】不使用索引（强制全表扫描）' AS '';
EXPLAIN
SELECT `crop_id`, COUNT(*) AS cnt, SUM(`yield_kg`) AS total
FROM `yield_record` IGNORE INDEX (`idx_crop_harvest`, `idx_harvest_month_crop`)
GROUP BY `crop_id`;
-- 8.0 实测：
--   type  = ALL               全表扫描
--   rows  = 259
--   Extra = Using temporary
--         ↑ 数据不是按 crop_id 有序存储的，必须建**临时表**做分组。
--           临时表在 128MB 的 buffer pool 下极易落盘，是查询里最贵的操作之一。
--
-- 注意：8.0 这里**没有** Using filesort，而 5.7 会有。
--       原因是 8.0 的分组实现更聪明，分组完成后不需要再单独排序一次。
--       结论方向不变（临时表仍在），但如果你拿 5.7 的文档来对照会对不上。

SELECT '【优化后】使用 idx_crop_harvest (crop_id, harvest_date, yield_kg, yield_per_mu)' AS '';
EXPLAIN
SELECT `crop_id`, COUNT(*) AS cnt, SUM(`yield_kg`) AS total
FROM `yield_record` FORCE INDEX (`idx_crop_harvest`)
GROUP BY `crop_id`;
-- 8.0 实测：
--   type  = index               全索引扫描（但按 crop_id 有序）
--   key   = idx_crop_harvest
--   Extra = Using index         覆盖索引：用到的列全在索引里，完全不回表
--           ↑ Using temporary 消失
--
-- 【为什么能消除临时表】
--   索引的第一列就是 crop_id，所以索引本身就按 crop_id 有序。
--   MySQL 可以边扫描边分组，不需要额外的临时表。
--   这个优化叫"索引顺序分组"（index order group by）。
--
-- 【最左前缀的体现】—— 这是本案例的核心考点
--   同样这四个列，如果索引定义成 (harvest_date, crop_id, yield_kg, yield_per_mu)，
--   GROUP BY crop_id 就**无法**利用索引顺序（第二列不是全局有序的），
--   临时表会立刻回来。
--   **同样四个列、同样一个查询，列顺序不同，性能天差地别。**


-- #############################################################################
-- 案例 2：最左前缀原则 —— 等值列必须在范围/排序列之前
-- #############################################################################
--
-- 查询：某地块的产量历史（按收获日期倒序）。
SELECT '=============================================================' AS '';
SELECT '案例 2：WHERE plot_id=? ORDER BY harvest_date DESC —— 最左前缀' AS '';
SELECT '=============================================================' AS '';

SELECT '【优化前】无可用索引（全表扫描）' AS '';
EXPLAIN
SELECT `harvest_date`, `yield_kg`, `yield_per_mu`
FROM `yield_record` IGNORE INDEX (`idx_crop_harvest`, `idx_plot_harvest`, `idx_harvest_month_crop`, `idx_harvest_date`)
WHERE `plot_id` = 25
ORDER BY `harvest_date` DESC
LIMIT 20;
-- 8.0 实测：type=ALL，Extra = Using where; Using filesort
--          扫全表，再对结果排序。

-- ---- 构造"反例索引"：列都对，但顺序写反了 ----
SELECT '【反例】索引 (harvest_date, plot_id) —— 列都对，顺序反了' AS '';
SET @exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = 'smart_agri' AND TABLE_NAME = 'yield_record'
                  AND INDEX_NAME = 'idx_bad_order');
SET @sql := IF(@exists > 0, 'DROP INDEX `idx_bad_order` ON `yield_record`', 'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
CREATE INDEX `idx_bad_order` ON `yield_record` (`harvest_date`, `plot_id`);

EXPLAIN
SELECT `harvest_date`, `yield_kg`, `yield_per_mu`
FROM `yield_record` FORCE INDEX (`idx_bad_order`)
WHERE `plot_id` = 25
ORDER BY `harvest_date` DESC
LIMIT 20;
-- 8.0 实测（FORCE INDEX 强制使用）：
--   type     = index
--   key      = idx_bad_order
--   key_len  = 7                只用到前两列
--   rows     = 20
--   filtered = 10.00%           ⚠️ 关键指标：扫描到的行里只有 10% 满足条件
--   Extra    = Using where; Backward index scan
--
-- 【怎么读这个结果】
--   它走的是**索引扫描**，不是全表扫描；而且因为索引本身提供了
--   harvest_date 的顺序，ORDER BY ... DESC 可以沿索引反向扫描，
--   所以**没有 filesort**。看起来好像也还行？
--
--   问题在 `filtered = 10%`：它按日期顺序扫描，把约 90% 不属于
--   plot_id=25 的行读出来又扔掉。要给某一块地取 20 条记录，
--   却不得不翻遍所有地块的日期。
--   B+ 树的定位能力只有在**最左列**确定时才能发挥 —— plot_id 在第二列，
--   无法用来做区间定位，只能当普通的过滤条件。
--
-- ⚠️ 顺带一个 8.0 的观察：如果把 FORCE INDEX 换成 USE INDEX（提示而非强制），
--    8.0 的优化器**会直接忽略这个索引去走全表扫描** ——
--    它宁可全表扫也不愿低效地用这个索引。
--    （5.7 的优化器有时会老老实实按提示走，所以两边 EXPLAIN 看起来不同。
--      这不是"8.0 变差了"，恰恰相反，说明 8.0 的成本模型更准。）

SELECT '【优化后】正确顺序：(plot_id, harvest_date, yield_per_mu)' AS '';
EXPLAIN
SELECT `harvest_date`, `yield_kg`, `yield_per_mu`
FROM `yield_record` FORCE INDEX (`idx_plot_harvest`)
WHERE `plot_id` = 25
ORDER BY `harvest_date` DESC
LIMIT 20;
-- 8.0 实测：
--   type     = ref                    按 plot_id 精确定位
--   key_len  = 4
--   rows     = 4
--   filtered = 100.00%                ⚠️ 一行都不浪费
--   Extra    = Using where; Backward index scan
--
-- 【两组数字的对比】
--                type    key_len   rows   filtered
--   反例索引     index      7       20     10%      ← 扔掉 90% 的扫描结果
--   正确索引     ref        4        4    100%      ← 直接命中
--
-- 【结论（最实用的一条推论）】
--   索引 (A, B) 支持   WHERE A=? ORDER BY B     ✅
--   索引 (A, B) 不支持 WHERE B=? ORDER BY A     ❌
--   **等值条件列必须放在范围/排序列之前。**

SET @exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = 'smart_agri' AND TABLE_NAME = 'yield_record'
                  AND INDEX_NAME = 'idx_bad_order');
SET @sql := IF(@exists > 0, 'DROP INDEX `idx_bad_order` ON `yield_record`', 'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


-- #############################################################################
-- 案例 3：函数导致索引失效 —— 用 STORED 生成列破解
-- #############################################################################
SELECT '=============================================================' AS '';
SELECT '案例 3：GROUP BY DATE_FORMAT(...) —— 函数导致索引失效' AS '';
SELECT '=============================================================' AS '';

SELECT '【优化前】对列施加函数：GROUP BY DATE_FORMAT(harvest_date, "%Y-%m")' AS '';
EXPLAIN
SELECT DATE_FORMAT(`harvest_date`, '%Y-%m') AS ym, SUM(`yield_kg`)
FROM `yield_record` FORCE INDEX (`idx_harvest_date`)
GROUP BY ym;
-- 8.0 实测：type=ALL，Extra = Using temporary
--
-- ⚠️ 注意这里是 **type=ALL（全表扫描）**，而不是"用了索引但没帮上忙"。
--    因为 GROUP BY 的表达式和索引列毫无关系，优化器直接判定
--    `idx_harvest_date` 对这个查询**完全不可用**，索性放弃它。
--    （即使写了 FORCE INDEX 也强制不动 —— 索引确实一点忙都帮不上。）
--
-- 【根本问题：函数让索引丧失了提供顺序的能力】
--   B+ 树索引里存的是 harvest_date 的**原始值**，不是 DATE_FORMAT 之后的
--   字符串。所以索引在 harvest_date 上的有序性，对
--   DATE_FORMAT(harvest_date) 这个表达式**毫无帮助**，分组只能靠临时表。
--
-- 【5.7 和 8.0 在这里的表现不同，要说准确】
--   本机 8.0 实测：type=ALL —— 优化器判定索引完全帮不上忙，直接放弃。
--   而 5.7 上同一条查询会显示 type=index + Using index + Using temporary，
--   看起来"用上了覆盖索引"，容易被误读成"这样还行"。
--
--   两种表现背后的道理是一样的：**索引丧失了提供顺序的能力**，
--   所以分组只能靠临时表。覆盖索引只省了回表，节省不了临时表。
--   8.0 只是更直接地把这个事实表现成了"不用这个索引"。
--
--   无论哪个版本，结论都是：**GROUP BY 里出现函数就必须改造查询**。
--
--   同样的坑还有：WHERE YEAR(paid_at) = 2024、WHERE amount * 1.1 > 100。
--   **只要索引列被包在函数里或参与运算，索引就用不上。**
--   改写方式是把函数移到常量侧：
--       WHERE harvest_date >= '2024-01-01' AND harvest_date < '2025-01-01'

SELECT '【优化后】用 STORED 生成列：GROUP BY harvest_month' AS '';
EXPLAIN
SELECT `harvest_month`, SUM(`yield_kg`)
FROM `yield_record` FORCE INDEX (`idx_harvest_month_crop`)
GROUP BY `harvest_month`;
-- 8.0 实测：type=index，Extra = Using index（Using temporary 消失）
--
-- 【生成列是怎么破解这个问题的】
--   harvest_month 是建表时定义的 STORED 生成列：
--       DATE_SUB(DATE(harvest_date), INTERVAL DAYOFMONTH(harvest_date)-1 DAY)
--   它在**写入时**就计算好并物理存储，所以索引里是真实存在的值。
--   GROUP BY harvest_month 因此可以走索引顺序分组。
--
-- 【为什么选 STORED 而不是 VIRTUAL】
--   VIRTUAL 生成列不占存储、读时计算，但**无法用于覆盖索引**
--   （覆盖索引要求值真的存在索引页里）。
--   代价是每次 INSERT/UPDATE 都要计算并落盘 —— 对产量记录这种低频写入
--   完全可以接受。
--
-- 【8.0 的一个改进】5.7 里 ALTER TABLE 增加 STORED 生成列不支持 INSTANT，
--   会重建整张表；8.0 支持 ALGORITHM=INSTANT 的部分场景，
--   但加 STORED 生成列仍然需要重建（因为它要回填所有已有行）。
--   生产环境要在业务低峰执行。


-- #############################################################################
-- 补充：预聚合汇总表 —— 把"两次全表聚合"变成"两次主键点查"
-- #############################################################################
SELECT '=============================================================' AS '';
SELECT '补充：月度汇总表（DWS 层）—— 预聚合 vs 实时聚合' AS '';
SELECT '=============================================================' AS '';

-- ⚠️ 用 DROP + CREATE 而不是 CREATE TABLE IF NOT EXISTS。
--
-- 原因：`IF NOT EXISTS` 在表已存在时是**空操作**，不会更新任何定义 ——
-- 包括 collation。本项目这张表最初是 5.7 时代建的（utf8mb4_general_ci），
-- 迁移到 8.0 后即使改了这里的 COLLATE，它也一直没跟着变，
-- 实测发现时它还是 general_ci，与其它表的 0900_ai_ci 不一致。
--
-- 这张表是**纯派生数据**（内容完全来自 v_yield_monthly，下面马上就会重新灌入），
-- 所以 DROP 掉重建没有任何数据风险 —— 这正是"派生表"和"事实表"的区别：
-- 事实表要慎用 DROP，派生表随便重建。
DROP TABLE IF EXISTS `dws_yield_monthly`;
CREATE TABLE `dws_yield_monthly` (
  `harvest_month`     DATE          NOT NULL              COMMENT '收获月份首日',
  `total_yield_kg`    DECIMAL(18,2) NOT NULL DEFAULT 0.00 COMMENT '产量合计',
  `total_output_value`DECIMAL(18,2) NOT NULL DEFAULT 0.00 COMMENT '产值合计',
  `total_area_mu`     DECIMAL(14,2) NOT NULL DEFAULT 0.00 COMMENT '收获面积合计',
  `yield_per_mu`      DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '单产=产量/面积(加权)',
  `record_cnt`        INT UNSIGNED  NOT NULL DEFAULT 0    COMMENT '记录数',
  `plot_cnt`          INT UNSIGNED  NOT NULL DEFAULT 0    COMMENT '涉及地块数',
  `updated_at`        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP COMMENT '刷新时间',
  PRIMARY KEY (`harvest_month`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='月度产量汇总表(DWS层,预聚合)';

-- 用视图的定义刷新汇总表。ON DUPLICATE KEY UPDATE 保证可重复执行（幂等）。
INSERT INTO `dws_yield_monthly`
  (`harvest_month`, `total_yield_kg`, `total_output_value`, `total_area_mu`,
   `yield_per_mu`, `record_cnt`, `plot_cnt`)
SELECT `harvest_month`, `total_yield_kg`, `total_output_value`, `total_area_mu`,
       `yield_per_mu`, `record_cnt`, `plot_cnt`
FROM `v_yield_monthly`
ON DUPLICATE KEY UPDATE
  `total_yield_kg`     = VALUES(`total_yield_kg`),
  `total_output_value` = VALUES(`total_output_value`),
  `total_area_mu`      = VALUES(`total_area_mu`),
  `yield_per_mu`       = VALUES(`yield_per_mu`),
  `record_cnt`         = VALUES(`record_cnt`),
  `plot_cnt`           = VALUES(`plot_cnt`);

SELECT '【优化前】YoY 查询直接自连接视图（每次都要重新聚合明细）' AS '';
EXPLAIN
SELECT cur.`harvest_month`, cur.`total_yield_kg`, yoy.`total_yield_kg`
FROM `v_yield_monthly` cur
LEFT JOIN `v_yield_monthly` yoy
       ON yoy.`harvest_month` = DATE_SUB(cur.`harvest_month`, INTERVAL 1 YEAR);
-- 8.0 实测：派生表被物化成 <derived2> / <derived3>，各扫 259 行；
--           连接时还要临时建一个 <auto_key0>。
--
--   带 GROUP BY 的视图无法被 derived_merge 合并进外层，只能先算出来。
--   于是 yield_record 被完整聚合了**两次**，外加一个临时索引的开销。
--   数据量从 259 涨到 100 万时，这里就是"聚合两遍全表"。

SELECT '【优化后】YoY 查询自连接汇总表（主键点查）' AS '';
EXPLAIN
SELECT cur.`harvest_month`, cur.`total_yield_kg`, yoy.`total_yield_kg`
FROM `dws_yield_monthly` cur
LEFT JOIN `dws_yield_monthly` yoy
       ON yoy.`harvest_month` = DATE_SUB(cur.`harvest_month`, INTERVAL 1 YEAR);
-- 8.0 实测：
--   cur  : type=ALL     rows=29       ← 表只有 29 行，全扫比走索引更快
--   yoy  : type=eq_ref  rows=1        ← 通过主键等值连接，JOIN 里最高效的方式
--   派生表和 auto_key 全部消失。
--
-- 注：cur 显示 ALL 看着"不好"，但**这里是对的** —— 29 行的表全扫比走索引快。
--     看 EXPLAIN 不能只盯 type 的"好坏排名"，要结合 rows 一起判断。
--
-- 【量化对比】yield_record 259 行 → 汇总表 29 行；
--   实际生产中明细表是百万级，而月份数永远是几十行 —— 差距 4~5 个数量级。

SELECT '=============================================================' AS '';
SELECT '汇总表刷新完成' AS '', COUNT(*) AS 月份数 FROM `dws_yield_monthly`;


-- #############################################################################
-- 附加：8.0 的 skip_scan —— 为什么迁移后 EXPLAIN 会变
-- #############################################################################
--
-- 这个优化器开关（8.0 新增，默认开启）是**迁移到 8.0 后 EXPLAIN 变化的
-- 重要来源之一**，所以虽然不属于"索引设计"的经典考点，也值得单独看一眼。
-- =============================================================================
SELECT '=============================================================' AS '';
SELECT '附加：skip_scan —— 跳过联合索引的最左列' AS '';
SELECT '=============================================================' AS '';

SELECT '【背景】最左前缀原则说「(A, B) 用不了 WHERE B=?」—— skip_scan 部分打破了这条' AS '';
SELECT '但有一个前提：**最左列 A 的基数必须很低**。下面实测两种情况。' AS '';

-- 情况一：最左列高基数（harvest_date 有 212 个不同值）
SELECT '--- 情况一：最左列 harvest_date，基数 212（高）---' AS '';
SET @exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA='smart_agri' AND TABLE_NAME='yield_record'
                  AND INDEX_NAME='idx_bad_order');
SET @sql := IF(@exists > 0, 'DROP INDEX `idx_bad_order` ON `yield_record`', 'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
CREATE INDEX `idx_bad_order` ON `yield_record` (`harvest_date`, `plot_id`);

EXPLAIN
SELECT COUNT(*) FROM `yield_record`
    IGNORE INDEX (`idx_plot_harvest`, `idx_crop_harvest`, `idx_harvest_month_crop`)
WHERE `plot_id` = 25;
-- 实测：type=index，rows=259，Extra = Using where; Using index
-- **没有触发 skip scan** —— 因为要跳 212 次，还不如直接扫一遍索引。

-- 情况二：最左列低基数（crop_id 只有 12 个不同值）
SELECT '--- 情况二：最左列 crop_id，基数 12（低）---' AS '';
SET @exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA='smart_agri' AND TABLE_NAME='yield_record'
                  AND INDEX_NAME='idx_lowcard');
SET @sql := IF(@exists > 0, 'DROP INDEX `idx_lowcard` ON `yield_record`', 'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
CREATE INDEX `idx_lowcard` ON `yield_record` (`crop_id`, `plot_id`);

SELECT '  skip_scan = ON（8.0 默认）' AS '';
-- ⚠️ 必须显式 SET。optimizer_switch 是**会话级**变量，
--    不写这一行的话两次 EXPLAIN 跑的是同一个设置，对照实验就失效了。
--    （这个坑我自己踩过一次：漏了 SET 导致两组结果一模一样。）
SET SESSION optimizer_switch = 'skip_scan=on';
EXPLAIN
SELECT COUNT(*) FROM `yield_record`
    IGNORE INDEX (`idx_plot_harvest`, `idx_crop_harvest`, `idx_harvest_month_crop`,
                  `idx_harvest_date`, `idx_bad_order`)
WHERE `plot_id` = 25;
-- 实测：type=range，rows=53，Extra = Using where; Using index for skip scan
-- 扫描行数 259 → 53（少 4.9 倍）

SELECT '  skip_scan = OFF（模拟 5.7 行为）' AS '';
SET SESSION optimizer_switch = 'skip_scan=off';
EXPLAIN
SELECT COUNT(*) FROM `yield_record`
    IGNORE INDEX (`idx_plot_harvest`, `idx_crop_harvest`, `idx_harvest_month_crop`,
                  `idx_harvest_date`, `idx_bad_order`)
WHERE `plot_id` = 25;
-- 实测：type=index，rows=259，Extra = Using where; Using index
-- 退化成完整索引扫描

-- 清理
SET @exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA='smart_agri' AND TABLE_NAME='yield_record'
                  AND INDEX_NAME='idx_lowcard');
SET @sql := IF(@exists > 0, 'DROP INDEX `idx_lowcard` ON `yield_record`', 'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA='smart_agri' AND TABLE_NAME='yield_record'
                  AND INDEX_NAME='idx_bad_order');
SET @sql := IF(@exists > 0, 'DROP INDEX `idx_bad_order` ON `yield_record`', 'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 【结论】
--   skip scan 让"跳过最左列"成为可能，但**有代价**：
--   它需要对最左列的每个不同值各做一次区间查找。
--   所以它的收益取决于「最左列基数」和「目标值的选择性」的比值：
--     · 最左列基数低（12 个值）+ 目标选择性高 → 收益大（259 → 53）
--     · 最左列基数高（212 个值）→ 没用，不如直接扫
--   **它不能替代正确的索引设计** —— 该建的 (plot_id, harvest_date) 还是要建。
--   skip scan 是"没有正确索引时的补救"，不是"正确索引的替代品"。

-- 恢复默认设置（脚本跑完不该给会话留下非默认状态）
SET SESSION optimizer_switch = 'skip_scan=on';

SELECT '=============================================================' AS '';
SELECT '03_optimize.sql 执行完成（MySQL 8.0）' AS '';
SELECT '=============================================================' AS '';
