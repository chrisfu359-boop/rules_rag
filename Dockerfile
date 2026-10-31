# ===== rules_rag 应用镜像 =====
# 只装「核心档」依赖：语义向量/精排需要本地 BGE 模型（约 2GB，且路径是本机绝对路径），
# 镜像里装不起来也没意义 —— 检索链路会自动降级 BM25 + npz，功能完整可用。
#
# 构建：docker build -t rules-rag:latest .
# 运行：docker compose up -d         （完整栈：app + MySQL + Redis + Milvus）
#       docker run -p 8000:8000 rules-rag:latest   （SQLite 单容器，无外部依赖）
#
# 上线前请确认三件事：
#   1. CORS_ORIGINS 改成真实域名，否则前端拿不到响应且报错在浏览器侧
#   2. API_KEY 设上（»/api/ask» 的 X-API-Key，留空表示不校验）
#   3. JWT_SECRET 用外部注入，不要用容器里自动生成的那个（重建即失效）

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，利用层缓存：改代码不用重装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码与种子数据（体积垃圾由 .dockerignore 挡在外面）
COPY . .

# 默认 SQLite 后端：容器里没有 MySQL 也能起，功能不变
ENV DB_BACKEND=sqlite \
    VECTOR_BACKEND=npz \
    RERANK=off \
    CORS_ORIGINS=http://127.0.0.1:8000,http://localhost:8000

# 非 root 运行：容器逃逸的代价和"能写哪里"直接相关
RUN useradd -m -u 10001 appuser \
 && mkdir -p /app/logs \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 用 python 做探活而不是 curl：slim 镜像里没有 curl，装它只为健康检查不值得
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
