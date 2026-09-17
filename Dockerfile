# =============================================================================
#  智慧农业种植管理与产量分析平台 —— Linux 容器（nginx + gunicorn）
# =============================================================================
#  构建：  docker build -t smart-agri-analytics:latest .
#  运行：  docker run --rm -p 8080:80 --env-file docker/container.env \
#              smart-agri-analytics:latest
#
#  国内网络构建请带上软件源参数（原因见下面 DEBIAN_MIRROR 那段）：
#          docker build \
#            --build-arg DEBIAN_MIRROR=mirrors.tuna.tsinghua.edu.cn \
#            --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
#            -t smart-agri-analytics:latest .
#
#  【和 Windows 那一套的关系】
#    Windows 上没有 gunicorn（它依赖 os.fork），所以 deploy/ 下是
#    serve_cluster.py 自己编排 N 个 waitress + nginx，还得绕开 nginx 的
#    非 ASCII 路径问题（README Bug 13）。容器里这些全都用不上：
#      · 多进程由 gunicorn 自己管     → 不需要 serve_cluster.py
#      · 路径本来就在 /app，纯 ASCII  → 不需要 junction 绕法
#    两边共用同一个 WSGI 入口 wsgi.py —— 当初把它单独拆出来就是为了这个。
#
#  【为什么一个容器里塞两个进程】
#    这是本文件最"不那么教科书"的地方：常见做法是 app 和 nginx 分成两个
#    容器。这里合成一个是刻意的取舍 —— 镜像能整包带走（docker save 出一个
#    tar 就能在别的机器上跑），代价是容器内部要自己管两个进程的生死。
#    那边的工作在 docker/entrypoint.sh。
#
#  【为什么不切非 root 用户】
#    nginx 的 master 需要绑定 80 端口（<1024）并写 /var/run，官方 nginx 镜像
#    同样是 root 跑 master、worker 降权到 www-data。要彻底非 root 得把监听
#    端口改成 8080，属于另一套取舍，这里没做。
# =============================================================================

FROM python:3.12-slim-bookworm

# ------------------------------------------------------------------ 软件源
# 默认用官方源，**没有写死成国内镜像** —— 写死了这份 Dockerfile 在别处就跑不通。
# 但国内直连 Debian 官方源经常 503（实测拉了半屏包失败，带宽 54 kB/s），
# 所以国内构建时传两个参数即可：
#
#     docker build \
#       --build-arg DEBIAN_MIRROR=mirrors.tuna.tsinghua.edu.cn \
#       --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
#       -t smart-agri-analytics:latest .
#
# 注意 DEBIAN_MIRROR 会被同时替换到 debian 和 debian-security 两行上，
# 所以要选一个同时镜像这两者的源（清华、中科大、阿里都可以）。
ARG DEBIAN_MIRROR=deb.debian.org
ARG PIP_INDEX=https://pypi.org/simple

# ------------------------------------------------------------------ 系统依赖
# nginx  : 对外入口，静态文件直接读磁盘，动态请求转给 gunicorn（见 docker/nginx.conf）
# tini   : 当 PID 1。容器里有两个进程，没有 init 的话 docker stop 发的 SIGTERM
#          只会送到 PID 1，另一个进程收不到信号，要干等到超时才被 SIGKILL ——
#          表现就是"每次停止都很慢"，而且 gunicorn 来不及处理完在途请求。
#          tini 还负责回收孤儿进程，否则僵尸会堆在 PID 1 底下。
# tzdata : 见下面的 TZ。python:slim 不带这个包，缺了它 TZ 会被静默忽略，
#          时间悄悄退回 UTC —— 是最难发现的那种失败。
# curl   : HEALTHCHECK 用
RUN sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        nginx \
        tini \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 容器默认走 UTC。这个应用里 date.today() 参与"收获日期"和种植批次状态的
# 判定（app/services/planting_service.py），差 8 小时会让北京时间 0 点到 8 点
# 之间算出来的日期是前一天 —— 偏偏演示常在这种时间点做。
ENV TZ=Asia/Shanghai

# 日志要实时进 docker logs。Python 的 stdout 在非 TTY（管道）下是块缓冲的，
# 不设这一项，日志会攒一批才吐出来。
ENV PYTHONUNBUFFERED=1

# ⚠️ 这一行是补出来的 —— 第一版漏了它，容器起来就报
#        /app/docker/entrypoint.sh: line 124: gunicorn: command not found
#    原因：venv 建在 /app/.venv，但**没有任何地方 activate 过它**。
#    在开发机上人敲 `source .venv/bin/activate`，容器里没人敲这一步：
#    ENTRYPOINT 直接执行脚本，PATH 上只有系统目录，自然找不到 gunicorn，
#    也找不到 venv 里的 python。
#    构建期看不出来（Dockerfile 里用的都是 /app/.venv/bin/xxx 绝对路径），
#    只有真跑起来才暴露 —— 所以这里显式把 venv 放进 PATH，
#    后面（entrypoint、exec 进去排查）写的都是裸命令名，不用再记绝对路径。
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# ------------------------------------------------------------------ 依赖
# 只先拷 requirements.txt：让"装依赖"单独成层。改代码不会触发重新 pip install，
# 只有改依赖才触发。
COPY requirements.txt /app/requirements.txt

# ⚠️ 镜像里为什么要建 venv（容器本身就是隔离，按说不需要）
#   因为 env_check.py 会把"没有跑在本项目的 .venv 里"判成环境错误并
#   sys.exit(1)，而本项目每个入口都会先跑它 —— 容器里不满足它就根本起不来。
#   那条检查的本意是在开发机上挡住"用错解释器"（系统 Python、别的项目的 venv）。
#
#   两种改法，这里选了后一种：
#     · 给检查开后门（识别到容器就跳过）—— 会同时放松开发机上的保护；
#     · 满足它 —— 解释器就放在它预期的位置 /app/.venv。
#   代价是镜像大几十 MB，换来的是两边跑**同一份代码，没有分支**。
#
# gunicorn 不写进 requirements.txt：那份清单 Windows 开发环境也用它，而
# gunicorn 在 Windows 上根本起不来（依赖 os.fork）。所以它只装在这里。
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip -i "${PIP_INDEX}" \
    && /app/.venv/bin/pip install --no-cache-dir -r /app/requirements.txt -i "${PIP_INDEX}" \
    && /app/.venv/bin/pip install --no-cache-dir 'gunicorn==23.0.0' -i "${PIP_INDEX}"

# ------------------------------------------------------------------ 源码
COPY . /app

# ⚠️ 上面那行有个陷阱：.dockerignore 要是漏掉了 .venv，宿主机的 **Windows**
#    venv（Scripts/ 而不是 bin/）会被合并进来，把刚建好的 Linux venv 覆盖掉，
#    之后报错会指向一堆莫名其妙的地方。
#    所以这里立刻验一次 —— 宁可构建失败，也不要构建出一个坏镜像。
RUN /app/.venv/bin/python -c "\
import sys, flask, pymysql, cryptography; \
assert sys.platform == 'linux', 'venv 被宿主机的 Windows venv 覆盖了: ' + sys.platform"

# ⚠️ 必须显式 chmod：构建上下文在 Windows 上，那里没有可执行位这个概念，
#    COPY 进来的 entrypoint.sh 不一定是可执行的 —— 而 ENTRYPOINT 直接执行它，
#    报错会是看着莫名其妙的 "permission denied"。
RUN chmod +x /app/docker/entrypoint.sh

# ------------------------------------------------------------------ nginx
# 用 sites-enabled 而不是直接覆盖 conf.d：Debian 的 nginx.conf 两个目录都 include，
# 删掉默认站点后放我们自己的。
#
# 两个 /dev/stdout 的软链接是 nginx 官方镜像的老办法：把 nginx 的日志接到容器
# 自己的 stdout/stderr。不接的话日志写进容器内的文件，docker logs 是空的 ——
# 等于没有日志。
#
# 末尾的 nginx -t 是构建期验一次配置：配置写错了在这里失败（镜像构建不出来），
# 比运行起来每个请求 502 要好得多。
RUN rm -f /etc/nginx/sites-enabled/default \
    && cp /app/docker/nginx.conf /etc/nginx/sites-available/smart-agri.conf \
    && ln -s /etc/nginx/sites-available/smart-agri.conf /etc/nginx/sites-enabled/smart-agri.conf \
    && ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log \
    && nginx -t

EXPOSE 80

# /login 是唯一不需要登录就能拿到 200 的页面，适合当探针。
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1/login > /dev/null || exit 1

# tini 当 PID 1；-- 之后才是我们自己的入口脚本。
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
