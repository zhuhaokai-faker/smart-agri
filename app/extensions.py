# -*- coding: utf-8 -*-
"""
Flask 扩展实例。

【为什么扩展对象要单独一个文件，而不是在 __init__.py 里创建】
  这是 Flask 官方推荐的"应用工厂 + 扩展单例"模式。
  如果扩展在 __init__.py 里创建，models 想用 db 就得反向 import __init__，
  而 __init__ 又要 import models —— **循环导入**。
  把 db 放在一个不依赖任何业务模块的文件里，两边都 import 它，环就断了。

  另一个收益：可以在测试里创建多个 app 实例、绑定不同的配置，
  而扩展单例只初始化一次（用 init_app 延迟绑定）。
"""

from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import text  # pyright: ignore[reportMissingImports]

db = SQLAlchemy()
login_manager = LoginManager()

# CSRF 防护。只用了 Flask-WTF 的 CSRFProtect，没用它的表单类 ——
# 模板是手写的 Bootstrap 表单，改写成 WTForms 类工作量大且没有额外收益。
# CSRFProtect 是全局生效的：初始化一次，然后每个 <form> 里放一个
# {{ csrf_token() }} 就行，路由代码一行都不用改。
#
# 【为什么本地演示时没加，公开前必须加】
#   本地只有自己点，没有"第三方页面"这个攻击面。
#   一旦通过隧道暴露到公网，任何人在自己页面上放一个
#   <form action="https://你的域名/yields/1/delete" method="post">
#   就能借**已登录管理员**的会话删数据 —— 浏览器会自动带上 cookie，
#   服务端只看 cookie 分不清是不是本人点的。
csrf = CSRFProtect()

# 未登录访问受保护页面时的跳转终点
login_manager.login_view = 'auth.login'
login_manager.login_message = '请先登录'
login_manager.login_message_category = 'warning'


def raw_query(sql, params=None):
    """
    执行原生 SQL 并返回字典列表。

    【为什么分析查询要用原生 SQL，不用 ORM】
    这不是偷懒，是刻意的技术选型：
      · 有效积温累计求和需要用户变量（@gdd := @gdd + ...）
      · 皮尔逊相关系数需要手写公式（MySQL 没有 CORR()）
      · ABC 帕累托需要逐行累计、分组 TopN 需要模拟 ROW_NUMBER()
    这些**没有任何一个能用 ORM 表达**。ORM 擅长的是 CRUD 和关联加载，
    分析查询必须落到 SQL 层。

    面试里能主动说清"什么时候该用 ORM、什么时候必须写 SQL"，
    比只会其中一种要强得多。

    【关于 text() 和参数绑定】
    必须用 text() 包一层并走参数绑定（:param），不能自己拼字符串 ——
    拼接就是 SQL 注入。这里所有查询的变量都通过 params 传入。
    """
    result = db.session.execute(text(sql), params or {})
    cols = result.keys()
    return [dict(zip(cols, row)) for row in result.fetchall()]


def raw_scalar(sql, params=None):
    """执行原生 SQL 并返回第一行第一列（用于 COUNT 之类的标量查询）。"""
    result = db.session.execute(text(sql), params or {})
    row = result.fetchone()
    return row[0] if row else None
