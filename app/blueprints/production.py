# -*- coding: utf-8 -*-
"""生产蓝图：种植批次、农事记录、产量记录。"""

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from ..decorators import admin_required, editor_required
from ..extensions import db
from ..models import (Crop, FarmingLog, InputCost, Planting, Plot,
                      YieldRecord)
from ..services import audit_service, planting_service
from ..services.planting_service import PlantingError

bp = Blueprint('production', __name__)

PER_PAGE = 15

# 产量记录的可编辑字段 + 变更审计要盯的字段（同一个清单，两个用途）。
#
# ⚠️ 这个清单里**绝对不能**出现 harvest_month / yield_per_mu / output_value。
#    它们是 STORED 生成列，MySQL 禁止显式赋值（errno 3105 直接报错）。
#    模型的这三个属性标了 server_default=FetchedValue()，只要没人给它赋值，
#    SQLAlchemy 就不会把它们写进 UPDATE 语句。
#
# 同理不含 planting_id / plot_id / crop_id / harvest_date：
#    前三个是从种植批次冗余来的分析维度，批次定了就永不变更；
#    harvest_date 以批次为准（见 yield_create 里的说明）。
YIELD_AUDIT_FIELDS = ['harvest_area_mu', 'yield_kg', 'unit_price', 'grade',
                      'moisture_content', 'quality_note']


def _parse_yield_form(form, plot=None):
    """
    解析产量表单的数值字段，返回 (values, error_message)。

    传入 plot 时会校验"收获面积不能超过地块总面积" —— 这一条以前只写在
    yield_create.html 的说明文字里，代码里其实并没有，属于**页面在承诺
    一个不存在的校验**。现在把它补成真的。

    录入(yield_create)和编辑(yield_edit)共用这一份解析 + 校验规则。
    两处各写一份是数据不一致的经典来源：改了录入的规则忘了改编辑的，
    就会出现"录入时拦得住、编辑时绕得过"的漏洞。
    values 里的 grade 可能是 None（字段缺失），由调用方决定默认值：
    录入默认一级，编辑保持原值 —— 这两者的"合理默认"本来就不同。
    """
    try:
        values = {
            'harvest_area_mu': float(form.get('harvest_area_mu') or 0),
            'yield_kg': float(form.get('yield_kg') or 0),
            'unit_price': float(form.get('unit_price') or 0),
            'moisture_content': float(form.get('moisture_content') or 0),
        }
    except ValueError:
        return None, '产量、面积、单价、含水率必须是数字'

    # 这两个下限同时也是数据库 CHECK 约束 ck_yield_nonneg 的要求，
    # 但 CHECK 只挡负数，挡不住 0 —— 而面积为 0 时单产没有意义
    # （生成列里那个 NULLIF 就是为此存在的）。
    if values['harvest_area_mu'] <= 0 or values['yield_kg'] <= 0:
        return None, '产量和收获面积必须大于 0'

    # 收获面积不能超过地块总面积。数据库层没有对应的 CHECK ——
    # 它要 JOIN 另一张表才知道地块多大，而 CHECK 约束只能看本行。
    # 这类"跨行/跨表"的规则正是应用层该守的地方。
    if plot is not None and values['harvest_area_mu'] > float(plot.area_mu):
        return None, (f"收获面积（{values['harvest_area_mu']:g} 亩）"
                      f"不能超过地块总面积（{float(plot.area_mu):g} 亩）")

    values['grade'] = form.get('grade', type=int)
    values['quality_note'] = (form.get('quality_note') or '').strip()
    return values, None


@bp.route('/plantings')
@login_required
def planting_list():
    """
    种植批次列表。

    ⚠️ 三个 joinedload：plot / crop / 以及 plot.farm。
       模板里要显示"地块编号 + 所属农场 + 作物名"，不加预加载的话
       15 行会产生 15×3 = 45 条 SQL。列表页是 N+1 的重灾区。
    """
    page = request.args.get('page', 1, type=int)
    status = request.args.get('status', type=int)
    crop_id = request.args.get('crop_id', type=int)
    season = (request.args.get('season') or '').strip()

    q = Planting.query.options(joinedload(Planting.plot).joinedload(Plot.farm),
                               joinedload(Planting.crop))
    if status:
        q = q.filter(Planting.status == status)
    if crop_id:
        q = q.filter(Planting.crop_id == crop_id)
    if season:
        q = q.filter(Planting.season.like(f'%{season}%'))

    pagination = (q.order_by(Planting.season_seq.desc(), Planting.id.desc())
                  .paginate(page=page, per_page=PER_PAGE, error_out=False))

    return render_template('planting_list.html',
                           pagination=pagination,
                           crops=Crop.query.order_by(Crop.id).all(),
                           status=status, crop_id=crop_id, season=season)


@bp.route('/plantings/<int:pid>')
@login_required
def planting_detail(pid):
    p = (Planting.query
         .options(joinedload(Planting.plot).joinedload(Plot.farm),
                  joinedload(Planting.crop))
         .get_or_404(pid))
    logs = (FarmingLog.query.options(joinedload(FarmingLog.operator))
            .filter_by(planting_id=pid).order_by(FarmingLog.op_date).all())
    costs = (InputCost.query.filter_by(planting_id=pid)
             .order_by(InputCost.record_date).all())

    # 累计有效积温（Python 侧算一遍，与 SQL 分析 1 互为验证）
    gdd = planting_service.compute_gdd(pid)
    tips = planting_service.suggest_next_actions(p)

    # 成本汇总
    material = sum(float(c.amount or 0) for c in costs)
    labor = sum(float(l.labor_cost or 0) for l in logs)
    machine = sum(float(l.machine_cost or 0) for l in logs)

    return render_template('planting_detail.html', p=p, logs=logs, costs=costs,
                           gdd=gdd, tips=tips,
                           material=material, labor=labor, machine=machine,
                           total_cost=material + labor + machine)


@bp.route('/plantings/<int:pid>/status', methods=['POST'])
@login_required
@editor_required
def change_status(pid):
    """
    状态流转。

    非法流转由服务层的状态机拦住 —— 视图层只负责把异常转成提示。
    这就是"业务规则集中在服务层"的收益：换个入口调用，
    规则依然生效，不会因为某处忘了校验而漏过。
    """
    p = Planting.query.get_or_404(pid)
    new_status = request.form.get('status', type=int)
    if not new_status:
        flash('未指定目标状态', 'warning')
        return redirect(url_for('production.planting_detail', pid=pid))

    try:
        old, new = planting_service.change_status(p, new_status)
    except PlantingError as e:
        flash(str(e), 'danger')
        return redirect(url_for('production.planting_detail', pid=pid))

    audit_service.log('STATUS_CHANGE', 'planting', p.id,
                      before={'status': old}, after={'status': new})
    db.session.commit()
    flash(f'状态已更新：{old} → {new}', 'success')
    return redirect(url_for('production.planting_detail', pid=pid))


@bp.route('/yields')
@login_required
def yield_list():
    page = request.args.get('page', 1, type=int)
    crop_id = request.args.get('crop_id', type=int)

    q = YieldRecord.query.options(joinedload(YieldRecord.crop),
                                  joinedload(YieldRecord.plot))
    if crop_id:
        q = q.filter(YieldRecord.crop_id == crop_id)

    pagination = (q.order_by(YieldRecord.harvest_date.desc())
                  .paginate(page=page, per_page=PER_PAGE, error_out=False))
    return render_template('yield_list.html', pagination=pagination,
                           crops=Crop.query.order_by(Crop.id).all(),
                           crop_id=crop_id)


@bp.route('/yields/new', methods=['GET', 'POST'])
@login_required
@editor_required
def yield_create():
    """录入产量。只列出"已收获但还没录产量"的批次，避免重复录入。"""
    if request.method == 'POST':
        pid = request.form.get('planting_id', type=int)
        p = Planting.query.get(pid) if pid else None
        if not p:
            flash('请选择种植批次', 'warning')
            return redirect(url_for('production.yield_create'))
        if p.yield_record:
            flash('该批次已有产量记录，不能重复录入', 'warning')
            return redirect(url_for('production.planting_detail', pid=pid))

        # 解析 + 校验走共用函数（原先含水率在 try 之外直接 float()，
        # 填非数字会变成未捕获的 ValueError → 500，而不是一条可读提示）。
        values, err = _parse_yield_form(request.form, p.plot)
        if err:
            flash(err, 'warning')
            return redirect(url_for('production.yield_create'))

        # 状态校验放在**建产量记录之前**，两个理由：
        #   ① 非法流转（如"待播种"直接跳"已收获"）要在这里被挡住。
        #      若先把 rec 加进 session 再靠 rollback 撤销，拦截就依赖
        #      框架 teardown 的隐式回滚 —— 那是"碰巧能对"，不是"设计上对"。
        #   ② change_status 推进到 40 时会补上 harvest_date（见服务层），
        #      正好给下面 rec.harvest_date 的兜底值 p.harvest_date 兜底；
        #      顺序反过来时它可能还是 None，写库会撞 NOT NULL 约束。
        # 这里不需要 rollback：校验跑在任何写入之前，session 还是干净的。
        # 状态机里 30 → 40 是进入"已收获"的唯一合法路径 —— 想录产量就得
        # 先把状态按流程推进过去，这正是"非法流转挡在入口"的意义。
        if p.status != 40:
            try:
                planting_service.change_status(p, 40)
            except PlantingError as e:
                flash(f'无法录入产量：{e}', 'danger')
                return redirect(url_for('production.planting_detail', pid=p.id))

        # 收获日以**批次记录**为唯一来源，不收表单值。
        # 原先写的是 `_parse_date(表单) or p.harvest_date`，等于允许两处日期
        # 各说各话 —— 实测写出过 harvest_date=2025-08-30 而批次是 2025-08-23
        # 的记录。这种不一致**不会报错**，只会让"这条产量哪天收的"有两个答案，
        # 并直接违反断言「yield_record 收获日 = planting 收获日」。
        # 前面的状态推进已保证 harvest_date 非空，这里兜底最后一种情况：
        # 状态是 40 却没有收获日期的批次 —— 宁可挡住，也不要写出一条
        # NOT NULL 违例的 500。
        if not p.harvest_date:
            flash('该批次没有收获日期，请先在批次详情页补上再录入产量', 'warning')
            return redirect(url_for('production.planting_detail', pid=p.id))

        rec = YieldRecord(
            planting_id=p.id, plot_id=p.plot_id, crop_id=p.crop_id,
            harvest_date=p.harvest_date,
            harvest_area_mu=values['harvest_area_mu'],
            yield_kg=values['yield_kg'],
            grade=values['grade'] or 1,          # 录入时默认一级
            unit_price=values['unit_price'],
            moisture_content=values['moisture_content'],
            quality_note=values['quality_note'],
            recorder_id=current_user.id,
        )
        db.session.add(rec)
        # flush 一次拿自增主键：审计的 target_table 是 yield_record，
        # target_id 就必须是**产量记录的 id**。原先这里传的是 p.id（批次 id），
        # 于是审计页的"目标记录"一直指向另一张表里的另一条数据。
        # flush 只发 INSERT、不提交事务，审计日志仍然和业务变更同一个事务。
        db.session.flush()
        # after 的键名取 diff_dict 之外的真实列名，两边保持一套名字 ——
        # changed_fields 生成列直接取 detail 里的 key，用 'area' 这种简写
        # 会让审计页的"最常变更字段"里出现一个数据库里不存在的字段名。
        audit_service.log('CREATE', 'yield_record', rec.id,
                          after=audit_service.diff_dict(rec,
                                                        YIELD_AUDIT_FIELDS))
        db.session.commit()
        flash('产量记录已录入', 'success')
        return redirect(url_for('production.yield_list'))

    pending = (Planting.query
               .options(joinedload(Planting.plot), joinedload(Planting.crop))
               .outerjoin(YieldRecord, YieldRecord.planting_id == Planting.id)
               .filter(Planting.status == 40, YieldRecord.id.is_(None))
               .order_by(Planting.harvest_date.desc()).all())
    return render_template('yield_create.html', pending=pending)


@bp.route('/yields/<int:yid>/edit', methods=['GET', 'POST'])
@login_required
@editor_required
def yield_edit(yid):
    """
    修改一条产量记录。

    【只能改"人录进去的那几个数"，其余一律不给入口】
      · harvest_date 跟随批次 —— 理由见 yield_create 里的长注释。
        不给它输入框，是比"填了再纠正"更彻底的做法。
      · planting_id / plot_id / crop_id 是从批次冗余来的分析维度，
        批次一定下来就永不变更（冗余之所以安全，就是因为被冗余的字段不可变）。
      · yield_per_mu / output_value / harvest_month 是 STORED 生成列，
        MySQL 在写入时自动重算 —— 改完总产量，单产和产值会跟着变，
        不需要应用层记得同步。**显式给它们赋值会报 errno 3105**，
        所以它们既不在 YIELD_AUDIT_FIELDS 里，也不在表单里。
    """
    rec = YieldRecord.query.get_or_404(yid)

    if request.method == 'POST':
        # 变更前先抓快照 —— 审计日志要能回答"把什么改成了什么"
        before = audit_service.diff_dict(rec, YIELD_AUDIT_FIELDS)

        values, err = _parse_yield_form(request.form, rec.plot)
        if err:
            # 先把用户填的值落到对象上再报错，重渲染时表单里还是他填的内容，
            # 不会让他白填一遍。脏对象由请求结束时的会话清理丢弃，不会落库。
            #
            # ⚠️ 这里**必须**逐字段 try：err 有可能就是因为某个字段不是数字，
            #    裸 float() 会让"友好的校验提示"变成第二个 500 ——
            #    错误处理路径自己抛异常是最难查的一类 bug。
            #    解析不了的字段就跳过，页面显示原值即可。
            for field in ('harvest_area_mu', 'yield_kg',
                          'unit_price', 'moisture_content'):
                try:
                    setattr(rec, field, float(request.form.get(field) or 0))
                except ValueError:
                    pass
            rec.quality_note = (request.form.get('quality_note') or '').strip()
            flash(err, 'warning')
            return render_template('yield_edit.html', y=rec)

        rec.harvest_area_mu = values['harvest_area_mu']
        rec.yield_kg = values['yield_kg']
        rec.unit_price = values['unit_price']
        rec.moisture_content = values['moisture_content']
        rec.quality_note = values['quality_note']
        # 编辑时 grade 缺失就保持原值（不是默认一级）——
        # "没提交这个字段"和"提交了一级"是两件事，合并处理会静默改数据。
        rec.grade = values['grade'] or rec.grade

        after = audit_service.diff_dict(rec, YIELD_AUDIT_FIELDS)
        audit_service.log('UPDATE', 'yield_record', rec.id,
                          before=before, after=after)
        db.session.commit()
        flash('产量记录已更新（单产、产值已由数据库重新计算）', 'success')
        return redirect(url_for('production.yield_list'))

    return render_template('yield_edit.html', y=rec)


@bp.route('/yields/<int:yid>/delete', methods=['POST'])
@login_required
@admin_required
def yield_delete(yid):
    """
    删除一条产量记录。

    【删完之后批次怎么办 —— 保持"已收获"不动】
      状态机里 40 是终态（见 models/operation.py 的 STATUS_FLOW），没有回退路径，
      所以这里不把批次退回"成熟待收"。批次会重新出现在「录入产量」的待办列表里
      —— 那个页面的查询条件就是「status=40 且没有产量记录」，唯一约束 uk_planting
      也随记录一起释放，可以重新录入。
      这不是权宜之计：planting_service.suggest_next_actions() 本来就会对这类批次
      提示"已标记收获，但尚未录入产量记录"，是设计好的待办状态。

    【为什么删除只给管理员，编辑却给农艺师】
      编辑是修正数字，删除是**不可逆地销毁**一条已经进入全部分析的记录 ——
      产量汇总、单产排行、ABC 帕累托、投入产出比都会跟着变。
      按"破坏半径"分配权限：可逆的操作放开，不可逆的收紧。
    """
    rec = YieldRecord.query.get_or_404(yid)
    batch_no = rec.planting.batch_no if rec.planting else None

    # 先把快照写进审计再删 —— 记录删掉以后，连"删的是哪一条、当时是什么值"
    # 都无从查证了。审计表只增不改的意义正在于此。
    audit_service.log('DELETE', 'yield_record', rec.id,
                      before=audit_service.diff_dict(rec, YIELD_AUDIT_FIELDS),
                      extra={'planting_id': rec.planting_id,
                             'batch_no': batch_no})
    db.session.delete(rec)
    db.session.commit()
    flash('产量记录已删除，该批次已回到「录入产量」的待办列表，可以重新录入',
          'success')
    return redirect(url_for('production.yield_list'))


@bp.route('/farming-logs')
@login_required
def farming_log_list():
    page = request.args.get('page', 1, type=int)
    q = (FarmingLog.query
         .options(joinedload(FarmingLog.planting).joinedload(Planting.plot),
                  joinedload(FarmingLog.operator)))
    pagination = (q.order_by(FarmingLog.op_date.desc())
                  .paginate(page=page, per_page=PER_PAGE, error_out=False))
    return render_template('farming_log_list.html', pagination=pagination)
