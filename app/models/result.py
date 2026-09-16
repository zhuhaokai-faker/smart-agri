# -*- coding: utf-8 -*-
"""
结果数据模型：产量记录、逐日气象、审计日志。
"""

from datetime import datetime

from sqlalchemy import DECIMAL, Date, DateTime, ForeignKey, JSON, String, text
from sqlalchemy.dialects.mysql import DATETIME, INTEGER, TINYINT
from sqlalchemy.orm import relationship
from sqlalchemy import FetchedValue

from ..extensions import db


class WeatherDaily(db.Model):
    """
    逐日气象数据（按气象区）。

    【为什么气象独立成表，不挂在 planting 上】
    气象是**按地区、按日**的公共维度：同一气象站覆盖范围内的所有地块
    共享同一套气温降水数据。若把 temp_max/temp_min 塞进 planting 或
    farming_log，每个地块每天都会存一份完全相同的值 ——
    几十个地块 × 1095 天 = 数万行纯冗余。
    更严重的是无法支持"同一地区不同地块在同一气象条件下的产量对比"，
    因为数据被复制成了互不相干的副本。
    """
    __tablename__ = 'weather_daily'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    region_code = db.Column(String(20), nullable=False)
    obs_date = db.Column(Date, nullable=False)
    # obs_month 是 STORED 生成列。用 STORED 而非 VIRTUAL 是因为它要参与
    # 月度聚合的索引扫描，物化后才能进覆盖索引。
    obs_month = db.Column(Date, nullable=False, server_default=FetchedValue())
    temp_max = db.Column(DECIMAL(5, 2), nullable=False)
    temp_min = db.Column(DECIMAL(5, 2), nullable=False)
    temp_avg = db.Column(DECIMAL(5, 2), nullable=False)
    precipitation = db.Column(DECIMAL(7, 2), nullable=False, default=0)
    sunshine_hours = db.Column(DECIMAL(5, 2), nullable=False, default=0)
    humidity = db.Column(DECIMAL(5, 2), nullable=False, default=0)
    solar_radiation = db.Column(DECIMAL(8, 2), nullable=False, default=0)
    created_at = db.Column(DateTime, default=datetime.now)

    def gdd(self, base_temp):
        """
        当日有效积温 = max(0, (日最高温 + 日最低温)/2 − 生物学零度)

        GREATEST(0, ...) 不能省：低于生物学零度的日子不但不积累有效积温，
        而且不能"倒扣" —— 这是有效积温定义的核心。
        （Python 侧的实现与 SQL 分析 1 保持完全一致，两处可互相验证。）
        """
        return max(0.0, (float(self.temp_max) + float(self.temp_min)) / 2.0
                   - float(base_temp))

    def __repr__(self):
        return f'<Weather {self.region_code} {self.obs_date}>'


class YieldRecord(db.Model):
    """
    产量记录 —— 分析层的核心事实表。

    三个 STORED 生成列（yield_per_mu / output_value / harvest_month）
    由数据库计算，保证任何写入路径（应用、运维改库、批量导入）都一致。
    """
    __tablename__ = 'yield_record'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    planting_id = db.Column(INTEGER(unsigned=True),
                            ForeignKey('planting.id', ondelete='CASCADE'),
                            unique=True, nullable=False)
    # plot_id / crop_id 是**刻意的分析型冗余**：它们能从 planting_id 推导，
    # 但产量分析（按地块排行、按作物分组 TopN、地图着色）几乎每次都要 JOIN
    # planting 再 JOIN crop/plot，冗余后单表即可完成绝大多数分析。
    # 冗余安全的依据：planting_id 一旦确定，其 plot_id/crop_id 永不变更。
    plot_id = db.Column(INTEGER(unsigned=True), ForeignKey('plot.id'), nullable=False)
    crop_id = db.Column(INTEGER(unsigned=True), ForeignKey('crop.id'), nullable=False)
    harvest_date = db.Column(Date, nullable=False)
    harvest_month = db.Column(Date, nullable=False, server_default=FetchedValue())
    harvest_area_mu = db.Column(DECIMAL(10, 2), nullable=False, default=0)
    yield_kg = db.Column(DECIMAL(14, 2), nullable=False, default=0)
    yield_per_mu = db.Column(DECIMAL(10, 2), nullable=False, server_default=FetchedValue())
    grade = db.Column(TINYINT(unsigned=True), nullable=False, default=1)
    unit_price = db.Column(DECIMAL(10, 4), nullable=False, default=0)
    output_value = db.Column(DECIMAL(16, 2), nullable=False, server_default=FetchedValue())
    moisture_content = db.Column(DECIMAL(5, 2), nullable=False, default=0)
    quality_note = db.Column(String(255), nullable=False, default='')
    recorder_id = db.Column(INTEGER(unsigned=True), ForeignKey('user.id'))
    created_at = db.Column(DateTime, default=datetime.now)
    updated_at = db.Column(DateTime, default=datetime.now, onupdate=datetime.now)

    planting = relationship('Planting', back_populates='yield_record')
    plot = relationship('Plot')
    crop = relationship('Crop')
    recorder = relationship('User')

    GRADE_NAMES = {1: '一级', 2: '二级', 3: '三级'}

    @property
    def grade_name(self):
        return self.GRADE_NAMES.get(self.grade, '未知')

    def __repr__(self):
        return f'<YieldRecord {self.harvest_date} {self.yield_kg}kg>'


class AuditLog(db.Model):
    """
    操作审计日志（只增不改）。

    detail 用 JSON 存变更前后的快照。5.7 能存能取但不能 JSON_TABLE 展开，
    所以设计原则是：**JSON 只当不可查询的附件**，需要检索的字段必须抽成
    独立列（changed_fields 就是用生成列从 JSON 里抽出来的）。
    """
    __tablename__ = 'audit_log'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    user_id = db.Column(INTEGER(unsigned=True), ForeignKey('user.id', ondelete='SET NULL'))
    action = db.Column(String(32), nullable=False)
    target_table = db.Column(String(64), nullable=False)
    target_id = db.Column(INTEGER(unsigned=True))
    detail = db.Column(JSON)
    changed_fields = db.Column(String(255), server_default=FetchedValue())
    ip = db.Column(String(45), nullable=False, default='')
    # DATETIME(3) 毫秒精度：审计日志写入频率高，同一秒可能几十条，
    # 秒级精度无法确定事件先后顺序。
    # ⚠️ fsp（小数秒精度）是 **MySQL 方言专属参数**，通用 DateTime 不接受，
    #    必须用 sqlalchemy.dialects.mysql.DATETIME。
    created_at = db.Column(DATETIME(fsp=3), nullable=False,
                           server_default=text('CURRENT_TIMESTAMP(3)'))

    user = relationship('User')

    ACTION_NAMES = {
        'CREATE': '新增', 'UPDATE': '修改', 'DELETE': '删除',
        'LOGIN': '登录', 'LOGOUT': '登出', 'EXPORT': '导出',
        'STATUS_CHANGE': '状态流转',
    }

    @property
    def action_name(self):
        return self.ACTION_NAMES.get(self.action, self.action)

    def __repr__(self):
        return f'<AuditLog {self.action} {self.target_table}#{self.target_id}>'
