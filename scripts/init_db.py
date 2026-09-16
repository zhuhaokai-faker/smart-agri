#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
建库建表：按顺序执行 sql/01_schema.sql -> 02_views.sql -> 03_optimize.sql

【为什么用 Python 执行而不是写个 .bat 调 mysql 命令行】
  1. 跨平台：不依赖 mysql.exe 在 PATH 里，也不用猜它的安装路径；
  2. 能报错定位：出错时能明确指出是哪个文件、哪一行附近；
  3. 能校验：执行完自动检查表数、外键数、字符集，不用人工确认。

【为什么用 CLIENT.MULTI_STATEMENTS 而不是按分号切分 SQL 文件】
  按 ';' 切分 SQL 文本是脆弱的 —— 字符串字面量、注释、存储过程体里都可能
  含分号，简单切分会把语句切碎。交给 MySQL 客户端协议自己解析才是可靠的。
  （代价：拿不到每条语句的独立报错，所以出错时返回的文件名要打印清楚。）
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# ---------------------------------------------------------------- 环境预检
# ⚠️ 必须放在**所有第三方 import 之前**。
#    否则当依赖缺失时，解释器会先炸在 `import pymysql` 这一行，
#    抛出的 ModuleNotFoundError 指向这里而不是真正的原因（解释器选错了）。
#    检查逻辑见项目根的 env_check.py。
from env_check import check  # noqa: E402

check()

import pymysql                       # noqa: E402
from pymysql.constants import CLIENT  # noqa: E402

from config import DB_CONFIG          # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SQL_FILES = [
    ('01_schema.sql',   '建库 + 10 张表 + 索引 + 生成列 + 外键 + 9 条 CHECK'),
    ('02_views.sql',    '6 个分析视图'),
    ('03_optimize.sql', 'EXPLAIN 调优演示 + skip_scan 对照 + 月度汇总表(DWS)'),
]


def run_sql_file(conn, path: Path):
    """执行一个 .sql 文件（允许多语句），返回受影响的语句数。"""
    sql = path.read_text(encoding='utf-8')
    with conn.cursor() as cur:
        cur.execute(sql)
        n = 1
        # MULTI_STATEMENTS 模式下，必须把结果集逐个消费掉，
        # 否则后续语句会报 "Commands out of sync"。
        while cur.nextset():
            n += 1
    conn.commit()
    return n


def main():
    import argparse
    ap = argparse.ArgumentParser(description='初始化数据库（会重建所有表）')
    ap.add_argument('--force', action='store_true',
                    help='已有数据时也直接重建（不加此参数会中止）')
    args = ap.parse_args()

    print('=' * 68)
    print('  智慧农业分析平台 —— 数据库初始化')
    print('=' * 68)
    print(f"  目标：{DB_CONFIG['user']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}"
          f"  库名 smart_agri")
    print()

    # 先不带库名连接（因为库可能还不存在），建库语句在 01_schema.sql 里
    cfg = {k: v for k, v in DB_CONFIG.items() if k != 'database'}
    try:
        conn = pymysql.connect(client_flag=CLIENT.MULTI_STATEMENTS, **cfg)
    except pymysql.err.OperationalError as e:
        print(f'❌ 连不上 MySQL：{e}')
        print('   检查：1) MySQL57 服务是否启动  2) .env 里的账号密码是否正确')
        return 1

    # ------------------------------------------------------------ 破坏性保护
    # 01_schema.sql 会 DROP 并重建所有表。如果不加保护，一次误执行就会
    # 悄悄清空已有数据 —— 这类"不可逆操作没有闸门"是真实事故的常见来源。
    # 有数据且未加 --force 时中止，让人明确知道自己在做什么。
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) FROM information_schema.TABLES
                       WHERE TABLE_SCHEMA='smart_agri' AND TABLE_NAME='yield_record'""")
        if cur.fetchone()[0]:
            cur.execute('SELECT COUNT(*) FROM `smart_agri`.`yield_record`')
            n = cur.fetchone()[0]
            if n > 0 and not args.force:
                print(f'⚠️  检测到 smart_agri.yield_record 已有 {n} 条数据。')
                print('   继续执行会 **删除所有表并重建**，已有数据全部丢失。')
                print('   确认要重建请加 --force：')
                print('       python scripts/init_db.py --force')
                print('   只想重新造数（不重建表）请用：')
                print('       python scripts/seed.py')
                conn.close()
                return 2
            if n > 0:
                print(f'⚠️  --force：将删除并重建所有表（当前 {n} 条产量记录会丢失）')
                print()

    ok = True
    for i, (fname, desc) in enumerate(SQL_FILES, start=1):
        path = BASE_DIR / 'sql' / fname
        if not path.exists():
            print(f'[{i}/{len(SQL_FILES)}] ❌ 找不到 {path}')
            ok = False
            continue
        print(f'[{i}/{len(SQL_FILES)}] 执行 {fname}  —— {desc} ...')
        try:
            n = run_sql_file(conn, path)
            print(f'         ✅ 完成（{n} 条语句）')
        except pymysql.err.Error as e:
            print(f'         ❌ 失败：{e}')
            print(f'            请检查 {path} 中出错位置附近')
            ok = False
            break

    if not ok:
        conn.close()
        return 1

    # ---------------------------------------------------------------- 校验
    print()
    print('-' * 68)
    print('初始化校验')
    print('-' * 68)
    with conn.cursor() as cur:
        cur.execute('USE `smart_agri`')

        cur.execute("""SELECT COUNT(*) FROM information_schema.TABLES
                       WHERE TABLE_SCHEMA='smart_agri' AND TABLE_TYPE='BASE TABLE'""")
        n_tables = cur.fetchone()[0]

        cur.execute("""SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
                       WHERE CONSTRAINT_SCHEMA='smart_agri' AND CONSTRAINT_TYPE='FOREIGN KEY'""")
        n_fk = cur.fetchone()[0]

        cur.execute("""SELECT COUNT(*) FROM information_schema.VIEWS
                       WHERE TABLE_SCHEMA='smart_agri'""")
        n_views = cur.fetchone()[0]

        cur.execute("""SELECT COUNT(*) FROM information_schema.STATISTICS
                       WHERE TABLE_SCHEMA='smart_agri'""")
        n_index_cols = cur.fetchone()[0]

        # 字符集必须全部是 utf8mb4，否则中文会乱码
        cur.execute("""SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES
                       WHERE TABLE_SCHEMA='smart_agri' AND TABLE_TYPE='BASE TABLE'
                         AND TABLE_COLLATION NOT LIKE 'utf8mb4%'""")
        bad_charset = cur.fetchall()

        # 生成列
        cur.execute("""SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
                       WHERE TABLE_SCHEMA='smart_agri' AND GENERATION_EXPRESSION <> ''
                       ORDER BY TABLE_NAME, ORDINAL_POSITION""")
        gen_cols = cur.fetchall()

        # CHECK 约束（8.0.16+ 才真正强制执行，5.7 只是解析后忽略）
        cur.execute("""SELECT TABLE_NAME, CONSTRAINT_NAME
                       FROM information_schema.TABLE_CONSTRAINTS
                       WHERE CONSTRAINT_SCHEMA='smart_agri' AND CONSTRAINT_TYPE='CHECK'
                       ORDER BY TABLE_NAME, CONSTRAINT_NAME""")
        checks = cur.fetchall()

    print(f'  基表          {n_tables} 张')
    print(f'  视图          {n_views} 个')
    print(f'  外键          {n_fk} 个')
    print(f'  索引列条目    {n_index_cols} 条（information_schema.STATISTICS 行数）')
    print(f'  生成列        {len(gen_cols)} 个')
    for t, c in gen_cols:
        print(f'                  {t}.{c}')

    print(f'  CHECK 约束    {len(checks)} 条  ← 8.0.16+ 才真正强制执行')

    if bad_charset:
        print(f'  ❌ 以下表不是 utf8mb4，中文会乱码：{bad_charset}')
        ok = False
    else:
        print('  字符集        全部 utf8mb4 ✅')

    conn.close()
    print()
    if ok:
        print('=' * 68)
        print('  ✅ 数据库初始化完成')
        print('  下一步（顺序不能换，gen_geojson 要读 seed 写进去的地块坐标）：')
        print('      1) python scripts/seed.py          # 造数')
        print('      2) python scripts/gen_geojson.py   # 生成地块边界')
        print('      3) python scripts/verify.py        # 跑正确性断言')
        print('=' * 68)
        return 0
    print('  ❌ 初始化存在问题，请查看上面的错误')
    return 1


if __name__ == '__main__':
    sys.exit(main())
