# SQL 深度解析

> 8 组分析的逐条原理、MySQL 8.0 的实现，以及**迁移中需要判断的地方**。
> 配套源码：`sql/04_analysis.sql`、`sql/06_audit_json.sql`、`app/services/analytics_service.py`

---

## 阅读顺序

先读「0. 前置知识」，再按 ①~⑧ 顺序读。
每节结构：**业务问题 → 8.0 的写法 → 为什么这么写 → 5.7 要怎么写 → 迁移的判断点**。

---

## 0. 前置知识

### 0.1 环境（实测确认，不是"理论上"）

| 项 | 实测值 | 影响 |
|---|---|---|
| MySQL 版本 | `8.0.46` | 有窗口函数 / CTE / `JSON_TABLE` |
| `character_set_server` | **`utf8mb4`** | 5.7 时代这里曾是 `latin1` |
| `collation_server` | `utf8mb4_0900_ai_ci` | 8.0 默认，已全库统一 |
| `character_set_client` | **`gbk`** | ⚠️ 命令行必须加 `--default-character-set=utf8mb4` |
| `sql_mode` | 含 `ONLY_FULL_GROUP_BY` / `STRICT_TRANS_TABLES` / `ERROR_FOR_DIVISION_BY_ZERO` | 与 5.7 基本相同 |
| `group_concat_max_len` | `1024` | ⚠️ `GROUP_CONCAT` 会**静默截断** |
| `optimizer_switch` | 含 `derived_merge=on`、**`skip_scan=on`**、`hash_join=on` | `skip_scan` 是 8.0 新增 |
| 默认认证插件 | `caching_sha2_password` | ⚠️ PyMySQL 需装 `cryptography` |
| `innodb_buffer_pool_size` | `128MB`（默认值） | 覆盖索引收益被放大 |

### 0.2 MySQL 8.0 **没有**的统计函数

这一条是很多人的知识盲区。**逐个调用验证过**，下面这些在 MySQL 8.0.46 里全都不存在：

```
CORR · COVAR_POP · COVAR_SAMP · REGR_SLOPE · REGR_R2 · REGR_INTERCEPT
MEDIAN · PERCENTILE_CONT
```

它们都是 Oracle / PostgreSQL 的函数，MySQL 至今没有。

**MySQL 有的是**：`STDDEV_POP` / `STDDEV_SAMP` / `VAR_POP` / `VAR_SAMP` /
`VARIANCE` / `STD` —— 这些可以用来做交叉验证（见 ③）。

> 我原本以为"8.0 至少有 `REGR_R2()` 可以替代 `CORR()`"，
> 写进代码后**运行报错 `FUNCTION REGR_SLOPE does not exist`** 才发现是错的。
> 这也是为什么这个项目里所有技术结论都标注"实测"。

### 0.3 关于除零，必须说准确

```sql
SELECT 1/0;   -- 返回 NULL（不是报错）
```

很多人（包括一些面试题标准答案）会说"`ERROR_FOR_DIVISION_BY_ZERO`
会导致除零报错，所以必须用 `NULLIF`"。**这个说法不准确**：

- 在 **`SELECT`** 中：返回 `NULL` 并产生 warning，**不报错**
- 在 **`INSERT` / `UPDATE`** 中：因 `STRICT_TRANS_TABLES` 才**报错**

本项目仍然全程使用 `NULLIF(分母, 0)`，但理由是
**消除 warning + 显式表达业务语义**，而不是"否则会报错"。

### 0.4 窗口函数的三条铁律

**铁律一：累计求和必须显式写 `ROWS` 帧**

窗口函数**默认帧是 `RANGE`**，它把 `ORDER BY` 值**相同的行算作同一帧**。

```sql
-- ❌ 默认 RANGE：并列的两行会拿到同一个累计值
SUM(x) OVER (ORDER BY y)

-- ✅ 显式 ROWS：严格逐行累加
SUM(x) OVER (ORDER BY y ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
```

本项目的造数数据恰好没有并列值，所以两者结果相同 ——
但 `sql/05_tests.sql` 的断言 11 专门检测了这一点并如实报告
"当前数据不暴露差异，但代码仍应显式写 ROWS 以防数据变化"。

**铁律二：窗口函数**不会**替你解决破平局问题**

```sql
ROW_NUMBER() OVER (ORDER BY yield_per_mu DESC)
```

若两行单产相同，`ROW_NUMBER()` 给出的顺序**依赖执行计划**，两次执行可能不同。
所以第二排序键仍然必须写：

```sql
ROW_NUMBER() OVER (ORDER BY yield_per_mu DESC, plot_no ASC)
```

**窗口函数不改变数据本身的模糊性**，它只换了一种表达方式。
这一点很容易被忽略：以为用了窗口函数就"标准、正确"了。

**铁律三：`LAG(x, N)` 数的是行，不是值**

这是本项目迁移中**最重要的一处判断**，详见 ②。

---

## ① 有效积温 (GDD) 累计曲线

### 业务问题

某批次的作物在整个生育期积累了多少有效积温？是否达到该品种成熟所需？

**积温不足是北方农业最主要的减产因素** —— 霜冻来临前没成熟，产量和品质都大幅下降。

### 公式

```
日有效积温 = max(0, (日最高温 + 日最低温) / 2 − 生物学零度)
生育期积温 = Σ 日有效积温
```

`max(0, …)` **不能省**：低于生物学零度的日子不但不积累，**还不能倒扣**。

生物学零度因作物而异（小麦 0℃、玉米 10℃、黄瓜 12℃），所以它是 `crop` 表的字段。

### 8.0 的写法

```sql
SELECT
    w.obs_date,
    ROUND(GREATEST(0, (w.temp_max + w.temp_min)/2 - c.base_temp), 2) AS gdd_daily,
    ROUND(SUM(GREATEST(0, (w.temp_max + w.temp_min)/2 - c.base_temp))
              OVER (ORDER BY w.obs_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2) AS gdd_cum
FROM planting p
JOIN plot pl ON pl.id = p.plot_id
JOIN farm f  ON f.id  = pl.farm_id
JOIN crop c  ON c.id  = p.crop_id
JOIN weather_daily w ON w.region_code = f.region_code
                    AND w.obs_date BETWEEN p.sow_date AND p.mature_date
WHERE p.id = :pid
ORDER BY w.obs_date
```

一行。注意 `ROWS BETWEEN ...` 是必需的（铁律一）。

### 5.7 要怎么写

```sql
SET @gdd := 0, @pid := NULL;

SELECT t.obs_date, t.gdd_daily,
       @gdd := IF(@pid = t.planting_id, @gdd + t.gdd_daily, t.gdd_daily) AS gdd_cum,
       @pid := t.planting_id AS _pid_guard       -- ← 漏了这行，累计直接失效
FROM (
    SELECT p.id AS planting_id, w.obs_date,
           GREATEST(0, (w.temp_max + w.temp_min)/2 - c.base_temp) AS gdd_daily
    FROM ... 
    ORDER BY w.obs_date
    LIMIT 18446744073709551615                   -- ← 不加，ORDER BY 被 derived_merge 吃掉
) t
CROSS JOIN (SELECT @gdd := 0, @pid := NULL) init;
```

**三个必须同时满足的条件**（缺一个结果就静默错乱）：

1. `@pid := t.planting_id` 必须存在且必须在累加行**之后** —— 它记住当前行的分组值供下一行比较。
   漏掉它，`@pid` 恒为 NULL，"累计值"退化成"当日值"；
2. `LIMIT 18446744073709551615` 强制派生表物化；
3. 派生表内必须 `ORDER BY`。

> **本项目真的踩过条件 ①**：移植时漏抄那行，GDD 曲线末行显示 **13.15**，
> 而真实累计值是 **1138.61** —— 不报错、不崩溃，只是结果全错。

### 为什么变量初始化写在 `CROSS JOIN` 里

不能写成：

```sql
SET @gdd := 0;          -- ❌ 危险
SELECT @gdd := @gdd + ...;
```

因为 Flask-SQLAlchemy 用**连接池**，同一个请求的两条 SQL 可能跑在**不同的连接**上。
`SET` 在连接 A 上执行，`SELECT` 跑在连接 B，`@gdd` 是 `NULL`。

`CROSS JOIN (SELECT @gdd := 0) init` 把初始化写进**同一条 SELECT**，
天然免疫这个问题。（写脚本时是单连接，不会暴露；一上 Web 就翻车。）

### 迁移的判断点

**这一处可以放心改。** 窗口函数的 `PARTITION BY` 语义与"逐行累加"完全一致，
不存在 ② 那样的语义陷阱。

---

## ② 产量同比 (YoY) 与环比 (MoM)  ——  迁移中最重要的一处判断

### 业务问题

本月产量相比上月（环比）和去年同月（同比）是增长还是下降？

### ⚠️ 结论：**不能**改成 `LAG()`

很多人迁移到 8.0 时会顺手写成：

```sql
LAG(total_yield_kg, 12) OVER (ORDER BY harvest_month) AS last_year   -- ❌ 错的
```

**看起来等价，实际上是错的。**

`LAG(x, N)` 数的是「往前 **N 行**」，不是「往前 **N 个月**」。
本项目的月度数据**有断档**（缺失 2023-02、2024-03 —— 那两个月份没有作物收获，
`v_yield_monthly` 里就没有对应行）。一旦有断档，第 12 行就不再是"去年同月"。

### 实测证据

在本项目真实数据上跑两种写法对比：

| 月份 | 行号 | `LAG(x,12)` | 自连接（正确） | 说明 |
|---|---|---|---|---|
| 2023-05 | 11 | NULL | NULL | 行数不足 12，都为空 |
| 2023-06 | 12 | **NULL** | **1648.5**（2022-06）| 自连接已能匹配，LAG 还差一行 |
| 2023-07 | 13 | 1648.5（2022-06）| **6337.0**（2022-07）| LAG 差了一个月 |
| 2023-08 | 14 | 6337.0（2022-07）| **3305.8**（2022-08）| 持续错位 |
| … | | | | **29 个月里 18 个月不一致** |

**而且不会报任何错。** 如果没做对照，这个 bug 会一直躺在报表里。

### 正确做法：自连接

```sql
SELECT
    cur.harvest_month,
    ROUND(cur.total_yield_kg / NULLIF(prev.total_yield_kg, 0) * 100 - 100, 2) AS mom_pct,
    ROUND(cur.total_yield_kg / NULLIF(yoy.total_yield_kg, 0) * 100 - 100, 2)  AS yoy_pct
FROM v_yield_monthly cur
LEFT JOIN v_yield_monthly prev
       ON prev.harvest_month = DATE_SUB(cur.harvest_month, INTERVAL 1 MONTH)
LEFT JOIN v_yield_monthly yoy
       ON yoy.harvest_month  = DATE_SUB(cur.harvest_month, INTERVAL 1 YEAR)
ORDER BY cur.harvest_month;
```

**自连接按日期值匹配，与行号无关**，断档也不影响。

### 为什么用自连接而不是条件聚合

条件聚合的写法是：

```sql
SUM(CASE WHEN month = '2024-03' THEN amount END) AS m_202403,   -- ❌
SUM(CASE WHEN month = '2024-04' THEN amount END) AS m_202404,
```

这是**硬编码的宽表**：月份数一变就要改 SQL，且无法处理不连续的月份。

### 为什么用 `LEFT JOIN` 而不是 `INNER JOIN`

数据集的第一个月没有上月，`INNER JOIN` 会**静默丢掉这个月份**，图表就缺一块。

### 这条结论被固化成了断言

`sql/05_tests.sql` 的断言 8 会检测"两种写法是否有差异"：

```sql
SELECT '  自连接 与 LAG(x,12) 不等价（证明有断档）',
       CASE WHEN SUM(CASE WHEN NOT (m.lag12 <=> sj.total_yield_kg) THEN 1 ELSE 0 END) > 0
            THEN 'PASS' ELSE 'FAIL' END,
       CONCAT(..., ' 个月两者结果不同 → 数据有断档，自连接才是正确实现')
```

**如果哪天数据变成连续无断档的，这条断言会失败**，提醒我
"LAG 现在是对的了，文档里的结论需要重新审视"。

> **测试不只用来验证代码，也用来标记结论的适用边界。**

### 什么情况下 `LAG` 才是对的

时间序列**连续无断档**时（比如每月都有记录），`LAG(x, 12)` 等价于"去年同期"。

**判断依据是数据本身，不是函数本身。** 同一个函数用在不同数据上，对错可能相反 ——
本项目的分析 ⑨ 里 `LAG` 就是正确的（气象数据逐日连续），详见 0.4 铁律三。

---

## ③ 单产影响因子的皮尔逊相关系数

### 业务问题

施肥量对产量有多大影响？

### MySQL 没有 `CORR()`，也没有任何回归聚合函数

见 0.2 节。必须手写：

```
r = (n·Σxy − Σx·Σy) / √[(n·Σx² − (Σx)²)(n·Σy² − (Σy)²)]
```

```sql
ROUND(
    (COUNT(*) * SUM(f.fert_per_mu * y.yield_per_mu)
     - SUM(f.fert_per_mu) * SUM(y.yield_per_mu))
    / NULLIF(SQRT(
        (COUNT(*) * SUM(f.fert_per_mu * f.fert_per_mu)
         - SUM(f.fert_per_mu) * SUM(f.fert_per_mu))
      * (COUNT(*) * SUM(y.yield_per_mu * y.yield_per_mu)
         - SUM(y.yield_per_mu) * SUM(y.yield_per_mu))
    ), 0)
, 3)
```

### 交叉验证：用两条完全不同的数学路径算同一个 r

手写公式写错的概率不低，而算错的 r 仍然是一个"看起来合理的小数"，**不会报错**。

所以同时算两遍：

| 路径 | 公式 | 数学来源 |
|---|---|---|
| ① 展开式 | `(n·Σxy − Σx·Σy) / √[(n·Σx²−(Σx)²)(n·Σy²−(Σy)²)]` | 代数化简 |
| ② 定义式 | `协方差 / (标准差 × 标准差)` | 统计定义 |
| | 其中协方差 = `(Σxy − Σx·Σy/n)/n`，标准差用内置 `STDDEV_POP` | |

两条路径来源完全不同，结果必须吻合到小数点后 4 位。
**实测本项目 12 个作物全部一致。**

这比"我觉得公式是对的"可靠得多，也是 `sql/05_tests.sql` 断言 10 的内容。

### 为什么必须分作物算

跨作物汇总相关系数没有意义：

```
番茄单产 5000+ kg/亩，大豆 200 kg/亩
而两者施肥量范围差不多（15~95 kg/亩）
```

混在一起算，作物间的**量级差异**会完全主导结果，把施肥的真实影响淹没。
这是辛普森悖论的另一种表现。

### 但分作物算又受限于样本量

每个作物只有 6~50 条记录，r 的抽样波动可达 ±0.3。
表里某个作物 r 偏低，很可能只是小样本噪声。

**解法：相对单产。** 把每块地的单产除以**该作物的平均单产**，
得到无量纲的"相对单产"，就能把全部 259 条记录放在一起算。
这是农学研究的常规做法。

### 结果怎么解读

实测 r 落在 **0.33 ~ 0.82**（分作物），跨作物汇总约 **0.51**。

- **这个区间是真实的。** 影响单产的因素很多，施肥只是其中之一；
- **算出来 r ≈ 1 反而可疑** —— 那种数据基本是编的；
- **r 偏低还可以解释为"关系是非线性的"**：皮尔逊只度量线性关联，
  而施肥的真实关系是对数型（边际报酬递减，曲线是凹的），r 被系统性低估。

### 迁移的判断点

**这条查询一个字都不用改。** 它本来就不依赖任何版本特有的能力 ——
窗口函数解决的是"跨行计算"，而相关系数需要的是"自定义聚合函数"，是另一个方向。

> **迁移版本不等于"所有查询都要改写"。** 本项目 8 组分析里，
> 相关系数、ROI、同比环比这三组在迁移中几乎没有改动。

---

## ④ 投入产出比 (ROI)

### 业务问题

每一块钱的投入能换回多少产值？钱主要花在哪了？

### 核心陷阱：一对多 JOIN 导致成本重复累加

一个批次有 5 条农资记录 + 8 条农事记录。如果直接写：

```sql
FROM planting p
LEFT JOIN input_cost  i ON i.planting_id = p.id
LEFT JOIN farming_log l ON l.planting_id = p.id     -- ❌
```

会产生 `5 × 8 = 40` 行的**笛卡尔积**，
然后 `SUM(i.amount)` 会把每条农资金额**重复算 8 遍**。

**危险之处**：它不报错，结果看起来只是"数字偏大"。

正确做法是**先各自聚合到 planting 粒度，再 JOIN**：

```sql
LEFT JOIN (SELECT planting_id, SUM(amount) AS material_cost
           FROM input_cost GROUP BY planting_id) ic ON ic.planting_id = p.id
LEFT JOIN (SELECT planting_id, SUM(labor_cost) AS labor_cost
           FROM farming_log GROUP BY planting_id) fl ON fl.planting_id = p.id
```

见 `sql/02_views.sql` 的 `v_planting_cost`。
项目里有一条断言专门守这个：`视图总成本 == 物资 + 人工 + 机械`。

### 8.0 的增量：`RANK()`

```sql
RANK() OVER (PARTITION BY d.crop_id
             ORDER BY d.output_value / NULLIF(c.total_cost, 0) DESC) AS rank_in_crop
```

加了"同作物内排名"这一列。5.7 里要用用户变量模拟（而且因为要按作物分区，复杂度不低）。

### 成本模型必须按面积缩放

造数时人工费最初写成 `uniform(0.5, 4.0)` 工时（**与面积无关**），
结果 300 亩的地块只算了 2 小时人工，亩均成本只有 250 元。

**查询本身完全正确，错的是输入数据。**
"结果看起来不合理"往往要先怀疑数据口径而不是 SQL。

---

## ⑤ 分组 Top N 排行榜

### 8.0 的写法

```sql
SELECT * FROM (
    SELECT crop_name, plot_no, yield_per_mu,
           ROW_NUMBER() OVER (PARTITION BY crop_id
                              ORDER BY yield_per_mu DESC, plot_no ASC) AS rn
    FROM v_plot_yield_summary
) t
WHERE t.rn <= 3;
```

⚠️ 末尾的 `plot_no` 第二排序键**仍然必须写**（铁律二）。

### 5.7 要怎么写

```sql
@rn  := IF(@grp = s.crop_id, @rn + 1, 1) AS rn,
@grp := s.crop_id                        AS _grp_guard   -- 漏了这行，全盘失效
... LIMIT 18446744073709551615           -- 不加，ORDER BY 被吃掉
```

**三行代码，三个必须同时满足的约束，缺一个结果就静默错乱。**

第 ① 条（`@grp` 那行）本项目真的踩过 —— 漏抄后"连续干旱天数"全部变成 1 天、
积温累计退化成当日值。

### 顺带：`RANK` / `DENSE_RANK` / `ROW_NUMBER` 的区别

面试高频考点：

| 函数 | 并列值行为 | 示例 |
|---|---|---|
| `ROW_NUMBER()` | 并列也强行排出先后（必定唯一）| 1 2 3 4 |
| `RANK()` | 并列同名次，后续名次**跳过** | 1 2 2 4 |
| `DENSE_RANK()` | 并列同名次，后续名次**不跳过** | 1 2 2 3 |

业务含义：

- `ROW_NUMBER` → "每组只能取 N 个名额"（推荐 Top3 地块）
- `RANK` → "排名第几"（并列第 2，下一个是第 4）
- `DENSE_RANK` → "分几档"（并列第 2，下一个是第 3）

页面下方有这三个函数的实测对比表。

### 第二种解法：确定性实现（相关子查询）

```sql
SELECT s.crop_name, s.plot_no, s.yield_per_mu
FROM v_plot_yield_summary s
WHERE (
    SELECT COUNT(*) FROM v_plot_yield_summary s2
    WHERE s2.crop_id = s.crop_id
      AND (s2.yield_per_mu > s.yield_per_mu
           OR (s2.yield_per_mu = s.yield_per_mu AND s2.plot_no < s.plot_no))
) < 3
```

语义是"比我强的同类地块少于 3 个 → 我进前三"。

**O(n²) 慢得多，但结果永远正确**，不依赖任何优化器行为。
两种实现的结果在 `verify.py` 里逐行比对 —— **36 行完全相同**。

---

## ⑥ ABC 帕累托分析

### 8.0 的写法（含具名窗口）

```sql
SELECT
    t.plot_no, t.total_yield_kg,
    SUM(t.total_yield_kg) OVER w AS cum_yield,
    ROUND(SUM(t.total_yield_kg) OVER w
          / NULLIF(SUM(t.total_yield_kg) OVER (), 0) * 100, 2) AS cum_pct,
    CASE
        WHEN SUM(t.total_yield_kg) OVER w
             / NULLIF(SUM(t.total_yield_kg) OVER (), 0) <= 0.70 THEN 'A'
        WHEN SUM(t.total_yield_kg) OVER w
             / NULLIF(SUM(t.total_yield_kg) OVER (), 0) <= 0.90 THEN 'B'
        ELSE 'C'
    END AS abc_class
FROM v_plot_yield_summary t
WINDOW w AS (                                  -- ★ 具名窗口
    ORDER BY t.total_yield_kg DESC, t.plot_id ASC
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
)
ORDER BY t.total_yield_kg DESC, t.plot_id ASC;
```

**具名窗口是 8.0 的可读性改进**：同一个窗口定义在 SELECT 里被引用 4 次，
用 `WINDOW` 子句定义一次即可。5.7 里每个 `OVER(...)` 都要重写完整定义。

### 两个容易忽略的细节

1. **累计帧必须显式写 `ROWS`**（铁律一）。
   如果两块地总产量完全相同，`RANGE` 会让它们得到**同一个累计值** ——
   相当于把两块地当作一块算；
2. **排序必须带破平局键**（铁律二）。

### ABC 分类的阈值

| 类别 | 累计占比 | 含义 |
|---|---|---|
| A | ≤ 70% | 贡献最大的核心地块 |
| B | 70% ~ 90% | 中坚力量 |
| C | > 90% | 长尾地块 |

阈值可按业务调整，关键是**口径要固定**，不能这个月 70/90、下个月 80/95。

### 本项目的实际结果

```
A 类  22% 的地块  →  贡献 69% 的产量
B 类  22% 的地块  →  贡献 20%
C 类  57% 的地块  →  贡献 10%
```

标准的帕累托分布。

### 5.7 的真实差异在内存上

- **用户变量版本是流式的** —— 逐行读、逐行累加，内存占用恒定；
- **窗口函数版本要物化整个结果集**才能计算，数据量极大时可能落盘。

所以"8.0 的写法更先进"**不等于**"8.0 的写法永远更快"。
真正解决性能问题靠的是**减少数据量**（预聚合汇总表），而不是换一种窗口函数写法。

---

## ⑦ 同期群 (Cohort) 留存分析  ——  CTE 改写

### 业务问题

按地块"第一次种植的茬口"分群，看它们在后续各茬口还在种的比例。

### 8.0 用 CTE 拆解

```sql
WITH valid AS (
    SELECT plot_id, season_seq, season_year, season
    FROM planting WHERE status IN (20, 30, 40)
),
cohort AS (
    SELECT plot_id, MIN(season_seq) AS cohort_seq FROM valid GROUP BY plot_id
),
cohort_size AS (
    SELECT cohort_seq, COUNT(*) AS cohort_size FROM cohort GROUP BY cohort_seq
),
activity AS (
    SELECT DISTINCT plot_id, season_seq FROM valid
),
label AS (
    SELECT DISTINCT season_seq, CONCAT(season_year, season) AS cohort_label FROM valid
)
SELECT c.cohort_seq, l.cohort_label, cs.cohort_size,
       (a.season_seq - c.cohort_seq) AS season_idx,
       COUNT(DISTINCT a.plot_id)     AS retained,
       ROUND(COUNT(DISTINCT a.plot_id) / NULLIF(cs.cohort_size, 0) * 100, 1) AS retention_pct
FROM cohort c
JOIN activity    a  ON a.plot_id     = c.plot_id
JOIN cohort_size cs ON cs.cohort_seq = c.cohort_seq
JOIN label       l  ON l.season_seq  = c.cohort_seq
GROUP BY c.cohort_seq, l.cohort_label, cs.cohort_size, a.season_seq
ORDER BY c.cohort_seq, season_idx;
```

每一步都是有名字、自包含的结果集，主查询只负责把它们拼起来。

### 5.7 要怎么写

```sql
FROM (SELECT plot_id, MIN(season_seq) AS cohort_seq FROM planting WHERE ... GROUP BY plot_id) co
JOIN (SELECT plot_id, season_seq FROM planting WHERE ... GROUP BY plot_id, season_seq) act
  ON act.plot_id = co.plot_id
JOIN (SELECT t.cohort_seq, COUNT(*) AS cohort_size FROM (
        SELECT plot_id, MIN(season_seq) AS cohort_seq FROM planting WHERE ... GROUP BY plot_id
      ) t GROUP BY t.cohort_seq) cs
  ON cs.cohort_seq = co.cohort_seq
```

两个明显的问题：

1. **"每个地块的首季"这段逻辑被重复写了两遍**（第 1 层和第 3 层各一次）；
   改口径时漏改一处就出错；
2. **读不出结构** —— 三层嵌套派生表层层套着，很难看出"首季 / 活动 / 群规模"的关系。

**CTE 让可读性提升了一个量级**：SQL 从 40 行降到 35 行，但理解成本大幅下降。

### 季序必须线性递增

```python
season_seq = 年份 × 2 + (春=1 / 秋=2)
```

相邻茬口恰好相差 1。

**改成年月编码就会全错。** 比如用 `202412`，2024 年 12 月的下一个是 `202501`，
两者相差 **89 而不是 1** —— 跨年时"连续"的判断会彻底崩掉。

**更隐蔽的是：这个 bug 只在跨年的连续段上暴露**，数据不足 12 个月永远发现不了。

### 天然的自检断言

季序 = 0 的那一行就是"首季本身"，留存地块数**必然等于群规模**，留存率**必然 100%**。

如果某行不是 100%，说明 `season_seq` 的线性假设被破坏了。
`sql/05_tests.sql` 的断言 4 就是守这一条 ——
**用业务逻辑本身的性质做测试，比人工核对具体数字可靠得多。**

---

## ⑧ 连续干旱日数预警（gaps-and-islands）

### 业务问题

每个气象区历史上持续最久的干旱过程是哪几次？

### 算法原理：为什么"日期 − 行号"能识别连续段

把日期转成线性天数 `TO_DAYS(obs_date)`，减去"连续段内行号"：

- **同一个连续段内**：日期每行 +1，行号也每行 +1 → **差值恒定**
- **段与段之间**：日期跳变但行号重置 → **差值改变**

于是这个"差值"就成了每个连续段的唯一标识，`GROUP BY` 它就能得到每段的起止和长度。

### 8.0 的写法

```sql
WITH dry AS (
    SELECT
        region_code, obs_date, temp_avg,
        TO_DAYS(obs_date)
          - ROW_NUMBER() OVER (PARTITION BY region_code ORDER BY obs_date) AS island
    FROM weather_daily
    WHERE precipitation < 1.0
      AND temp_avg >= 5.0
)
SELECT region_code, MIN(obs_date) AS start_date, MAX(obs_date) AS end_date,
       COUNT(*) AS dry_days, ROUND(AVG(temp_avg), 1) AS avg_temp, ...
FROM dry
GROUP BY region_code, island
HAVING COUNT(*) >= 12
ORDER BY dry_days DESC;
```

### 5.7 的真实痛苦

需要两个用户变量：

```sql
@rn8  := IF(@grp8 = region_code, @rn8 + 1, 1) AS island,
@grp8 := region_code AS _grp_guard        -- ← 漏了这行，查询返回 0 行
```

`@grp8` 那行**绝对不能省**，而且必须写在第一行之后 ——
漏掉它，行号每行都重置，所有"连续段"变成单天，查询返回 **0 行结果**，
而且**不报任何错**。本项目实际踩过这个坑。

> **8.0 的 `ROW_NUMBER() OVER (PARTITION BY region_code ...)` 自带分区语义** ——
> 结构上不可能漏写"记住上一行分组"的赋值。
> 这类错误的消失不是"代码变短了"，而是**错误变得不可表达了**。

### 两个坑（与版本无关）

**坑 1：日期必须转成线性数值**

用 `TO_DAYS()`（自公元 0 年以来的天数，天然线性递增），
不能用 `202412` 这种年月编码（相邻月差 89 而不是 1）。

**坑 2：必须排除冬季封冻期（领域知识）**

最初这条查询没有温度条件，结果黑龙江查出来的"最长干旱"是
**1~2 月的 53 天，期间均温 −14.9℃**。

**那不是旱情，是冬季封冻** —— 东北的冬天本来就几乎不降水，作物也不生长。
加上 `AND temp_avg >= 5` 后，查出来的全部是春夏生长季的干旱过程。

> **农业干旱的定义前提是"作物处于生长状态且水分亏缺"** ——
> 这是纯技术视角想不到的一层，必须靠领域知识补上。

---

## ⑨ 【8.0 独有】`JSON_TABLE` 字段级变更分析

### 业务问题

审计日志的 `detail` 存的是 `{"before": {...}, "after": {...}}` 这样的 JSON。
**这次修改到底动了哪几个字段？每个字段从什么变成了什么？**

### 5.7 完全做不到

5.7 有 JSON 类型和 `JSON_EXTRACT`，但**没有 `JSON_TABLE`** ——
无法把 JSON 里的键**展开成行**。所以只能：

- **方案 A**：整块读到应用层解析 —— 失去在数据库里做聚合/排序/分页的能力；
- **方案 B**：提前把关键字段抽成独立列。本项目 5.7 时代用 STORED 生成列
  `changed_fields` 做了这件事，但**只能做"包含某个字段"的模糊匹配**，
  无法做字段级的值比较。

### 8.0 的写法

```sql
SELECT
    a.id, a.created_at, a.action, a.target_table, a.target_id,
    jt.field_name,
    JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.before.', jt.field_name))) AS old_value,
    JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.after.',  jt.field_name))) AS new_value,
    CASE WHEN (JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.before.', jt.field_name))))
              <=> (JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.after.', jt.field_name))))
         THEN '未变化' ELSE '已变更' END AS really_changed
FROM audit_log a
JOIN JSON_TABLE(
         JSON_KEYS(a.detail, '$.before'), '$[*]'
         COLUMNS (field_name VARCHAR(64) PATH '$')
     ) AS jt
WHERE a.detail IS NOT NULL AND JSON_VALID(a.detail);
```

### 核心思路：动态键名怎么展开成行

难点在于 `detail` 里的字段名是**动态的**（改地块是 name/area_mu，改批次是 status），
不可能写死路径。解法分三步：

1. `JSON_KEYS(detail, '$.before')` 拿到这次修改涉及的所有字段名，
   得到一个 JSON 数组 `["area_mu","fertility_level",...]`；
2. `JSON_TABLE` 把这个数组展开成 N 行（每行一个字段名）；
3. 再用 `CONCAT` 拼出 `'$.before.<字段名>'` 的路径逐个取值。

这是 `JSON_TABLE` 最典型的用法：**动态键名的横向展开**。

### ⚠️ 实测踩到的限制：`->>` 的路径必须是字面量

```sql
a.detail ->> CONCAT('$.before.', jt.field_name)     -- ❌ ERROR 1064
```

**语法报错**。因为 `->>` 只接受字符串字面量作为路径，
而这里的路径是动态拼接的，必须退回完整写法：

```sql
JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.before.', jt.field_name)))   -- ✅
```

`->>` 只是"路径已知且为常量"时的语法糖，不是通用替代。
这一点官方文档里没有明说，是实际试出来的。

### 由此得到的分析能力

有了 `JSON_TABLE`，就能做 5.7 时代**做不到**的分析：

```sql
-- 哪些字段最常被修改？（需要先展开成行才能 GROUP BY 计数）
WITH field_changes AS (
    SELECT a.target_table, jt.field_name
    FROM audit_log a
    JOIN JSON_TABLE(JSON_KEYS(a.detail, '$.before'), '$[*]'
                    COLUMNS (field_name VARCHAR(64) PATH '$')) AS jt
    WHERE a.detail IS NOT NULL
      AND NOT ((JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.before.', jt.field_name))))
            <=> (JSON_UNQUOTE(JSON_EXTRACT(a.detail, CONCAT('$.after.', jt.field_name)))))
)
SELECT target_table, field_name, COUNT(*) AS change_cnt,
       RANK() OVER (PARTITION BY target_table ORDER BY COUNT(*) DESC, field_name) AS rank_in_table,
       ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY target_table), 1) AS pct
FROM field_changes
GROUP BY target_table, field_name;
```

**业务价值**：某个字段被频繁修改，往往说明录入环节或业务规则有问题。

### ⚠️ 但有一条设计原则没有变

`JSON_TABLE` 是**运行时展开**，每次查询都要解析 JSON，**无法建立索引**。所以：

- 需要**高频过滤/排序**的字段，仍然应该抽成独立列（甚至生成列）；
- `JSON_TABLE` 适合"偶尔做一次的分析型查询"，不适合高频点查。

> **`JSON_TABLE` 让 JSON 变得可查了，但没有让它变得高效。**
> 别因为有了它就把所有字段都塞进 JSON —— 那是把"能查"当成了"该查"。

---

## 附录：5.7 vs 8.0 能力对照

| 能力 | 5.7 | 8.0 | 本项目怎么用 |
|---|---|---|---|
| 窗口函数 | ❌ | ✅ | 8 组分析里 4 组直接受益 |
| CTE (`WITH`) | ❌ | ✅ | 同期群、干旱识别改用它 |
| 具名窗口 `WINDOW` | ❌ | ✅ | ABC 分析复用窗口定义 |
| `JSON_TABLE` | ❌ | ✅ | 字段级变更分析 |
| `CORR()` 等回归聚合 | ❌ | ❌ | **两版都没有**，手写公式 |
| `CHECK` 约束 | ⚠️ 解析但忽略 | ✅ 8.0.16+ 强制执行 | 新增 9 条 |
| `skip_scan` | ❌ | ✅ 默认开启 | 最左列低基数时可跳过 |
| 降序索引 | ❌（忽略 `DESC`）| ✅ | 本项目仍靠 `Backward index scan` |
| `INSTANT` 加列 | ❌ | ✅ 8.0.12+ | 加 STORED 生成列仍需重建 |
| 生成列 | ✅ 5.7.6+ | ✅ | 三个 STORED 生成列 |

> **注意最后一列的措辞**：8.0 给了很多新能力，但**不是每个都用上了**。
> "有这个特性"和"这个场景该用它"是两件事。
