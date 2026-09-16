-- =============================================================================
--  智慧农业种植管理与产量分析平台 — 数据库 Schema
--  MySQL 8.0  |  InnoDB  |  utf8mb4  |  utf8mb4_0900_ai_ci
-- =============================================================================
--
--  【字符集：8.0 之后问题的性质变了，但仍要显式声明】
--  MySQL 5.7 的 character_set_server 默认是 latin1，忘了指定就中文乱码 ——
--  那是"不写就出错"的强制要求。
--  MySQL 8.0 的默认已经是 utf8mb4，所以这一层的坑消失了。
--  但本项目仍然**库、表两级都显式写死 utf8mb4**，理由变成了防御性的：
--    · 不依赖服务端配置，换一台服务器/改一次 my.ini 都不会出问题；
--    · 显式声明让"这张表用什么字符集"成为代码里可读的事实，而不是隐含假设。
--
--  【collation 用 8.0 的 utf8mb4_0900_ai_ci】
--  这是 8.0 的默认 collation，基于 Unicode 9.0，比 5.7 时代常用的
--  utf8mb4_general_ci / utf8mb4_unicode_ci 排序更准确、性能也更好。
--  （那两个老 collation 在 8.0 里仍然可用，只是为了兼容既有数据。）
--
--  关键仍然是**全库统一**：如果 JOIN 两侧 collation 不同，
--  MySQL 需要做隐式转换，会导致索引失效。
--
--  【本文件用到了 8.0 的哪些新能力】
--    · CHECK 约束真正生效（8.0.16+）—— 见文件末尾的约束段。
--      5.7 会**解析 CHECK 但静默忽略**，所以那时只能靠应用层兜底。
--
--  【为什么主键统一用 INT UNSIGNED 而不是 BIGINT】
--  本项目全部表的数据量在十万行以内，INT UNSIGNED 上限 42 亿完全够用。
--  InnoDB 的二级索引叶子节点存的是主键值，主键宽度直接决定每个二级索引的大小。
--  盲目用 BIGINT 会让每个二级索引每行多 4 字节，索引整体膨胀、缓冲区命中率下降。
--  「按数据量选主键宽度」比「统一无脑 BIGINT」更需要判断力。
-- =============================================================================

CREATE DATABASE IF NOT EXISTS `smart_agri`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

-- ⚠️ 这里必须再显式 ALTER 一次，否则库级 collation 可能停留在旧值。
--
-- 原因：`CREATE DATABASE IF NOT EXISTS` 在库**已存在**时是**空操作** ——
-- 它只保证"库存在"，**不会**把已有的 character set / collation 改成新值。
-- 本项目最初的库是 MySQL 5.7 时代用 utf8mb4_general_ci 建的，
-- 迁移到 8.0 后即使改了建库语句，库级 collation 也一直没跟着变 ——
-- 实测发现时它还是 utf8mb4_general_ci。
--
-- 影响：库级 collation 是"新建表不显式指定时继承的默认值"。
-- 本项目所有表都显式写了 collation，所以看起来没出问题；
-- 但只要有一张表漏写（比如手工 CREATE TABLE），它就会继承旧的 general_ci，
-- 与其它表 JOIN 时触发**隐式字符集转换 → 索引失效**。
--
-- ALTER DATABASE 是幂等的：值已经对了也不会报错。
ALTER DATABASE `smart_agri`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE `smart_agri`;

-- 按依赖关系倒序删除，保证可重复执行
DROP TABLE IF EXISTS `audit_log`;
DROP TABLE IF EXISTS `yield_record`;
DROP TABLE IF EXISTS `input_cost`;
DROP TABLE IF EXISTS `farming_log`;
DROP TABLE IF EXISTS `planting`;
DROP TABLE IF EXISTS `weather_daily`;
DROP TABLE IF EXISTS `crop`;
DROP TABLE IF EXISTS `plot`;
DROP TABLE IF EXISTS `farm`;
DROP TABLE IF EXISTS `user`;


-- =============================================================================
-- 1. user — 后台账号 + RBAC
-- =============================================================================
CREATE TABLE `user` (
  `id`            INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户ID',
  `username`      VARCHAR(50)  NOT NULL                COMMENT '登录名',
  `password_hash` VARCHAR(255) NOT NULL                COMMENT '密码哈希(长度随算法和参数变化,不能定长)',
  `real_name`     VARCHAR(50)  NOT NULL DEFAULT ''     COMMENT '姓名',
  `email`         VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '邮箱',
  `phone`         VARCHAR(20)  NOT NULL DEFAULT ''     COMMENT '手机号',
  `role`          TINYINT UNSIGNED NOT NULL DEFAULT 3  COMMENT '角色 1=管理员 2=农艺师 3=只读',
  `status`        TINYINT UNSIGNED NOT NULL DEFAULT 1  COMMENT '状态 1=启用 0=禁用',
  `last_login_at` DATETIME     NULL                    COMMENT '最后登录时间',
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                        ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_username` (`username`),
  KEY `idx_role_status` (`role`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='后台用户表';

-- 【设计说明】
-- 1. password_hash 为什么用 VARCHAR(255) 而**不能**定长（这条是实测踩出来的）：
--    常有人说"bcrypt 哈希固定 60 字符，所以用 CHAR(60)"。这个说法在
--    werkzeug 3.x 上直接导致写入失败：
--      generate_password_hash('admin123')  --> 162 字符  scrypt:32768:8:1$salt$hash
--      generate_password_hash(pw, 'pbkdf2:sha256') --> 103 字符
--    哈希的**算法和参数会随安全策略演进**（werkzeug 默认从 pbkdf2 换成 scrypt，
--    迭代次数也会周期性调高）。把列宽硬编码成某个算法的输出长度，会在某次
--    依赖升级后突然报 "Data too long for column"，且因为只在注册/改密时触发，
--    很容易漏测。用足够宽的 VARCHAR 是对"实现会变"的正确预期。
-- 2. role 用 TINYINT 而不用 ENUM：
--    ENUM 的 ORDER BY / 比较按**内部索引序号**而非字面量，`WHERE role > 2`
--    的语义会随枚举定义顺序漂移，是隐形炸弹。且 TINYINT 也只占 1 字节，
--    ENUM 唯一的"省空间"优势并不存在。
-- 3. idx_role_status 服务"按角色筛选用户列表"。
--    uk_username 既是唯一约束，也是登录查询 `WHERE username = ?` 的索引。


-- =============================================================================
-- 2. farm — 农场/合作社（气象数据的归属维度）
-- =============================================================================
CREATE TABLE `farm` (
  `id`            INT UNSIGNED  NOT NULL AUTO_INCREMENT COMMENT '农场ID',
  `farm_no`       VARCHAR(32)   NOT NULL                COMMENT '农场编号(对外)',
  `name`          VARCHAR(100)  NOT NULL                COMMENT '农场名称',
  `province`      VARCHAR(20)   NOT NULL DEFAULT ''     COMMENT '省份',
  `city`          VARCHAR(20)   NOT NULL DEFAULT ''     COMMENT '城市',
  `county`        VARCHAR(20)   NOT NULL DEFAULT ''     COMMENT '区县',
  `region_code`   VARCHAR(20)   NOT NULL                COMMENT '气象区站号(关联 weather_daily)',
  `longitude`     DECIMAL(10,6) NOT NULL DEFAULT 0      COMMENT '经度',
  `latitude`      DECIMAL(10,6) NOT NULL DEFAULT 0      COMMENT '纬度',
  `total_area_mu` DECIMAL(12,2) NOT NULL DEFAULT 0.00   COMMENT '总面积(亩)',
  `owner_name`    VARCHAR(50)   NOT NULL DEFAULT ''     COMMENT '负责人',
  `contact_phone` VARCHAR(20)   NOT NULL DEFAULT ''     COMMENT '联系电话',
  `status`        TINYINT UNSIGNED NOT NULL DEFAULT 1   COMMENT '状态 1=正常 0=停用',
  `created_at`    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                         ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_farm_no` (`farm_no`),
  KEY `idx_region_code` (`region_code`),
  KEY `idx_province_city` (`province`, `city`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='农场/合作社表';

-- 【设计说明】
-- 1. longitude/latitude 用 DECIMAL(10,6) 而不用 FLOAT/DOUBLE：
--    DECIMAL(10,6) 表示小数点前 4 位、后 6 位，精度约 0.11 米，远超农业地块需求。
--    更重要的是 DECIMAL 是精确值，做地块距离计算和空间聚类时结果可复现；
--    FLOAT 是二进制浮点，同样的输入在不同聚合顺序下可能得到不同的尾数。
-- 2. region_code 是农场到气象数据的连接键。气象是**按地区**的公共维度，
--    多个农场/地块共享同一套气象数据，所以它放在 farm 而不是 plot。
-- 3. idx_province_city 服务"按地区统计"的维度下钻。


-- =============================================================================
-- 3. plot — 地块（系统的核心实体）
-- =============================================================================
CREATE TABLE `plot` (
  `id`               INT UNSIGNED  NOT NULL AUTO_INCREMENT COMMENT '地块ID',
  `plot_no`          VARCHAR(32)   NOT NULL                COMMENT '地块编号(对外)',
  `name`             VARCHAR(100)  NOT NULL                COMMENT '地块名称',
  `farm_id`          INT UNSIGNED  NOT NULL                COMMENT '所属农场',
  `area_mu`          DECIMAL(10,2) NOT NULL DEFAULT 0.00   COMMENT '面积(亩)',
  `soil_type`        TINYINT UNSIGNED NOT NULL DEFAULT 1   COMMENT '土壤类型 1=壤土 2=黏土 3=砂土 4=砂壤土',
  `irrigation_type`  TINYINT UNSIGNED NOT NULL DEFAULT 1   COMMENT '灌溉方式 1=雨养 2=漫灌 3=喷灌 4=滴灌',
  `fertility_level`  TINYINT UNSIGNED NOT NULL DEFAULT 2   COMMENT '肥力等级 1=优 2=中 3=差（用于产量建模分层）',
  `longitude`        DECIMAL(10,6) NOT NULL DEFAULT 0      COMMENT '中心经度',
  `latitude`         DECIMAL(10,6) NOT NULL DEFAULT 0      COMMENT '中心纬度',
  `altitude_m`       SMALLINT UNSIGNED NOT NULL DEFAULT 0  COMMENT '海拔(米)',
  `status`           TINYINT UNSIGNED NOT NULL DEFAULT 1   COMMENT '状态 1=在耕 0=闲置',
  `remark`           VARCHAR(255)  NOT NULL DEFAULT ''     COMMENT '备注',
  `created_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                            ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_plot_no` (`plot_no`),
  KEY `idx_farm_status` (`farm_id`, `status`),
  KEY `idx_fertility` (`fertility_level`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='地块表';

-- 【设计说明】
-- 1. fertility_level 是**产量建模的分层依据**。造数时用它生成"优/中/差"三类
--    基础单产（差 30~50%），否则所有地块单产一样，TopN 排行和 ABC 分析
--    都没有区分度。把"肥力等级"做成数据而不是硬编码，是领域建模的体现。
-- 2. idx_farm_status：服务"某农场下的在耕地块"列表，farm_id 等值在前、
--    status 过滤在后，符合最左前缀原则。
-- 3. 外键约束见下方 ALTER（为保持建表顺序清晰，外键统一收敛到文件末尾）。


-- =============================================================================
-- 4. crop — 作物品种（含作物生理参数，GDD 计算的基础）
-- =============================================================================
CREATE TABLE `crop` (
  `id`            INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '作物ID',
  `crop_code`     VARCHAR(32)  NOT NULL                COMMENT '作物编码',
  `name`          VARCHAR(50)  NOT NULL                COMMENT '作物名称',
  `variety`       VARCHAR(50)  NOT NULL DEFAULT ''     COMMENT '品种名',
  `category`      TINYINT UNSIGNED NOT NULL DEFAULT 1  COMMENT '类别 1=粮食 2=蔬菜 3=水果 4=经济作物',
  `base_temp`     DECIMAL(5,2) NOT NULL                COMMENT '生物学零度(℃)，低于此温度不积累有效积温',
  `gdd_maturity`  DECIMAL(8,2) NOT NULL                COMMENT '从播种到成熟所需有效积温(℃·d)',
  `growth_days`   SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '理论生育期天数',
  `status`        TINYINT UNSIGNED NOT NULL DEFAULT 1  COMMENT '状态 1=启用 0=停用',
  `created_at`    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                           ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_crop_code` (`crop_code`),
  KEY `idx_category` (`category`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='作物品种表';

-- 【设计说明】—— 这张表是本项目与普通 CRUD 项目的分水岭
-- 1. base_temp 和 gdd_maturity 把**作物生理参数沉淀成数据**，而不是硬编码在
--    Python 里。于是"有效积温是否达到成熟"这个判断可以完全在 SQL 里完成：
--        SELECT SUM(GREATEST(0, (t.temp_max + t.temp_min)/2 - c.base_temp)) AS gdd
--        ...
--        HAVING gdd >= c.gdd_maturity
--    改作物品种不用改代码，这是领域驱动设计的直接收益。
-- 2. 不同作物的 base_temp 差异很大（小麦 0℃、玉米 10℃、水稻 10~12℃），
--    gdd_maturity 也差几倍。如果写成常量，多作物支持就无从谈起。
-- 3. DECIMAL 而非 INT：base_temp 可能是 10.5℃ 这种半度值。


-- =============================================================================
-- 5. weather_daily — 逐日气象（按气象区，多地块共享）
-- =============================================================================
CREATE TABLE `weather_daily` (
  `id`               INT UNSIGNED  NOT NULL AUTO_INCREMENT COMMENT '记录ID',
  `region_code`      VARCHAR(20)   NOT NULL                COMMENT '气象区站号',
  `obs_date`         DATE          NOT NULL                COMMENT '观测日期',
  `obs_month`        DATE GENERATED ALWAYS AS
                       (DATE_SUB(DATE(`obs_date`), INTERVAL DAYOFMONTH(`obs_date`) - 1 DAY)) STORED
                                                           COMMENT '观测月份首日(生成列,供月度聚合走索引)',
  `temp_max`         DECIMAL(5,2)  NOT NULL                COMMENT '日最高气温(℃)',
  `temp_min`         DECIMAL(5,2)  NOT NULL                COMMENT '日最低气温(℃)',
  `temp_avg`         DECIMAL(5,2)  NOT NULL                COMMENT '日平均气温(℃)',
  `precipitation`    DECIMAL(7,2)  NOT NULL DEFAULT 0.00   COMMENT '日降水量(mm)',
  `sunshine_hours`   DECIMAL(5,2)  NOT NULL DEFAULT 0.00   COMMENT '日照时数(h)',
  `humidity`         DECIMAL(5,2)  NOT NULL DEFAULT 0.00   COMMENT '相对湿度(%)',
  `solar_radiation`  DECIMAL(8,2)  NOT NULL DEFAULT 0.00   COMMENT '太阳辐射(MJ/m²)',
  `created_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_region_date` (`region_code`, `obs_date`),
  KEY `idx_obs_date` (`obs_date`),
  KEY `idx_obs_month_precip` (`obs_month`, `precipitation`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='逐日气象数据表';

-- 【设计说明】
-- 1. 为什么气象独立成表、不挂在 planting 上？
--    气象是**按地区、按日**的公共维度：同一个气象站覆盖范围内的所有地块，
--    共享同一套气温降水数据。若把 temp_max/temp_min 塞进 planting 或 farming_log，
--    每个地块每天都会存一份完全相同的值，几十个地块 × 1095 天 = 数万行纯冗余。
--    更严重的是无法支持"同一地区不同地块在同一气象条件下的产量对比"——
--    因为数据被复制成了互不相干的副本。
-- 2. obs_month 生成列：这是 EXPLAIN 调优案例 3 的落点。
--    `GROUP BY DATE_FORMAT(obs_date,'%Y-%m')` 用不上索引（B+树里存的是
--    obs_date 原始值，不是格式化后的字符串）。用 STORED 生成列把月份首日
--    物化出来并建索引，月度聚合就能走索引。
-- 3. uk_region_date 既是唯一约束（同地区同日只能有一条），也是
--    "某地区某时段气象数据"查询的索引 —— 一个索引两个用途。


-- =============================================================================
-- 6. planting — 种植批次（地块 × 品种 × 茬口，含生育期时间轴）
-- =============================================================================
CREATE TABLE `planting` (
  `id`               INT UNSIGNED  NOT NULL AUTO_INCREMENT COMMENT '批次ID',
  `batch_no`         VARCHAR(40)   NOT NULL                COMMENT '批次编号(对外)',
  `plot_id`          INT UNSIGNED  NOT NULL                COMMENT '地块ID',
  `crop_id`          INT UNSIGNED  NOT NULL                COMMENT '作物品种ID',
  `season`           VARCHAR(20)   NOT NULL                COMMENT '茬口 如 2024春 / 2024秋',
  `season_year`      SMALLINT UNSIGNED NOT NULL            COMMENT '茬口年份(冗余,供分组统计)',
  `season_seq`       SMALLINT UNSIGNED NOT NULL            COMMENT '茬口序号(年份*2+季次)线性递增,供同期群/连续种植判定',
  `plant_area_mu`    DECIMAL(10,2) NOT NULL DEFAULT 0.00   COMMENT '实际种植面积(亩)',
  `density_per_mu`   INT UNSIGNED  NOT NULL DEFAULT 0      COMMENT '种植密度(株/亩)',
  `sow_date`         DATE          NOT NULL                COMMENT '播种日期',
  `emerge_date`      DATE          NULL                    COMMENT '出苗日期',
  `flower_date`      DATE          NULL                    COMMENT '开花日期',
  `mature_date`      DATE          NULL                    COMMENT '成熟日期',
  `harvest_date`     DATE          NULL                    COMMENT '收获日期',
  `status`           TINYINT UNSIGNED NOT NULL DEFAULT 10  COMMENT '状态 10=待播种 20=生长中 30=成熟待收 40=已收获 50=已废弃',
  `agronomist_id`    INT UNSIGNED  NULL                    COMMENT '负责农艺师',
  `remark`           VARCHAR(255)  NOT NULL DEFAULT ''     COMMENT '备注',
  `created_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                            ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_batch_no` (`batch_no`),
  UNIQUE KEY `uk_plot_crop_season` (`plot_id`, `crop_id`, `season`),
  KEY `idx_crop_status` (`crop_id`, `status`),
  KEY `idx_status_sow` (`status`, `sow_date`),
  KEY `idx_plot_seq` (`plot_id`, `season_seq`),
  KEY `idx_sow_date` (`sow_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='种植批次表';

-- 【设计说明】
-- 1. season_seq = 年份*2 + 季次（春=1 夏=2 …），是**线性递增的茬口序号**。
--    为什么需要它？同期群分析要判断"该地块在后续第 N 个茬口是否还在种植"。
--    如果直接用字符串 season（'2024春'）相减毫无意义；用 season_year 相减则
--    丢失了同年内的春/秋茬区别。线性序号让"相邻茬口相差 1"这个前提成立，
--    连续种植段的判定才成立。
--    ⚠️ 这与「连续干旱日数」里的坑是同一个道理：任何"连续第 N 个"的分析，
--       序号必须是**线性递增**的，不能用年月编码这类非连续数字。
-- 2. uk_plot_crop_season 一索引两用：
--    (a) 业务约束：同一地块、同一品种、同一茬口只能有一批种植记录；
--    (b) 该唯一键的最左前缀是 plot_id，**因此不需要再单独建 idx_plot(plot_id)**，
--        外键 fk_planting_plot 也会复用它，省下一个索引的写放大。
-- 3. idx_crop_status 服务"按作物统计种植情况"和分组 TopN（crop_id 在最左）。
-- 4. 状态编号刻意用 10/20/30/40 而非 1/2/3/4：
--    将来若要插入"部分收获 35"这类中间态，`WHERE status BETWEEN 20 AND 40`
--    这类范围写法不会失效，且 status 的大小顺序 = 农事推进顺序。
--    如果初始用 1,2,3,4，插入新状态时只能全表 UPDATE 重排 —— 这是真实生产事故。


-- =============================================================================
-- 7. farming_log — 农事作业记录（谁、何时、在哪块地、干了什么、花了多少工）
-- =============================================================================
CREATE TABLE `farming_log` (
  `id`            INT UNSIGNED  NOT NULL AUTO_INCREMENT COMMENT '记录ID',
  `planting_id`   INT UNSIGNED  NOT NULL                COMMENT '种植批次ID',
  `op_type`       TINYINT UNSIGNED NOT NULL             COMMENT '作业类型 10=播种 20=施肥 30=灌溉 40=打药 50=除草 60=收获 70=整地 90=其他',
  `op_date`       DATE          NOT NULL                COMMENT '作业日期',
  `description`   VARCHAR(255)  NOT NULL DEFAULT ''     COMMENT '作业描述',
  `labor_hours`   DECIMAL(8,2)  NOT NULL DEFAULT 0.00   COMMENT '人工工时(h)',
  `machine_hours` DECIMAL(8,2)  NOT NULL DEFAULT 0.00   COMMENT '机械工时(h)',
  `labor_cost`    DECIMAL(12,2) NOT NULL DEFAULT 0.00   COMMENT '人工费(元)',
  `machine_cost`  DECIMAL(12,2) NOT NULL DEFAULT 0.00   COMMENT '机械费(元)',
  `operator_id`   INT UNSIGNED  NULL                    COMMENT '记录人',
  `created_at`    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  KEY `idx_planting_type` (`planting_id`, `op_type`),
  KEY `idx_op_date` (`op_date`),
  KEY `idx_planting_date` (`planting_id`, `op_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='农事作业记录表';

-- 【设计说明】
-- 1. 为什么 farming_log 和 input_cost 是两张表而不是一张？
--    它们回答的是**两个不同维度的问题**：
--      farming_log → "什么时间做了什么作业"（过程维度，用于生育期和作业次数统计）
--      input_cost  → "买了什么、用了多少、花了多少钱"（成本维度，用于投入产出比）
--    合并会导致大量 NULL：一次灌溉有工时但没有物资单价；买化肥有金额但没有作业描述。
--    一张表两种语义、列大量为空，是典型的设计异味。
-- 2. labor_cost / machine_cost 单独列出而非只存工时：工时本身不是钱。
--    折算率（元/工时）是随地区和年份变化的，存成金额才是当期实际成本。
--    这是成本核算的基本要求 —— 用今天的折算率去重算三年前的工时是错的。
-- 3. idx_planting_type 服务"某批次的某类作业次数"（如施了几次肥）。
--    idx_planting_date 服务"某批次的农事时间轴"（按日期排序的作业流水）。


-- =============================================================================
-- 8. input_cost — 农资投入明细（成本核算的数据来源）
-- =============================================================================
CREATE TABLE `input_cost` (
  `id`           INT UNSIGNED  NOT NULL AUTO_INCREMENT COMMENT '投入ID',
  `planting_id`  INT UNSIGNED  NOT NULL                COMMENT '种植批次ID',
  `input_type`   TINYINT UNSIGNED NOT NULL             COMMENT '投入类型 10=种子 20=化肥 30=农药 40=农膜 50=水电 60=人工 70=机械 80=其他',
  `item_name`    VARCHAR(100)  NOT NULL                COMMENT '投入品名称',
  `quantity`     DECIMAL(12,3) NOT NULL DEFAULT 0.000  COMMENT '用量',
  `unit`         VARCHAR(20)   NOT NULL DEFAULT ''     COMMENT '单位(kg/L/袋/度)',
  `unit_price`   DECIMAL(12,4) NOT NULL DEFAULT 0.0000 COMMENT '单价(元)',
  `amount`       DECIMAL(14,2) GENERATED ALWAYS AS
                   (ROUND(`quantity` * `unit_price`, 2)) STORED
                                                        COMMENT '金额(元)=用量*单价,生成列自动维护',
  `record_date`  DATE          NOT NULL                COMMENT '投入日期',
  `remark`       VARCHAR(255)  NOT NULL DEFAULT ''     COMMENT '备注',
  `created_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  KEY `idx_planting_type` (`planting_id`, `input_type`),
  KEY `idx_record_date` (`record_date`),
  KEY `idx_type_date` (`input_type`, `record_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='农资投入明细表';

-- 【设计说明】—— 生成列是本项目的重点技巧之一
-- 1. amount 用 STORED 生成列而非普通列：
--    `quantity * unit_price` 是纯确定性、同行内计算，交给数据库保证一致性，
--    应用层不可能写错、也不可能漏更新。用普通列就必须在每次 INSERT/UPDATE
--    时手工同步，一旦有非应用写入（如运维直接改库）就会不一致。
-- 2. 为什么选 STORED 而不是 VIRTUAL？
--    VIRTUAL 生成列不占存储、读时计算，但每次读取都要重算表达式，
--    且**无法用于覆盖索引**（覆盖索引要求值真的存在索引里）。
--    amount 是高频 SUM 聚合的字段，物化后可直接参与覆盖扫描。
--    代价是每次写入要计算并落盘 —— 对农资记录这种低频写入完全可接受。
-- 3. 为什么 unit_price 是 DECIMAL(12,4) 而 amount 是 DECIMAL(14,2)？
--    单价需要更高的小数精度（如化肥 2.8375 元/kg），而金额到分即可（财务口径）。
--    精度按字段的实际语义分配，而不是所有金额字段统一一个精度。


-- =============================================================================
-- 9. yield_record — 产量记录（分析的核心事实表）
-- =============================================================================
CREATE TABLE `yield_record` (
  `id`                 INT UNSIGNED  NOT NULL AUTO_INCREMENT COMMENT '产量记录ID',
  `planting_id`        INT UNSIGNED  NOT NULL                COMMENT '种植批次ID(唯一)',
  `plot_id`            INT UNSIGNED  NOT NULL                COMMENT '地块ID(冗余,分析主维度)',
  `crop_id`            INT UNSIGNED  NOT NULL                COMMENT '作物ID(冗余,分析主维度)',
  `harvest_date`       DATE          NOT NULL                COMMENT '收获日期',
  `harvest_month`      DATE GENERATED ALWAYS AS
                         (DATE_SUB(DATE(`harvest_date`), INTERVAL DAYOFMONTH(`harvest_date`) - 1 DAY)) STORED
                                                             COMMENT '收获月份首日(生成列,供同比环比走索引)',
  `harvest_area_mu`    DECIMAL(10,2) NOT NULL DEFAULT 0.00   COMMENT '收获面积(亩)',
  `yield_kg`           DECIMAL(14,2) NOT NULL DEFAULT 0.00   COMMENT '总产量(kg)',
  `yield_per_mu`       DECIMAL(10,2) GENERATED ALWAYS AS
                         (ROUND(`yield_kg` / NULLIF(`harvest_area_mu`, 0), 2)) STORED
                                                             COMMENT '单产(kg/亩)=总产量/收获面积,生成列',
  `grade`              TINYINT UNSIGNED NOT NULL DEFAULT 1   COMMENT '等级 1=一级 2=二级 3=三级',
  `unit_price`         DECIMAL(10,4) NOT NULL DEFAULT 0.0000 COMMENT '销售单价(元/kg)',
  `output_value`       DECIMAL(16,2) GENERATED ALWAYS AS
                         (ROUND(`yield_kg` * `unit_price`, 2)) STORED
                                                             COMMENT '产值(元)=总产量*单价,生成列',
  `moisture_content`   DECIMAL(5,2)  NOT NULL DEFAULT 0.00   COMMENT '含水率(%)',
  `quality_note`       VARCHAR(255)  NOT NULL DEFAULT ''     COMMENT '品质说明',
  `recorder_id`        INT UNSIGNED  NULL                    COMMENT '录入人',
  `created_at`         DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`         DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                              ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_planting` (`planting_id`),
  KEY `idx_crop_harvest` (`crop_id`, `harvest_date`, `yield_kg`, `yield_per_mu`),
  KEY `idx_plot_harvest` (`plot_id`, `harvest_date`, `yield_per_mu`),
  KEY `idx_harvest_month_crop` (`harvest_month`, `crop_id`, `yield_kg`),
  KEY `idx_harvest_date` (`harvest_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='产量记录表';

-- 【设计说明】—— 这张表是整个分析层的地基
-- 1. 三个 STORED 生成列：
--    yield_per_mu（单产）是本项目最核心的分析指标，被排行、ABC、相关系数
--    反复使用。交给数据库计算保证一致性，且物化后能直接建进覆盖索引。
--    注意 NULLIF(harvest_area_mu, 0)：生成列表达式里也必须防除零，
--    否则面积录入为 0 时写入会因 STRICT 模式报错。
-- 2. plot_id / crop_id 是**刻意的分析型冗余**：
--    它们可以从 planting_id 推导，但产量分析（按地块排行、按作物分组 TopN、
--    地图着色）几乎每次都要 JOIN planting 表，而 planting 本身还要 JOIN crop。
--    冗余后单表即可完成绝大多数分析，省掉两跳 JOIN。
--    风险边界：planting_id 一旦确定，其 plot_id/crop_id 永不变更，
--    所以这份冗余不存在"改了源表忘了改副本"的一致性风险 ——
--    冗余是否安全，取决于被冗余的字段是否**事实上不可变**。
-- 3. harvest_month 生成列（EXPLAIN 案例 3 的落点）：
--    同比环比要按月聚合。`GROUP BY DATE_FORMAT(harvest_date,'%Y-%m')` 无法用索引，
--    因为 B+树里存的是 harvest_date 原始值而非格式化字符串。用生成列物化月份首日
--    并建立索引后，月度聚合从"全表扫描 + 临时表 + 排序"变成索引扫描。
-- 4. uk_planting：一个种植批次只产出一条产量记录（唯一约束即业务规则），
--    同时充当外键索引，无需另建 idx_planting。
-- 5. idx_crop_harvest (crop_id, harvest_date, yield_kg, yield_per_mu) 是**覆盖索引**：
--    服务"按作物统计产量/单产"（EXPLAIN 案例 1）。四个列全在索引中，
--    查询可完全不回表（Using index）。
-- 6. idx_plot_harvest (plot_id, harvest_date, yield_per_mu)：
--    服务"某地块的产量历史"（EXPLAIN 案例 2）。
--    等值列 plot_id 在前、排序列 harvest_date 在后 —— 这正是最左前缀原则
--    最关键的一条推论：**等值条件列必须在范围/排序列之前**，
--    否则 ORDER BY 无法利用索引有序性，会退化成 filesort。
-- 7. 为什么不像 order_item 那样冗余 unit_price 快照？
--    这里 unit_price 是产量记录本身的属性（销售时的实际价格），
--    不依赖任何外部表的可变字段，所以不存在"快照"问题。


-- =============================================================================
-- 10. audit_log — 操作审计（只增不改）
-- =============================================================================
CREATE TABLE `audit_log` (
  `id`             INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '日志ID',
  `user_id`        INT UNSIGNED NULL DEFAULT NULL       COMMENT '操作人ID,NULL=系统操作',
  `action`         VARCHAR(32)  NOT NULL                COMMENT '动作 CREATE/UPDATE/DELETE/LOGIN/EXPORT',
  `target_table`   VARCHAR(64)  NOT NULL                COMMENT '目标表名',
  `target_id`      INT UNSIGNED NULL DEFAULT NULL       COMMENT '目标记录ID',
  `detail`         JSON NULL DEFAULT NULL               COMMENT '变更详情 {"before":{...},"after":{...}}',
  `changed_fields` VARCHAR(255) GENERATED ALWAYS AS
                     (JSON_UNQUOTE(JSON_EXTRACT(`detail`, '$.changed_fields'))) STORED
                                                       COMMENT '被修改字段列表(生成列,抽出供索引)',
  `ip`             VARCHAR(45)  NOT NULL DEFAULT ''     COMMENT '操作IP(45字节兼容IPv6)',
  `created_at`     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '操作时间(毫秒精度)',
  PRIMARY KEY (`id`),
  KEY `idx_user_created` (`user_id`, `created_at`),
  KEY `idx_target` (`target_table`, `target_id`, `created_at`),
  KEY `idx_action_created` (`action`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='操作审计日志表';

-- 【设计说明】
-- 1. DATETIME(3) 毫秒精度：审计日志写入频率高（同一秒可能几十条），
--    秒级精度无法确定事件先后顺序。DATETIME(3) 占 6 字节 vs DATETIME 的 5 字节，
--    代价可忽略。用 DATETIME 而非 TIMESTAMP：TIMESTAMP 有 2038 年上限，
--    且写入时会随时区转换 —— 审计日志要的是"记录的永恒真相"，不能随时区漂移。
-- 2. JSON 类型在 5.7 是**能用但不能查**的：
--    5.7.8+ 支持 JSON 存储和 JSON_EXTRACT 取值，但**没有 JSON_TABLE**，
--    无法把 JSON 展开成行。所以设计原则是：JSON 只当"不可查询的附件"，
--    任何需要检索/索引的字段必须抽成独立列。
-- 3. changed_fields 生成列演示"5.7 中如何给 JSON 内部字段建索引"：
--    JSON 列本身不能直接建索引，用 STORED 生成列把内部字段抽出来再建。
--    这是 5.7 处理 JSON 检索的标准姿势。
-- 4. 业务上应禁止对该表执行 UPDATE/DELETE（只增不改），
--    外键 ON DELETE SET NULL 保证删除用户不丢审计记录。


-- =============================================================================
-- 外键约束（统一在末尾添加，保持建表顺序清晰可读）
-- =============================================================================
-- 注意：外键两侧的字段类型必须**完全一致**（含 UNSIGNED），
-- 否则建表会直接报 errno 3780。这是 MySQL 外键最常见的建表失败原因。
ALTER TABLE `plot`
  ADD CONSTRAINT `fk_plot_farm` FOREIGN KEY (`farm_id`)
    REFERENCES `farm` (`id`) ON DELETE RESTRICT;

ALTER TABLE `planting`
  ADD CONSTRAINT `fk_planting_plot` FOREIGN KEY (`plot_id`)
    REFERENCES `plot` (`id`) ON DELETE RESTRICT,
  ADD CONSTRAINT `fk_planting_crop` FOREIGN KEY (`crop_id`)
    REFERENCES `crop` (`id`) ON DELETE RESTRICT,
  ADD CONSTRAINT `fk_planting_user` FOREIGN KEY (`agronomist_id`)
    REFERENCES `user` (`id`) ON DELETE SET NULL;

ALTER TABLE `farming_log`
  ADD CONSTRAINT `fk_flog_planting` FOREIGN KEY (`planting_id`)
    REFERENCES `planting` (`id`) ON DELETE CASCADE,
  ADD CONSTRAINT `fk_flog_user` FOREIGN KEY (`operator_id`)
    REFERENCES `user` (`id`) ON DELETE SET NULL;

ALTER TABLE `input_cost`
  ADD CONSTRAINT `fk_icost_planting` FOREIGN KEY (`planting_id`)
    REFERENCES `planting` (`id`) ON DELETE CASCADE;

ALTER TABLE `yield_record`
  ADD CONSTRAINT `fk_yield_planting` FOREIGN KEY (`planting_id`)
    REFERENCES `planting` (`id`) ON DELETE CASCADE,
  ADD CONSTRAINT `fk_yield_plot` FOREIGN KEY (`plot_id`)
    REFERENCES `plot` (`id`) ON DELETE RESTRICT,
  ADD CONSTRAINT `fk_yield_crop` FOREIGN KEY (`crop_id`)
    REFERENCES `crop` (`id`) ON DELETE RESTRICT,
  ADD CONSTRAINT `fk_yield_user` FOREIGN KEY (`recorder_id`)
    REFERENCES `user` (`id`) ON DELETE SET NULL;

ALTER TABLE `audit_log`
  ADD CONSTRAINT `fk_audit_user` FOREIGN KEY (`user_id`)
    REFERENCES `user` (`id`) ON DELETE SET NULL;

-- 【外键设计说明】
-- 1. ON DELETE RESTRICT（plot→farm、planting→plot/crop、yield→plot/crop）：
--    有子记录时禁止删除父记录。农场下有地块就不能删农场，地块有种植历史
--    就不能删地块。这是**保护分析数据完整性**的关键 —— 产量分析依赖历史数据，
--    允许级联删除会让"去年这块地的产量"凭空消失。
-- 2. ON DELETE CASCADE（farming_log/input_cost/yield_record → planting）：
--    农事记录、投入明细、产量记录都是种植批次的**从属数据**，脱离了批次
--    没有独立意义。删除批次时必须一起清理，否则会留下悬挂行污染聚合结果。
-- 3. ON DELETE SET NULL（→ user）：删除用户不应删除其操作记录。
--    审计日志和作业记录要保留"曾经有人做过这件事"，操作人置空即可。


-- =============================================================================
-- 外键的索引复用说明（面试常问：外键会自动建索引吗？）
-- =============================================================================
-- MySQL 创建外键时，如果**没有**一个以该列作为最左前缀的索引，会自动创建一个。
-- 本项目已刻意让各索引的最左列覆盖外键列，因此**不会产生冗余的自动索引**：
--   fk_plot_farm(farm_id)              → 复用 idx_farm_status 的最左列
--   fk_planting_plot(plot_id)          → 复用 uk_plot_crop_season 的最左列
--   fk_planting_crop(crop_id)          → 复用 idx_crop_status 的最左列
--   fk_flog_planting(planting_id)      → 复用 idx_planting_type 的最左列
--   fk_icost_planting(planting_id)     → 复用 idx_planting_type 的最左列
--   fk_yield_planting/plot/crop        → 复用 uk_planting / idx_plot_harvest / idx_crop_harvest
-- 可以用下面这条语句验证没有任何"为了外键而建"的冗余索引：
--   SELECT * FROM information_schema.STATISTICS WHERE TABLE_SCHEMA='smart_agri';
-- 如果当初把 idx_farm_status 定义成 (status, farm_id)，MySQL 就会额外自动
-- 创建一个单列 farm_id 索引 —— 这是**隐形的写放大**，也是"索引列顺序的影响
-- 远不止查询性能"的一个好例子。


-- =============================================================================
-- CHECK 约束（MySQL 8.0.16+ 真正强制执行）
-- =============================================================================
-- 【这是 5.7 → 8.0 迁移中一个容易被忽略、但价值很高的变化】
--
-- 5.7 对 CHECK 约束的处理是：**解析它、存下来、然后完全忽略**。
-- 也就是说下面这样的代码在 5.7 里毫无作用，脏数据照样能写进去：
--     CHECK (yield_kg >= 0)          -- 5.7：装饰品
-- 所以 5.7 时代的设计原则是"应用层兜底"，数据库层指望不上。
--
-- 8.0.16 起 CHECK 约束**真正生效**，违反会直接拒绝写入：
--     ERROR 3819 (HY000): Check constraint 'ck_xxx' is violated.
--
-- 于是本项目可以做到**纵深防御**：
--     第一层 前端 required / min 属性   ← 体验优化，不是安全边界
--     第二层 服务层显式校验              ← 能给出可读的业务错误提示
--     第三层 数据库 CHECK 约束           ← 最后一道闸门，绕过应用也拦得住
--
-- ⚠️ 注意 CHECK 约束的定位：它拦的是**数据本身的荒谬性**
--    （负产量、倒流的日期），而不是业务规则（"已收获的批次不能退回生长中"
--    这种跨行状态流转，CHECK 表达不了，仍然必须靠服务层的状态机）。
--    把 CHECK 当业务规则引擎用会写出一堆难以维护的约束。

-- 金额与数量不能为负
ALTER TABLE `input_cost`
  ADD CONSTRAINT `ck_icost_nonneg`
  CHECK (`quantity` >= 0 AND `unit_price` >= 0);

ALTER TABLE `yield_record`
  ADD CONSTRAINT `ck_yield_nonneg`
  CHECK (`harvest_area_mu` >= 0 AND `yield_kg` >= 0 AND `unit_price` >= 0);

ALTER TABLE `farming_log`
  ADD CONSTRAINT `ck_flog_nonneg`
  CHECK (`labor_hours` >= 0 AND `machine_hours` >= 0
         AND `labor_cost` >= 0 AND `machine_cost` >= 0);

-- 种植面积必须为正（这是"面积=0 时单产无意义"的数据库层兜底）
ALTER TABLE `planting`
  ADD CONSTRAINT `ck_planting_area`
  CHECK (`plant_area_mu` > 0);

-- 状态取值必须在合法集合内
-- ⚠️ 注意这里**只约束取值范围，不约束流转顺序** ——
--    "10 只能流转到 20" 这种规则需要知道"变更前是什么状态"，
--    是跨行/跨时间的问题，CHECK 表达不了，必须留在服务层的状态机里。
ALTER TABLE `planting`
  ADD CONSTRAINT `ck_planting_status`
  CHECK (`status` IN (10, 20, 30, 40, 50));

-- 生育期日期必须单调递增（播种 ≤ 出苗 ≤ 开花 ≤ 成熟 ≤ 收获）
-- 日期逆序不会让查询报错，只会让生育期分析算出负数 —— 属于"结果错了但不报错"，
-- 正是 CHECK 约束最该拦的一类问题。
ALTER TABLE `planting`
  ADD CONSTRAINT `ck_planting_dates`
  CHECK (
      (`emerge_date` IS NULL OR `emerge_date` >= `sow_date`)
      AND (`flower_date` IS NULL OR `flower_date` >= `emerge_date`)
      AND (`mature_date` IS NULL OR `mature_date` >= `flower_date`)
      AND (`harvest_date` IS NULL OR `harvest_date` >= `mature_date`)
  );

-- 气象数据的基本物理合理性
ALTER TABLE `weather_daily`
  ADD CONSTRAINT `ck_weather_temp`
  CHECK (`temp_max` >= `temp_min`
         AND `temp_min` >= -70 AND `temp_max` <= 60),
  ADD CONSTRAINT `ck_weather_nonneg`
  CHECK (`precipitation` >= 0 AND `sunshine_hours` >= 0
         AND `humidity` >= 0 AND `humidity` <= 100);

-- 地块面积必须为正
ALTER TABLE `plot`
  ADD CONSTRAINT `ck_plot_area`
  CHECK (`area_mu` > 0);


SELECT '01_schema.sql 执行完成' AS msg,
       (SELECT COUNT(*) FROM information_schema.TABLES
         WHERE TABLE_SCHEMA = 'smart_agri' AND TABLE_TYPE = 'BASE TABLE') AS table_count,
       (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
         WHERE CONSTRAINT_SCHEMA = 'smart_agri' AND CONSTRAINT_TYPE = 'FOREIGN KEY') AS fk_count;
