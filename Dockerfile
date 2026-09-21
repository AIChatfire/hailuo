# hailuo-service —— 图片生成异步出口
#
# ⚠️ `CMD` 里那对**括号不能省**：目标是**工厂**而不是模块级 `app` 对象。
#    写成 `app.main:app` 会得到 `Failed to find attribute 'app' in 'app.main'`，
#    而单测全绿也照样炸（它们都直接调 `create_app()`）。
#    `tests/test_wiring.py` 里有用例钉这条。

FROM python:3.12-slim AS base

# --- 版本与来源（发布工作流注入；label 也是 GHCR 包与仓库关联的依据）
ARG APP_VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="hailuo" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.source="https://github.com/AIChatfire/hailuo" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# --- 依赖单独一层：改代码不会让依赖重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- 应用代码
# ⚠️ 不要 `COPY docs`：`.dockerignore` 排除了 docs/，COPY 一个被 ignore 的路径会
#    **直接构建失败**（"excluded by .dockerignore"），而且这里也**从未真跑过构建** ——
#    CI 冒烟走的是宿主 gunicorn，不是镜像。docs/ 只有注释层面被引用（运行期不读）。
COPY app ./app
COPY gunicorn_conf.py ./
COPY scripts ./scripts

# --- 非 root 运行
# 任务库默认落在工作目录（SQLite）⇒ 目录必须对运行用户可写。
RUN useradd --create-home --uid 10001 hailuo \
    && mkdir -p /app/data \
    && chown -R hailuo:hailuo /app
USER hailuo

# --- 端口（默认 8300，与 app/config.py 的 Settings.port 一致）
EXPOSE 8300

# --- 存活探针：**零依赖、不触上游、不消耗额度**。
#     刻意用 /healthz 而不是 /readyz —— 后者会 ping 一次任务库，太贵。
#     /healthz 既不上报 span 也不留日志（见 app/observability.py 的 PROBE_PATHS）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8300/healthz', timeout=4).status==200 else 1)"

CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:create_app()"]
