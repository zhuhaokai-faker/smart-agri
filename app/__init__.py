# -*- coding: utf-8 -*-
"""
Flask 应用工厂。

【为什么用 create_app() 工厂函数，而不是模块级 app = Flask(__name__)】
  1. **避免循环导入**：蓝图和扩展都要 import app，模块级创建会成环。
  2. **可测试**：测试里可以创建 N 个 app 实例，各绑不同配置（临时库、
     关闭 CSRF、改 SECRET_KEY），互不干扰。模块级单例做不到这点。
  3. **可多实例**：同一个进程里跑多套配置（比如多租户）也支持。

  这是 Flask 官方推荐的工程化写法，也是区分"简单 Flask demo"和
  "可维护 Flask 项目"的标志之一。
"""

import logging
import sys

from flask import Flask, jsonify, render_template, request
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException

from config import Config
from .extensions import csrf, db, login_manager


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # ------------------------------------------------------------ 扩展
    db.init_app(app)
    login_manager.init_app(app)
    # 全局 CSRF 校验：所有 POST/PUT/PATCH/DELETE 都必须带合法 token。
    # 表单里靠 {{ csrf_token() }} 提供，路由代码不需要改动。
    csrf.init_app(app)

    # ------------------------------------------------------------ 模型
    # 必须在 init_app 之后导入，否则 db.Model 还没有绑定，会报
    # "No application found"。这个导入顺序是 Flask-SQLAlchemy 的硬性要求。
    from . import models  # noqa: F401

    # ------------------------------------------------------------ 蓝图
    from .blueprints import (admin, analytics, audit, auth, base_data,
                             dashboard, map as map_bp, production, weather)
    app.register_blueprint(auth.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(base_data.bp)
    app.register_blueprint(production.bp)
    app.register_blueprint(weather.bp)
    app.register_blueprint(analytics.bp)
    app.register_blueprint(map_bp.bp)
    app.register_blueprint(audit.bp)

    _register_jinja(app)
    _register_errors(app)
    _register_cli(app)
    _setup_logging(app)

    return app


def _register_jinja(app):
    """注册模板过滤器与全局变量。"""

    @app.template_filter('wan')
    def wan(v):
        """元 → 万元，保留 1 位。金额在页面上用"万"更易读。"""
        try:
            return f'{float(v) / 10000:,.1f}'
        except (TypeError, ValueError):
            return '-'

    @app.template_filter('num')
    def num(v, digits=0):
        """千分位数字。None 显示为 '-' 而不是 'None'。"""
        if v is None:
            return '-'
        try:
            return f'{float(v):,.{digits}f}'
        except (TypeError, ValueError):
            return v

    @app.template_filter('pct')
    def pct(v, digits=1):
        if v is None:
            return '-'
        try:
            return f'{float(v):.{digits}f}%'
        except (TypeError, ValueError):
            return v

    @app.template_filter('r1')
    def r1(v):
        """保留 1 位小数。"""
        if v is None:
            return '-'
        try:
            return f'{float(v):,.1f}'
        except (TypeError, ValueError):
            return v

    @app.template_filter('signed')
    def signed(v, digits=1):
        """带正负号的百分比（同比环比用，正数显式带 + 号）。"""
        if v is None:
            return '-'
        try:
            f = float(v)
            return f'{f:+.{digits}f}%'
        except (TypeError, ValueError):
            return v

    @app.context_processor
    def inject_globals():
        """所有模板都能用的全局变量。"""
        import config as cfg
        return {
            'PLANTING_STATUS': cfg.PLANTING_STATUS,
            'OP_TYPES': cfg.OP_TYPES,
            'INPUT_TYPES': cfg.INPUT_TYPES,
            'CROP_CATEGORIES': cfg.CROP_CATEGORIES,
            'FERTILITY_LEVELS': cfg.FERTILITY_LEVELS,
            'app_name': '智慧农业种植管理与产量分析平台',
        }


def _register_errors(app):
    """统一错误处理。

    JSON 请求返回 JSON 错误、页面请求返回 HTML 错误页 ——
    对 API 调用方返回一个 HTML 报错页是没用的，它解析不了。
    """

    def _wants_json():
        return (request.path.startswith('/api/')
                or request.accept_mimetypes.best == 'application/json')

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        """
        CSRF 校验失败的专用提示。

        CSRFError 是 BadRequest(400) 的子类 —— 不单独注册这个处理器的话，
        它会落到下面的 HTTPException 分支，用户看到的是一句英文的
        "The CSRF token is missing."。对着一句英文报错，没人知道该做什么。

        最常见的原因**不是攻击，而是页面开太久**：令牌默认 1 小时过期，
        表单还开着但令牌已失效。所以提示要直接给出可操作的下一步。
        """
        app.logger.warning('CSRF 校验失败：%s（%s）', e.description, request.path)
        msg = ('表单已过期或来源校验失败。这通常是因为页面打开太久'
               '（安全令牌 1 小时过期）—— 刷新页面后重新提交即可。')
        if _wants_json():
            return jsonify({'error': 'CSRF Failed', 'code': 400,
                            'message': msg}), 400
        return render_template('error.html', code=400, name='请求被拒绝',
                               message=msg), 400

    @app.errorhandler(HTTPException)
    def handle_http_error(e):
        if _wants_json():
            return jsonify({'error': e.name, 'code': e.code,
                            'message': e.description}), e.code
        return render_template('error.html', code=e.code, name=e.name,
                               message=e.description), e.code

    @app.errorhandler(Exception)
    def handle_unexpected(e):
        # 未预期的异常要记完整堆栈（排查靠它），但**不能把堆栈返回给用户**
        # —— 堆栈里可能含文件路径、SQL、配置等敏感信息。
        app.logger.exception('未处理的异常: %s', e)
        if _wants_json():
            return jsonify({'error': 'Internal Server Error', 'code': 500}), 500
        return render_template('error.html', code=500, name='服务器内部错误',
                               message='系统出现异常，请查看控制台日志'), 500


def _register_cli(app):
    """自定义 CLI 命令：flask init-db / flask stats"""

    @app.cli.command('stats')
    def stats():
        """打印各表行数，快速确认数据是否就绪。"""
        from .extensions import raw_query
        rows = raw_query("""
            SELECT 'farm' t, COUNT(*) n FROM farm
            UNION ALL SELECT 'plot', COUNT(*) FROM plot
            UNION ALL SELECT 'crop', COUNT(*) FROM crop
            UNION ALL SELECT 'planting', COUNT(*) FROM planting
            UNION ALL SELECT 'farming_log', COUNT(*) FROM farming_log
            UNION ALL SELECT 'input_cost', COUNT(*) FROM input_cost
            UNION ALL SELECT 'weather_daily', COUNT(*) FROM weather_daily
            UNION ALL SELECT 'yield_record', COUNT(*) FROM yield_record
        """)
        for r in rows:
            print(f"  {r['t']:<16} {r['n']:>8,}")


def _setup_logging(app):
    """把日志同时输出到控制台和文件，便于排查。"""
    if app.debug:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'))
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
