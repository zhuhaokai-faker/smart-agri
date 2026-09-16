# -*- coding: utf-8 -*-
"""
生产过程模型：种植批次、农事记录、农资投入。

这三张表是"记录发生了什么"的过程数据；
结果数据（产量）在 result.py 里。分开的原因见 planting 下面的注释。
"""

from datetime import datetime

from sqlalchemy import DECIMAL, Date, DateTime, ForeignKey, String
from sqlalchemy.dialects.mysql import INTEGER, SMALLINT, TINYINT
from sqlalchemy.orm import relationship
from sqlalchemy import FetchedValue

from ..extensions import db


class Planting(db.Model):
    """
    种植批次 —— 地块 × 作物 × 茬口。

    这是整个系统的枢纽：农事记录、农资投入、产量记录都挂在它上面。
    """
    __tablename__ = 'planting'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    batch_no = db.Column(String(40), unique=True, nullable=False)
    plot_id = db.Column(INTEGER(unsigned=True), ForeignKey('plot.id'), nullable=False)
    crop_id = db.Column(INTEGER(unsigned=True), ForeignKey('crop.id'), nullable=False)
    season = db.Column(String(20), nullable=False)
    season_year = db.Column(SMALLINT(unsigned=True), nullable=False)
    # season_seq = 年*2 + (春=1/秋=2)，**线性递增**。
    # 同期群分析和"连续种植季"判定都依赖"相邻茬口差 1"这个性质，
    # 所以必须用线性序号而不是年份或年月编码。
    season_seq = db.Column(SMALLINT(unsigned=True), nullable=False)
    plant_area_mu = db.Column(DECIMAL(10, 2), nullable=False, default=0)
    density_per_mu = db.Column(INTEGER(unsigned=True), nullable=False, default=0)
    sow_date = db.Column(Date, nullable=False)
    emerge_date = db.Column(Date)
    flower_date = db.Column(Date)
    mature_date = db.Column(Date)
    harvest_date = db.Column(Date)
    status = db.Column(TINYINT(unsigned=True), nullable=False, default=10)
    agronomist_id = db.Column(INTEGER(unsigned=True), ForeignKey('user.id'))
    remark = db.Column(String(255), nullable=False, default='')
    created_at = db.Column(DateTime, default=datetime.now)
    updated_at = db.Column(DateTime, default=datetime.now, onupdate=datetime.now)

    plot = relationship('Plot', back_populates='plantings')
    crop = relationship('Crop')
    agronomist = relationship('User')
    farming_logs = relationship('FarmingLog', back_populates='planting',
                                cascade='all, delete-orphan')
    input_costs = relationship('InputCost', back_populates='planting',
                               cascade='all, delete-orphan')
    # 一个批次最多一条产量记录（数据库层面由 uk_planting 唯一约束保证），
    # 所以是 uselist=False 的一对一关系。
    yield_record = relationship('YieldRecord', back_populates='planting',
                                uselist=False, cascade='all, delete-orphan')

    # ------------------------------------------------------------------ 状态机
    # 合法的状态流转。这是本项目的业务核心之一：
    # 农事流程是不可逆的，已经收获的批次不能再退回"生长中"。
    STATUS_FLOW = {
        10: (20, 50),        # 待播种 -> 生长中 / 已废弃
        20: (30, 50),        # 生长中 -> 成熟待收 / 已废弃
        30: (40,),           # 成熟待收 -> 已收获
        40: (),              # 已收获是终态
        50: (),              # 已废弃是终态
    }

    @property
    def status_name(self):
        from config import PLANTING_STATUS
        return PLANTING_STATUS.get(self.status, ('未知', 'secondary'))[0]

    @property
    def status_color(self):
        from config import PLANTING_STATUS
        return PLANTING_STATUS.get(self.status, ('未知', 'secondary'))[1]

    @property
    def is_terminal(self):
        """是否已是终态（不能再流转）。"""
        return not self.STATUS_FLOW.get(self.status)

    def can_transition_to(self, new_status):
        """校验状态流转是否合法 —— 非法流转必须在服务层拦住。"""
        return new_status in self.STATUS_FLOW.get(self.status, ())

    @property
    def growth_days_actual(self):
        """实际生育期天数（播种到收获）。"""
        end = self.harvest_date or self.mature_date
        return (end - self.sow_date).days if end else None

    def __repr__(self):
        return f'<Planting {self.batch_no}>'


class FarmingLog(db.Model):
    """
    农事作业记录：谁、何时、在哪块地、干了什么、花了多少人工和机械。

    和 InputCost 的分工：
      FarmingLog 回答"什么时间做了什么作业"（过程维度，含人工/机械费）
      InputCost  回答"买了什么、用了多少、多少钱"（物资成本维度）
    合成一张表会导致大量 NULL（一次灌溉有工时但没物资单价），
    是典型的设计异味。
    """
    __tablename__ = 'farming_log'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    planting_id = db.Column(INTEGER(unsigned=True),
                            ForeignKey('planting.id', ondelete='CASCADE'), nullable=False)
    op_type = db.Column(TINYINT(unsigned=True), nullable=False)
    op_date = db.Column(Date, nullable=False)
    description = db.Column(String(255), nullable=False, default='')
    labor_hours = db.Column(DECIMAL(8, 2), nullable=False, default=0)
    machine_hours = db.Column(DECIMAL(8, 2), nullable=False, default=0)
    labor_cost = db.Column(DECIMAL(12, 2), nullable=False, default=0)
    machine_cost = db.Column(DECIMAL(12, 2), nullable=False, default=0)
    operator_id = db.Column(INTEGER(unsigned=True), ForeignKey('user.id'))
    created_at = db.Column(DateTime, default=datetime.now)

    planting = relationship('Planting', back_populates='farming_logs')
    operator = relationship('User')

    @property
    def op_name(self):
        from config import OP_TYPES
        return OP_TYPES.get(self.op_type, '其他')

    @property
    def total_cost(self):
        return (self.labor_cost or 0) + (self.machine_cost or 0)

    def __repr__(self):
        return f'<FarmingLog {self.op_type}@{self.op_date}>'


class InputCost(db.Model):
    """农资投入明细（物资成本）"""
    __tablename__ = 'input_cost'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    planting_id = db.Column(INTEGER(unsigned=True),
                            ForeignKey('planting.id', ondelete='CASCADE'), nullable=False)
    input_type = db.Column(TINYINT(unsigned=True), nullable=False)
    item_name = db.Column(String(100), nullable=False)
    quantity = db.Column(DECIMAL(12, 3), nullable=False, default=0)
    unit = db.Column(String(20), nullable=False, default='')
    unit_price = db.Column(DECIMAL(12, 4), nullable=False, default=0)
    # ⚠️ amount 是数据库的 STORED 生成列（quantity × unit_price）。
    #    server_default=FetchedValue() 告诉 SQLAlchemy：
    #    "这个值由数据库算，不要写进 INSERT，但插入后要读回来"。
    #    如果不加这个标注，ORM 会尝试插入 NULL，MySQL 会直接报错：
    #    "The value specified for generated column 'amount' ... is not allowed"
    amount = db.Column(DECIMAL(14, 2), nullable=False,
                       server_default=FetchedValue())
    record_date = db.Column(Date, nullable=False)
    remark = db.Column(String(255), nullable=False, default='')
    created_at = db.Column(DateTime, default=datetime.now)

    planting = relationship('Planting', back_populates='input_costs')

    @property
    def type_name(self):
        from config import INPUT_TYPES
        return INPUT_TYPES.get(self.input_type, '其他')

    def __repr__(self):
        return f'<InputCost {self.item_name}>'
