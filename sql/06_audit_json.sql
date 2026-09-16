-- =============================================================================
--  JSON_TABLE：把审计日志的变更快照展开成"字段级"的行
--  MySQL 8.0
-- =============================================================================
--
--  【这个文件要解决的实际问题】
--  audit_log.detail 存的是 JSON：
--      {"before":{"area_mu":"210.50","fertility_level":"2"},
--       "after": {"area_mu":"260.00","fertility_level":"1"},
--       "changed_fields":"area_mu,fertility_level"}
--
--  在 MySQL 5.7 里，这个 JSON **只能整块读出来给应用层解析** ——
--  5.7 有 JSON 类型、有 JSON_EXTRACT，但**没有 JSON_TABLE**，
--  也就是说它无法把 JSON 里的键展开成"行"。
--  所以 5.7 时代的设计原则是：JSON 只当"不可查询的附件"，
--  任何要检索的字段必须抽成独立列（本项目用 STORED 生成列 changed_fields 做了这件事）。
--
--  8.0 有了 JSON_TABLE 之后，这个限制被打破：
--  可以在 SQL 里直接把 JSON 对象的每个键变成一行，
--  于是"这次修改动了哪几个字段、每个字段从什么变成了什么"
--  变成了一条普通的 SELECT。
--
--  【本节用到的新东西】
--    JSON_TABLE(expr, path COLUMNS(...))  —— 把 JSON 转成关系表
--    JSON_KEYS(json_doc, path)            —— 取出对象的所有键名
--    ->> 运算符                            —— JSON_EXTRACT + JSON_UNQUOTE 的简写
--
--  ⚠️ 一个实测踩到的限制：**`->>` 的路径必须是字符串字面量**，
--     不能是表达式。下面这样写会直接语法报错：
--         a.detail ->> CONCAT('$.before.', jt.field_name)      -- ❌ ERROR 1064
--     因为这里的路径是**动态拼接**出来的（字段名来自 JSON_KEYS），
--     所以必须退回完整写法：
--         JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.before.', jt.field_name)))  -- ✅
--     `->>` 只是"路径已知且为常量"时的语法糖，不是通用替代。
-- =============================================================================

USE `smart_agri`;


-- #############################################################################
-- 1. 字段级变更明细（核心查询）
-- #############################################################################
--
-- 【思路拆解】
--   难点在于 detail 里的字段名是**动态的**（改地块是 name/area_mu，
--   改批次是 status），不可能写死路径。
--
--   解法是分两步：
--     ① JSON_KEYS(detail, '$.before') 拿到这次修改涉及的所有字段名，
--        得到一个 JSON 数组 ["area_mu","fertility_level",...]
--     ② JSON_TABLE 把这个数组展开成 N 行（每行一个字段名）
--     ③ 再用 CONCAT 拼出 '$.before.<字段名>' 这样的路径，逐个取值
--
--   这是 JSON_TABLE 最典型的用法：**动态键名的横向展开**。
SELECT
    a.`id`                                      AS 日志ID,
    a.`created_at`                              AS 操作时间,
    u.`real_name`                               AS 操作人,
    a.`action`                                  AS 动作,
    a.`target_table`                            AS 目标表,
    a.`target_id`                               AS 目标ID,
    jt.`field_name`                             AS 变更字段,
    -- ⚠️ 路径是动态拼接的，不能用 ->> 语法糖，必须写全
    JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.before.', jt.`field_name`))) AS 变更前,
    JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.after.',  jt.`field_name`))) AS 变更后,
    -- 判断这个字段到底有没有真的变
    CASE
        WHEN (JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.before.', jt.`field_name`))))
           <=> (JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.after.', jt.`field_name`))))
        THEN '未变化'
        ELSE '已变更'
    END                                         AS 是否实际变更,
    a.`ip`                                      AS 来源IP
FROM `audit_log` a
LEFT JOIN `user` u ON u.`id` = a.`user_id`
-- JSON_TABLE 在这里扮演"行生成器"：把 before 对象的键展开成多行
JOIN JSON_TABLE(
         JSON_KEYS(a.`detail`, '$.before'),      -- 动态取出所有字段名
         '$[*]'                                  -- 遍历数组的每个元素
         COLUMNS (
             `field_name` VARCHAR(64) PATH '$'   -- 每个元素就是字段名本身
         )
     ) AS jt
WHERE a.`detail` IS NOT NULL
  AND JSON_VALID(a.`detail`)
ORDER BY a.`id` DESC, jt.`field_name`;


-- #############################################################################
-- 2. 只看"真正发生了变化"的字段
-- #############################################################################
-- 审计快照里可能包含未被修改的字段（应用层为了简单，把整行都存了进去）。
-- 用 <=> 做 NULL 安全的比较：普通 `=` 在任一侧为 NULL 时返回 NULL，
-- 会被当成"没变"，而 <=> 能正确判断"两边都是 NULL"这种情况。
SELECT
    a.`id`                                      AS 日志ID,
    a.`target_table`                            AS 目标表,
    a.`target_id`                               AS 目标ID,
    jt.`field_name`                             AS 变更字段,
    JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.before.', jt.`field_name`))) AS 变更前,
    JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.after.',  jt.`field_name`))) AS 变更后,
    CASE jt.`field_name`                        -- 把字段名翻译成中文，便于阅读
        WHEN 'name'            THEN '地块名称'
        WHEN 'area_mu'         THEN '面积(亩)'
        WHEN 'fertility_level' THEN '地力等级'
        WHEN 'irrigation_type' THEN '灌溉方式'
        WHEN 'soil_type'       THEN '土壤类型'
        WHEN 'status'          THEN '状态'
        WHEN 'remark'          THEN '备注'
        WHEN 'yield_kg'        THEN '产量(kg)'
        ELSE jt.`field_name`
    END                                         AS 字段说明
FROM `audit_log` a
JOIN JSON_TABLE(
         JSON_KEYS(a.`detail`, '$.before'), '$[*]'
         COLUMNS (`field_name` VARCHAR(64) PATH '$')
     ) AS jt
WHERE a.`detail` IS NOT NULL
  AND NOT ((JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.before.', jt.`field_name`))))
        <=> (JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.after.',  jt.`field_name`)))))
ORDER BY a.`id` DESC, jt.`field_name`;


-- #############################################################################
-- 3. 统计"哪些字段最常被修改"
-- #############################################################################
-- 这是 5.7 时代**做不到**的分析 —— 需要先把 JSON 展开成行才能分组计数。
-- 业务价值：如果某个字段被频繁修改，往往说明录入环节有问题。
WITH field_changes AS (
    SELECT
        a.`target_table`,
        jt.`field_name`
    FROM `audit_log` a
    JOIN JSON_TABLE(
             JSON_KEYS(a.`detail`, '$.before'), '$[*]'
             COLUMNS (`field_name` VARCHAR(64) PATH '$')
         ) AS jt
    WHERE a.`detail` IS NOT NULL
      AND NOT ((JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.before.', jt.`field_name`))))
            <=> (JSON_UNQUOTE(JSON_EXTRACT(a.`detail`, CONCAT('$.after.',  jt.`field_name`)))))
)
SELECT
    `target_table`                                          AS 目标表,
    `field_name`                                            AS 字段,
    COUNT(*)                                                AS 修改次数,
    -- 窗口函数算该字段在所属表内的修改频次排名
    RANK() OVER (PARTITION BY `target_table`
                 ORDER BY COUNT(*) DESC, `field_name`)      AS 表内排名,
    -- 占本表全部字段修改的比例
    ROUND(COUNT(*) * 100.0
          / SUM(COUNT(*)) OVER (PARTITION BY `target_table`), 1) AS 占比_pct
FROM field_changes
GROUP BY `target_table`, `field_name`
ORDER BY 目标表, 修改次数 DESC;


-- #############################################################################
-- 4. 用 JSON_TABLE 做"行转列"：把某条记录的变更摊平成一行
-- #############################################################################
-- 场景：审计详情页要显示"这次修改：名称 A→B，面积 100→200"。
-- 用条件聚合把它压成一行，前端直接渲染，不用在应用层拼。
SELECT
    a.`id`                                                      AS 日志ID,
    a.`target_table`                                            AS 目标表,
    MAX(CASE WHEN jt.`field_name` = 'name'
             THEN CONCAT(a.`detail` ->> '$.before.name', ' → ',
                         a.`detail` ->> '$.after.name') END)        AS 名称变更,
    MAX(CASE WHEN jt.`field_name` = 'area_mu'
             THEN CONCAT(a.`detail` ->> '$.before.area_mu', ' → ',
                         a.`detail` ->> '$.after.area_mu', ' 亩') END) AS 面积变更,
    MAX(CASE WHEN jt.`field_name` = 'fertility_level'
             THEN CONCAT(a.`detail` ->> '$.before.fertility_level', ' → ',
                         a.`detail` ->> '$.after.fertility_level') END) AS 地力等级变更,
    COUNT(*)                                                    AS 涉及字段数
FROM `audit_log` a
JOIN JSON_TABLE(
         JSON_KEYS(a.`detail`, '$.before'), '$[*]'
         COLUMNS (`field_name` VARCHAR(64) PATH '$')
     ) AS jt
WHERE a.`detail` IS NOT NULL
GROUP BY a.`id`, a.`target_table`
ORDER BY a.`id` DESC;


-- #############################################################################
-- 5. 对比：同一件事在 5.7 里只能怎么做
-- #############################################################################
-- 【5.7 的处境】
--   5.7 有 JSON 类型和 JSON_EXTRACT，但**没有 JSON_TABLE**，
--   无法把 JSON 展开成行。所以要实现上面这些查询，只能：
--     a) 把整条 detail 读到应用层，用 Python 解析后再处理 ——
--        失去了在数据库里做聚合/排序/分页的能力；
--     b) 或者提前把 JSON 里的关键字段抽成独立列。
--        本项目在 5.7 时代用的就是这个方案：用 STORED 生成列
--        changed_fields 把"被修改的字段列表"从 JSON 里抽出来并建索引，
--        但**只能做"包含某个字段"的模糊匹配，无法做字段级的值比较**。
--
-- 【8.0 的 JSON_TABLE】
--   把 JSON 当成了真正的关系型数据源，可以直接 JOIN、GROUP BY、
--   参与窗口函数计算。审计日志从"只能读不能查"变成了"可分析的数据资产"。
--
-- ⚠️ 但有一条设计原则**没有变**：
--    JSON_TABLE 是**运行时展开**，每次查询都要解析 JSON，
--    无法建立索引。所以：
--      · 需要**高频过滤/排序**的字段，仍然应该抽成独立列（甚至生成列）
--      · JSON_TABLE 适合"偶尔做一次的分析型查询"，不适合高频点查
--    换句话说，JSON_TABLE 让 JSON 变得可查了，但**没有让它变得高效**。
--    这个区分很重要 —— 别因为有了 JSON_TABLE 就把所有字段都塞进 JSON。


-- =============================================================================
-- 执行完成
-- =============================================================================
SELECT '06_audit_json.sql 执行完成（JSON_TABLE 审计分析）' AS msg;
SELECT COUNT(*) AS 可解析的审计记录数
FROM `audit_log` WHERE `detail` IS NOT NULL AND JSON_VALID(`detail`);
