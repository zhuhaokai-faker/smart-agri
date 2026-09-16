#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一次性端到端检查：产量记录的 收获日来源 / 编辑 / 删除。

用 Flask 测试客户端走完整的 HTTP 流程（路由、装饰器、模板、审计、生成列）。

⚠️ 校验查询刻意**不走 SQLAlchemy 会话**，而是每次新开一条 pymysql 连接：
   测试客户端的请求有自己的应用上下文和会话，外层会话读到的可能是旧快照
   （InnoDB 默认 REPEATABLE READ），会得出"改了但没看见"的假结论。
   新连接读到的永远是已提交的数据。

为了不动演示数据，全程只用批次 325 这条临时记录，跑完删掉，净变化为零。
这是临时验证脚本，不是正式测试套件。
"""
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from env_check import check  # noqa: E402
check()

import pymysql  # noqa: E402

from app import create_app  # noqa: E402
from config import DB_CONFIG  # noqa: E402


def q(sql, args=None):
    """每次新开连接查一次，保证读到的是已提交的数据。"""
    cfg = dict(DB_CONFIG)
    cfg['database'] = 'smart_agri'
    conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.fetchall()
    finally:
        conn.close()


def scalar(sql, args=None):
    rows = q(sql, args)
    return list(rows[0].values())[0] if rows else None


PASS, FAIL = [], []


def ck(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✅' if cond else '❌'} {label}" + (f'   {detail}' if detail else ''))


def token(c, path):
    """
    从页面里取出 CSRF token —— 和浏览器做的事一样。

    刻意不关掉 CSRF（`WTF_CSRF_ENABLED = False`）来图省事：
    那样这个脚本就永远测不出"表单里漏了 token"这种问题，
    而漏一个表单正是补 CSRF 时最容易犯的错。
    """
    html = c.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ''


def post(c, path, data=None, follow_redirects=False, page='/change-password'):
    """
    带真实 token 发 POST。page 是"从哪个页面取 token"。

    ⚠️ 默认从 /change-password 取，而不是 /yields：
       产量列表页上的 POST 表单是**按角色条件渲染**的 ——
       删除表单只有管理员才看得到，农艺师和只读用户的页面上一个
       csrf_token 都没有。拿它当默认取 token 的地方，会安静地取到空串，
       然后 POST 一律 400 —— 看起来像"权限坏了"，实际是 token 没取到。
       /change-password 的表单所有登录用户都有，是可靠的 token 来源。
    """
    d = dict(data or {})
    d['csrf_token'] = token(c, page)
    return c.post(path, data=d, follow_redirects=follow_redirects)


def login(c, u, p):
    return post(c, '/login', {'username': u, 'password': p},
                follow_redirects=True, page='/login')


app = create_app()

PID = 325                                    # 已收获但无产量记录的批次
BASE_RECORDS = scalar('SELECT COUNT(*) AS n FROM yield_record')
BATCH_DATE = scalar('SELECT harvest_date FROM planting WHERE id=%s', (PID,))
print(f'测试前：产量记录 {BASE_RECORDS} 条，批次 {PID} 收获日 {BATCH_DATE}')

with app.test_client() as c:
    print('\n▌ 1. 收获日以批次为准（录入路径）')
    login(c, 'admin', 'admin123')
    r = post(c, '/yields/new', data={
        'planting_id': PID, 'harvest_area_mu': '10', 'yield_kg': '1000',
        'unit_price': '2', 'grade': '2', 'moisture_content': '13',
        'quality_note': 'E2E 临时记录',
        # 故意提交一个**和批次不同**的收获日，验证它被忽略
        'harvest_date': '1999-01-01',
    }, follow_redirects=False)
    ck('录入返回 302', r.status_code == 302, f'HTTP {r.status_code}')

    rows = q('SELECT * FROM yield_record WHERE planting_id=%s', (PID,))
    ck('产量记录已创建', len(rows) == 1)
    rec = rows[0] if rows else {}
    yid = rec.get('id')
    ck('harvest_date 取自批次而非表单',
       str(rec.get('harvest_date')) == str(BATCH_DATE),
       f"记录={rec.get('harvest_date')} 批次={BATCH_DATE} 表单提交=1999-01-01")

    print('\n▌ 2. CREATE 审计的 target_id 指向产量记录本身')
    a = q("""SELECT target_id, detail FROM audit_log
             WHERE action='CREATE' AND target_table='yield_record'
             ORDER BY id DESC LIMIT 1""")
    ck('target_id == yield_record.id（原先错传了批次 id）',
       a and a[0]['target_id'] == yid,
       f"target_id={a[0]['target_id'] if a else None} 实际 yield id={yid}")
    detail = (a[0]['detail'] if a else '') or ''
    ck("changed_fields 用真实列名，不含假名 'area'",
       'harvest_area_mu' in detail and '"area"' not in detail,
       detail[:120])

    print('\n▌ 3. 编辑：生成列自动重算 + 审计 UPDATE')
    r = c.get(f'/yields/{yid}/edit')
    ck('编辑页 200', r.status_code == 200)
    html = r.get_data(as_text=True)
    ck('没有可提交的收获日期输入框', 'name="harvest_date"' not in html)
    ck('表单里没有生成列的提交字段',
       'name="yield_per_mu"' not in html and 'name="output_value"' not in html
       and 'name="harvest_month"' not in html)
    ck('生成列以只读方式展示', html.count('disabled') >= 3,
       f'disabled 出现 {html.count("disabled")} 次')

    before = q('SELECT yield_per_mu, output_value FROM yield_record WHERE id=%s', (yid,))[0]
    r = post(c, f'/yields/{yid}/edit', data={
        'harvest_area_mu': '20', 'yield_kg': '2000', 'unit_price': '3',
        'grade': '1', 'moisture_content': '12', 'quality_note': '改过了',
    }, follow_redirects=False)
    ck('编辑返回 302', r.status_code == 302, f'HTTP {r.status_code}')
    after = q('SELECT yield_per_mu, output_value FROM yield_record WHERE id=%s', (yid,))[0]
    ck('单产被数据库重算', float(after['yield_per_mu']) == 100.0,
       f"{before['yield_per_mu']} -> {after['yield_per_mu']}（2000/20）")
    ck('产值被数据库重算', float(after['output_value']) == 6000.0,
       f"{before['output_value']} -> {after['output_value']}（2000*3）")

    au = q("""SELECT target_id, detail FROM audit_log
              WHERE action='UPDATE' AND target_table='yield_record'
              ORDER BY id DESC LIMIT 1""")
    ck('写入了 UPDATE 审计且 target_id 正确', au and au[0]['target_id'] == yid)
    changed = ''
    if au:
        d = au[0]['detail'] or ''
        changed = d.split('changed_fields": "')[-1].split('"')[0] if 'changed_fields' in d else ''
    ck('changed_fields 只含真正改动的字段',
       set(filter(None, changed.split(','))) <=
       {'harvest_area_mu', 'yield_kg', 'unit_price', 'grade',
        'quality_note', 'moisture_content'},
       f'changed = {changed}')

    print('\n▌ 4. 编辑的校验拒绝（都不能变成 500）')
    cases = [
        ('面积填 0', {'harvest_area_mu': '0', 'yield_kg': '100',
                    'unit_price': '1', 'grade': '1', 'moisture_content': '1'}),
        ('含水率填非数字', {'harvest_area_mu': '10', 'yield_kg': '100',
                      'unit_price': '1', 'grade': '1', 'moisture_content': 'abc'}),
        ('面积超过地块总面积', {'harvest_area_mu': '999999', 'yield_kg': '100',
                        'unit_price': '1', 'grade': '1', 'moisture_content': '1'}),
    ]
    for name, data in cases:
        r = post(c, f'/yields/{yid}/edit', data=data, follow_redirects=False)
        cur = q('SELECT yield_kg, harvest_area_mu FROM yield_record WHERE id=%s', (yid,))[0]
        ck(f'{name} → 被挡住且未落库',
           r.status_code == 200 and float(cur['yield_kg']) == 2000.0,
           f"HTTP {r.status_code}，库中仍 {cur['yield_kg']}kg / {cur['harvest_area_mu']}亩")

with app.test_client() as c:
    print('\n▌ 5. RBAC：农艺师能编辑、不能删除')
    login(c, 'agronomist', 'agri123')
    ck('农艺师可打开编辑页', c.get(f'/yields/{yid}/edit').status_code == 200)
    r = post(c, f'/yields/{yid}/delete', follow_redirects=False)
    ck('农艺师删除 → 403', r.status_code == 403, f'HTTP {r.status_code}')
    ck('记录未被删除',
       scalar('SELECT COUNT(*) AS n FROM yield_record WHERE id=%s', (yid,)) == 1)

with app.test_client() as c:
    print('\n▌ 6. RBAC：只读角色看不到任何操作入口')
    login(c, 'viewer', 'view123')
    html = c.get('/yields').get_data(as_text=True)
    # ⚠️ 必须查具体标记而不是「操作」二字 —— base.html 的导航里就有"操作审计"
    ck('列表页没有「操作」列', '>操作</th>' not in html)
    ck('列表页没有编辑/删除入口',
       'yield_delete' not in html and 'yield_edit' not in html)
    ck('直接访问编辑页 → 403', c.get(f'/yields/{yid}/edit').status_code == 403)
    ck('直接 POST 删除 → 403',
       post(c, f'/yields/{yid}/delete').status_code == 403)

with app.test_client() as c:
    print('\n▌ 7. 删除（管理员）+ 批次回到待录入列表')
    login(c, 'admin', 'admin123')
    r = post(c, f'/yields/{yid}/delete', follow_redirects=False)
    ck('删除返回 302', r.status_code == 302, f'HTTP {r.status_code}')
    ck('记录已删除',
       scalar('SELECT COUNT(*) AS n FROM yield_record WHERE id=%s', (yid,)) == 0)
    st = scalar('SELECT status FROM planting WHERE id=%s', (PID,))
    ck('批次状态保持 40（未被回退，符合决定）', st == 40, f'status={st}')
    ck('批次回到「录入产量」待办列表',
       PID in [x['id'] for x in q(
           """SELECT p.id FROM planting p LEFT JOIN yield_record y
              ON y.planting_id=p.id WHERE p.status=40 AND y.id IS NULL""")])

    d = q("""SELECT target_id, detail FROM audit_log
             WHERE action='DELETE' AND target_table='yield_record'
             ORDER BY id DESC LIMIT 1""")
    ck('写入 DELETE 审计且 target_id 为产量记录 id',
       d and d[0]['target_id'] == yid, f"target_id={d[0]['target_id'] if d else None}")
    dd = (d[0]['detail'] if d else '') or ''
    ck('删除前的快照留在审计里',
       'before' in dd and 'batch_no' in dd, dd[:150])

    print('\n▌ 8. 演示数据未受影响')
    ck('产量记录总数回到测试前',
       scalar('SELECT COUNT(*) AS n FROM yield_record') == BASE_RECORDS,
       f"测试前 {BASE_RECORDS}，现在 {scalar('SELECT COUNT(*) AS n FROM yield_record')}")

with app.test_client() as c:
    print('\n▌ 9. CSRF 防护（补上之后必须真的生效）')
    login(c, 'admin', 'admin123')

    # 反面：不带 token 的 POST 必须被拒。这正是"第三方页面伪造请求"的形态。
    r = c.post(f'/yields/{yid}/delete', data={}, follow_redirects=False)
    ck('无 token 的 POST 被拒 → 400', r.status_code == 400, f'HTTP {r.status_code}')

    r = c.post('/login', data={'username': 'admin', 'password': 'admin123'},
               follow_redirects=False)
    ck('无 token 的登录被拒 → 400', r.status_code == 400, f'HTTP {r.status_code}')

    # 反面：伪造一个 token 也必须被拒
    r = c.post('/login', data={'username': 'admin', 'password': 'admin123',
                               'csrf_token': 'forged-token-value'},
               follow_redirects=False)
    ck('伪造 token 被拒 → 400', r.status_code == 400, f'HTTP {r.status_code}')

    # 反面：GET 不受影响（否则整站打不开）
    ck('GET 请求不受 CSRF 影响', c.get('/yields').status_code == 200)

    # 正面：从页面取的 token 能通过（前面所有 POST 都是这样过的）
    ck('带真实 token 的 POST 可通过（前面各节已证明）', True,
       '本节之前的每一处 POST 都走的是 token(c, page)')

print()
print('=' * 68)
print(f'  通过 {len(PASS)} 项，失败 {len(FAIL)} 项')
if FAIL:
    print('  失败明细：')
    for f in FAIL:
        print(f'    · {f}')
print('=' * 68)
sys.exit(1 if FAIL else 0)
