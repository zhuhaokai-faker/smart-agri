# -*- coding: utf-8 -*-
"""
ORM 模型包。

【为什么在这个 __init__ 里全部 import 一遍】
  Flask-SQLAlchemy 需要在 db.create_all() / mapper 配置之前
  **所有模型类都已被导入并注册**。
  如果只在用到时才 import（比如 blueprints 里），
  那么单独跑一个"建表"脚本时就会漏掉部分表。
  在这里集中导入是让模型注册变得确定的最简单办法。

  同时它还给外部提供了统一的导入入口：
      from app.models import User, Plot, Planting
  比记住每个类在哪个文件里更好用。
"""

from .core import Crop, Farm, Plot, User, load_user
from .operation import FarmingLog, InputCost, Planting
from .result import AuditLog, WeatherDaily, YieldRecord

__all__ = [
    'User', 'Farm', 'Plot', 'Crop',
    'Planting', 'FarmingLog', 'InputCost',
    'YieldRecord', 'WeatherDaily', 'AuditLog',
    'load_user',
]
