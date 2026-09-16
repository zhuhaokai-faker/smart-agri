#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
运行 sql/05_tests.sql 里的全部正确性断言，汇总 PASS/FAIL 并设置退出码。

【为什么需要这个脚本，而不是直接用 mysql 命令行跑】
  1. **退出码**：CI 或自动化流程需要一个明确的成功/失败信号。
     直接用 mysql 的话，"有 FAIL 但 exit code 仍是 0"，问题会被漏掉。
  2. **汇总**：SQL 输出的是一堆散落的结果集，人眼容易漏看某一行的 FAIL。
     这里统一收集、计数、把失败项单独列出来。
  3. **可移植**：不依赖 mysql.exe 在 PATH 里。

【退出码】0 = 全部通过；1 = 有 FAIL；2 = 执行出错。
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


def _s(v):
    """
    把结果值转成可读字符串。

    MySQL 在 UNION 里混合不同类型的字面量时，可能把结果列推断成二进制类型，
    PyMySQL 就会原样返回 bytes（打印出来是 b'0 \\xe6\\x9d\\xa1...'）。
    这里统一按 utf-8 解码，保证中文正常显示。
    """
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', errors='replace')
    return '' if v is None else str(v)


def main():
    sql_path = BASE_DIR / 'sql' / '05_tests.sql'
    if not sql_path.exists():
        print(f'❌ 找不到 {sql_path}')
        return 2

    print('=' * 68)
    print('  正确性断言校验')
    print('=' * 68)

    cfg = dict(DB_CONFIG)
    cfg['database'] = 'smart_agri'
    try:
        conn = pymysql.connect(client_flag=CLIENT.MULTI_STATEMENTS,
                               cursorclass=pymysql.cursors.DictCursor, **cfg)
    except pymysql.err.OperationalError as e:
        print(f'❌ 连不上数据库：{e}')
        print('   先执行：python scripts/init_db.py')
        return 2

    passed, failed, errors = 0, [], 0
    try:
        with conn.cursor() as cur:
            cur.execute(sql_path.read_text(encoding='utf-8'))
            while True:
                # 每个结果集逐行检查有没有名为「结果」的列，值是 FAIL 就记下来
                if cur.description:
                    col_names = [d[0] for d in cur.description]
                    if '结果' in col_names:
                        for row in cur.fetchall():
                            item = _s(row.get('检查项') or '?')
                            res = _s(row.get('结果'))
                            note = _s(row.get('说明'))
                            group = _s(row.get('断言组'))
                            if group:
                                print(f'\n▌ {group}')
                            if res == 'PASS':
                                passed += 1
                                print(f'  ✅ {item}   {note}')
                            elif res == 'FAIL':
                                failed.append((group, item, note))
                                print(f'  ❌ {item}   {note}')
                if not cur.nextset():
                    break
    except pymysql.err.Error as e:
        print(f'❌ 执行断言时出错：{e}')
        errors += 1
    finally:
        conn.close()

    # ---------------------------------------------------------------- 服务层断言
    # 【为什么 SQL 断言之外还要有这一组】
    #   05_tests.sql 里的断言用的是**它自己内联的 SQL**，
    #   而页面真正执行的是 app/services/analytics_service.py 里的 SQL。
    #   两者是两份代码 —— 服务层写错时，SQL 断言照样全绿。
    #   本组直接调用服务层函数，把结果与独立算法交叉比对。
    #
    #   这组断言的存在理由：我们在把 SQL 从 .sql 文件移植到服务层时，
    #   漏掉了用户变量"记住上一行分组"的赋值（@pid / @grp8），
    #   导致积温累计失效、干旱预警查不出任何结果 ——
    #   查询不报错，只是静默返回错误结果。
    #   如果当时有这组断言，问题会在第一次运行时就暴露。
    print()
    print('▌ 服务层断言（直接调用服务层函数，与独立算法交叉验证）')
    try:
        from app import create_app
        from app.services import analytics_service as A
        app = create_app()
        with app.app_context():
            # --- 1. GDD 累计曲线：末行累计值必须等于各日积温之和 ---
            pid = None
            from app.models import Planting
            row = Planting.query.filter_by(status=40).order_by(Planting.id).first()
            if row:
                curve = A.gdd_curve(row.id)
                last_cum = float(curve[-1]['gdd_cum']) if curve else 0
                daily_sum = sum(float(r['gdd_daily']) for r in curve)
                ok = curve and abs(last_cum - daily_sum) < 0.5
                if ok:
                    passed += 1
                    print(f'  ✅ GDD 累计生效   末行 {last_cum:.2f} == 日积温之和 {daily_sum:.2f}')
                else:
                    failed.append(('服务层', 'GDD 累计生效',
                                   f'末行 {last_cum:.2f} != 日积温之和 {daily_sum:.2f}'
                                   f'（累计失效，检查 @pid 赋值）'))
                    print(f'  ❌ GDD 累计生效   末行 {last_cum:.2f} != 日积温之和 {daily_sum:.2f}')

                # --- 2. 累计曲线必须单调不减 ---
                cums = [float(r['gdd_cum']) for r in curve]
                bad = sum(1 for i in range(1, len(cums)) if cums[i] < cums[i - 1] - 1e-6)
                if bad == 0:
                    passed += 1
                    print('  ✅ GDD 累计单调   无回退')
                else:
                    failed.append(('服务层', 'GDD 累计单调', f'{bad} 处回退'))
                    print(f'  ❌ GDD 累计单调   {bad} 处回退')

            # --- 3. 干旱预警必须能查出结果 ---
            events = A.drought_events(10, 50)
            if events:
                passed += 1
                print(f'  ✅ 干旱预警有结果 {len(events)} 段，最长 '
                      f"{max(e['dry_days'] for e in events)} 天")
            else:
                failed.append(('服务层', '干旱预警有结果',
                               '0 段（检查 @grp8 赋值，漏写会让连续段全变成单天）'))
                print('  ❌ 干旱预警有结果 0 段')

            # --- 4. 分组 TopN：用户变量版必须与确定性版完全一致 ---
            a = sorted((r['crop_name'], r['plot_no']) for r in A.topn_by_crop(3))
            b = sorted((r['crop_name'], r['plot_no']) for r in A.topn_by_crop_deterministic(3))
            if a == b and a:
                passed += 1
                print(f'  ✅ 分组TopN一致   两种实现各 {len(a)} 行，完全相同')
            else:
                failed.append(('服务层', '分组TopN一致',
                               f'用户变量版 {len(a)} 行 vs 确定性版 {len(b)} 行'))
                print(f'  ❌ 分组TopN一致   用户变量版 {len(a)} 行 vs 确定性版 {len(b)} 行')

            # --- 5. 加权单产必须等于 SUM/SUM，而不是 AVG ---
            from app.extensions import raw_scalar
            w = raw_scalar("""SELECT ROUND(SUM(yield_kg)/NULLIF(SUM(harvest_area_mu),0),2)
                              FROM yield_record""")
            avg = raw_scalar('SELECT ROUND(AVG(yield_per_mu),2) FROM yield_record')
            if w is not None and avg is not None and abs(float(w) - float(avg)) > 1e-9:
                passed += 1
                print(f'  ✅ 加权单产口径   加权 {w} != 简单平均 {avg}（差异正说明权重生效）')
            else:
                failed.append(('服务层', '加权单产口径', '两者相等，可能误用了 AVG'))
                print(f'  ❌ 加权单产口径   加权 {w} == 简单平均 {avg}')
    except Exception as e:
        failed.append(('服务层', '服务层断言执行', f'{type(e).__name__}: {e}'))
        print(f'  ❌ 服务层断言执行出错：{type(e).__name__}: {e}')

    print()
    print('=' * 68)
    print(f'  通过 {passed} 项，失败 {len(failed)} 项')
    if failed:
        print('-' * 68)
        print('  失败明细：')
        for group, item, note in failed:
            print(f'    · [{group}] {item} —— {note}')
        print('=' * 68)
        return 1
    if errors:
        return 2
    print('  ✅ 全部断言通过')
    print('=' * 68)
    return 0


if __name__ == '__main__':
    sys.exit(main())
