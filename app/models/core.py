# -*- coding: utf-8 -*-
"""
基础维度表模型：用户、农场、地块、作物。

【关于 ORM 模型的写法约定】
  1. 表名显式写 `__tablename__`，不依赖 SQLAlchemy 的自动复数化 ——
     单复数规则在不同版本/配置下会变，显式声明才可靠。
  2. 生成列（yield_per_mu / amount 等）用 `server_default=FetchedValue()`：
     告诉 SQLAlchemy "这个值由数据库计算，你不要往 INSERT 里写"，
     同时在 INSERT 后自动取回真实值。若不这样标注，ORM 会尝试写 NULL，
     而 MySQL 会因"不能给生成列赋值"直接报错。
  3. 关系（relationship）默认用惰性加载（lazy='select'）。
     ⚠️ 惰性加载在循环里访问就是经典的 **N+1 查询问题** ——
        列表页渲染 50 行，每行访问一次关联对象就是 51 次查询。
        本项目在列表页一律用 `joinedload()` 预加载，见 blueprints 里的用法。
"""

from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import DECIMAL, DateTime, ForeignKey, String
# ⚠️ TINYINT / INTEGER(unsigned=True) 这些是 **MySQL 方言特有的类型**，
#    SQLAlchemy 顶层没有导出。直接从 sqlalchemy 导入 TINYINT 会 ImportError。
#    必须从 sqlalchemy.dialects.mysql 导。这是写 MySQL 专属项目时第一个会踩的坑。
from sqlalchemy.dialects.mysql import INTEGER, SMALLINT, TINYINT
from sqlalchemy.orm import relationship
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db, login_manager


class User(db.Model, UserMixin):
    """后台账号。角色：1=管理员 2=农艺师 3=只读"""
    __tablename__ = 'user'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    username = db.Column(String(50), unique=True, nullable=False)
    password_hash = db.Column(String(255), nullable=False)
    real_name = db.Column(String(50), nullable=False, default='')
    email = db.Column(String(100), nullable=False, default='')
    phone = db.Column(String(20), nullable=False, default='')
    role = db.Column(TINYINT(unsigned=True), nullable=False, default=3)
    status = db.Column(TINYINT(unsigned=True), nullable=False, default=1)
    last_login_at = db.Column(DateTime)
    created_at = db.Column(DateTime, default=datetime.now)
    updated_at = db.Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 密码绝不明文存储。Werkzeug 的 generate_password_hash 默认用 scrypt，
    # 每次调用生成不同的 salt，所以同一个密码两次哈希结果不同 —— 这是正确的。
    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def role_name(self):
        from config import ROLE_NAMES
        return ROLE_NAMES.get(self.role, '未知')

    @property
    def is_active(self):
        """Flask-Login 会读这个属性；返回 False 则拒绝登录。"""
        return self.status == 1

    @property
    def is_admin(self):
        return self.role == 1

    @property
    def can_edit(self):
        """农艺师及以上可编辑；只读角色只能看。"""
        return self.role in (1, 2)

    def __repr__(self):
        return f'<User {self.username}>'


@login_manager.user_loader
def load_user(user_id):
    """Flask-Login 每次请求用它把 session 里的 user_id 还原成 User 对象。"""
    return db.session.get(User, int(user_id))


class Farm(db.Model):
    """农场 / 合作社"""
    __tablename__ = 'farm'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    farm_no = db.Column(String(32), unique=True, nullable=False)
    name = db.Column(String(100), nullable=False)
    province = db.Column(String(20), nullable=False, default='')
    city = db.Column(String(20), nullable=False, default='')
    county = db.Column(String(20), nullable=False, default='')
    region_code = db.Column(String(20), nullable=False)
    longitude = db.Column(DECIMAL(10, 6), nullable=False, default=0)
    latitude = db.Column(DECIMAL(10, 6), nullable=False, default=0)
    total_area_mu = db.Column(DECIMAL(12, 2), nullable=False, default=0)
    owner_name = db.Column(String(50), nullable=False, default='')
    contact_phone = db.Column(String(20), nullable=False, default='')
    status = db.Column(TINYINT(unsigned=True), nullable=False, default=1)
    created_at = db.Column(DateTime, default=datetime.now)
    updated_at = db.Column(DateTime, default=datetime.now, onupdate=datetime.now)

    plots = relationship('Plot', back_populates='farm', lazy='select')

    @property
    def region_name(self):
        return f'{self.province}{self.city}{self.county}'

    def __repr__(self):
        return f'<Farm {self.name}>'


class Plot(db.Model):
    """地块 —— 系统的核心实体，所有种植批次都挂在它上面"""
    __tablename__ = 'plot'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    plot_no = db.Column(String(32), unique=True, nullable=False)
    name = db.Column(String(100), nullable=False)
    farm_id = db.Column(INTEGER(unsigned=True), ForeignKey('farm.id'), nullable=False)
    area_mu = db.Column(DECIMAL(10, 2), nullable=False, default=0)
    soil_type = db.Column(TINYINT(unsigned=True), nullable=False, default=1)
    irrigation_type = db.Column(TINYINT(unsigned=True), nullable=False, default=1)
    fertility_level = db.Column(TINYINT(unsigned=True), nullable=False, default=2)
    longitude = db.Column(DECIMAL(10, 6), nullable=False, default=0)
    latitude = db.Column(DECIMAL(10, 6), nullable=False, default=0)
    altitude_m = db.Column(SMALLINT(unsigned=True), nullable=False, default=0)
    status = db.Column(TINYINT(unsigned=True), nullable=False, default=1)
    remark = db.Column(String(255), nullable=False, default='')
    created_at = db.Column(DateTime, default=datetime.now)
    updated_at = db.Column(DateTime, default=datetime.now, onupdate=datetime.now)

    farm = relationship('Farm', back_populates='plots')
    plantings = relationship('Planting', back_populates='plot', lazy='select')

    @property
    def soil_name(self):
        from config import SOIL_TYPES
        return SOIL_TYPES.get(self.soil_type, '未知')

    @property
    def irrigation_name(self):
        from config import IRRIGATION_TYPES
        return IRRIGATION_TYPES.get(self.irrigation_type, '未知')

    @property
    def fertility_name(self):
        from config import FERTILITY_LEVELS
        return FERTILITY_LEVELS.get(self.fertility_level, '未知')

    def __repr__(self):
        return f'<Plot {self.plot_no}>'


class Crop(db.Model):
    """
    作物品种。

    这张表存着 **base_temp（生物学零度）** 和 **gdd_maturity（所需有效积温）**，
    它们让"是否达到成熟"这个判断可以完全在 SQL 里完成 ——
    这是本项目与普通 CRUD 项目的分水岭：把领域知识沉淀进数据模型，
    而不是硬编码在业务代码里。
    """
    __tablename__ = 'crop'

    id = db.Column(INTEGER(unsigned=True), primary_key=True)
    crop_code = db.Column(String(32), unique=True, nullable=False)
    name = db.Column(String(50), nullable=False)
    variety = db.Column(String(50), nullable=False, default='')
    category = db.Column(TINYINT(unsigned=True), nullable=False, default=1)
    base_temp = db.Column(DECIMAL(5, 2), nullable=False)
    gdd_maturity = db.Column(DECIMAL(8, 2), nullable=False)
    growth_days = db.Column(SMALLINT(unsigned=True), nullable=False, default=0)
    status = db.Column(TINYINT(unsigned=True), nullable=False, default=1)
    created_at = db.Column(DateTime, default=datetime.now)
    updated_at = db.Column(DateTime, default=datetime.now, onupdate=datetime.now)

    @property
    def category_name(self):
        from config import CROP_CATEGORIES
        return CROP_CATEGORIES.get(self.category, '未知')

    @property
    def full_name(self):
        return f'{self.name}·{self.variety}' if self.variety else self.name

    def __repr__(self):
        return f'<Crop {self.name}>'
