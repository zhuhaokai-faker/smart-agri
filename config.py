# -*- coding: utf-8 -*-
"""
项目配置。

【为什么配置要集中在这里，而不是散落在各文件】
造数脚本（scripts/）、建表脚本、Flask 应用三者都需要连数据库。如果每处各写一份
连接参数，改密码时就要改三个地方，且很容易漏改导致"造数写进了 A 库、应用读的是 B 库"
这种极难排查的问题。集中一份配置是底线。

【为什么用 .env 而不是把密码写在代码里】
密码写进代码就会跟着 git 一起提交上去。.env 在 .gitignore 里，仓库里只留
.env.example 模板。这是最基本的凭据管理习惯 —— 面试时被问到"你的数据库密码
怎么管理的"能直接答上来。
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 加载 .env（若不存在则用下面的默认值，不影响开箱即用）
def load_dotenv(path: Path) -> None:
    """Load basic KEY=VALUE entries without requiring python-dotenv."""
    if not path.is_file():
        return

    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"\'')
        if key:
            os.environ.setdefault(key, value)


load_dotenv(BASE_DIR / '.env')


# =============================================================================
# 数据库连接（MySQL 8.0）
# =============================================================================
# 【charset 仍然要显式指定，但理由和 5.7 时代不同了】
#   MySQL 8.0 的服务端默认字符集已经是 utf8mb4（5.7 默认是 latin1），
#   所以"忘了指定就乱码"的坑在服务端这一侧已经消失。
#   但**客户端字符集仍然受操作系统影响** —— 本机 character_set_client 实测仍是 gbk，
#   所以在命令行执行 SQL 时依然要加 --default-character-set=utf8mb4。
#   应用侧显式指定 charset 是防御性写法：不依赖服务端配置，换环境也不会出问题。
#
# 【8.0 的连接层新变化】
#   认证插件默认为 caching_sha2_password（5.7 是 mysql_native_password），
#   这要求 PyMySQL 装 cryptography —— 见 requirements.txt 的说明。
DB_CONFIG = {
    'host':     os.getenv('DB_HOST', '127.0.0.1'),
    'port':     int(os.getenv('DB_PORT', '3306')),
    'user':     os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', 'change-me'),
    'charset':  'utf8mb4',
    'autocommit': False,
}

DB_NAME = os.getenv('DB_NAME', 'smart_agri')

# SQLAlchemy 连接串（Flask-SQLAlchemy 用）
SQLALCHEMY_DATABASE_URI = (
    f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_NAME}?charset=utf8mb4"
)


# =============================================================================
# Flask 应用配置
# =============================================================================
class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # SQLAlchemy 连接池。默认 pool_recycle=-1 表示连接永不过期，
    # 但 MySQL 默认 wait_timeout 是 8 小时（8.0 同样是 28800 秒），
    # 超过后连接被服务端单方面关闭，应用再拿这个"死连接"发请求就会报
    # (2006, 'MySQL server has gone away')。
    # 设 pool_recycle=3600 让连接每小时自动重建，避开这个经典坑。
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 10,
        'pool_recycle': 3600,
        'pool_pre_ping': True,   # 取连接前先 ping 一下，失效则自动重建
        'echo': False,
    }

    JSON_AS_ASCII = False        # 保证 JSON 接口返回的中文不被转义
    PER_PAGE = 15                # 列表页默认每页条数

    # 是否开放公开注册。
    # 【为什么做成开关而不是写死】
    #   开放注册是有代价的：任何人都能建号，只能靠限流兜着。
    #   真实项目上线后通常会关掉公开注册，改成邀请制或由管理员建号。
    #   做成开关，切换时不用改代码，改个环境变量就行。
    ALLOW_REGISTRATION = os.getenv('ALLOW_REGISTRATION', 'true').lower() == 'true'


# =============================================================================
# 业务常量（与数据库里的 TINYINT 编码一一对应）
# =============================================================================

# 用户角色
ROLE_ADMIN = 1
ROLE_AGRONOMIST = 2
ROLE_VIEWER = 3
ROLE_NAMES = {1: '管理员', 2: '农艺师', 3: '只读'}

# 种植批次状态机
# 10 待播种 → 20 生长中 → 30 成熟待收 → 40 已收获
#                    ↘ 50 已废弃（绝收/改种）
PLANTING_STATUS = {
    10: ('待播种', 'secondary'),
    20: ('生长中', 'info'),
    30: ('成熟待收', 'warning'),
    40: ('已收获', 'success'),
    50: ('已废弃', 'dark'),
}

# ⚠️ 全项目统一的"有效种植批次"口径。
#    所有分析查询必须用这一个定义，不允许各写各的 —— 否则同一个指标在不同
#    页面算出不同数字，是最典型的数据不一致事故。
#    定义：生长中 + 成熟待收 + 已收获（排除待播种和已废弃）
VALID_PLANTING_STATUS = (20, 30, 40)
# 已收获=40（有产量记录），仅在需要产量时使用
HARVESTED_STATUS = 40

# 农事作业类型
OP_TYPES = {
    10: '播种', 20: '施肥', 30: '灌溉', 40: '打药',
    50: '除草', 60: '收获', 70: '整地', 90: '其他',
}

# 农资投入类型
INPUT_TYPES = {
    10: '种子', 20: '化肥', 30: '农药', 40: '农膜',
    50: '水电', 60: '人工', 70: '机械', 80: '其他',
}

# 作物类别
CROP_CATEGORIES = {1: '粮食作物', 2: '蔬菜', 3: '水果', 4: '经济作物'}

SOIL_TYPES = {1: '壤土', 2: '黏土', 3: '砂土', 4: '砂壤土'}
IRRIGATION_TYPES = {1: '雨养', 2: '漫灌', 3: '喷灌', 4: '滴灌'}
FERTILITY_LEVELS = {1: '优', 2: '中', 3: '差'}
