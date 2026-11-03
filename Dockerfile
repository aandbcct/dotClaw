# 前端只在构建阶段编译，部署镜像不携带 Node.js。
FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim-bookworm AS python-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN python -m venv /opt/venv \
    && useradd --create-home --uid 1000 dotclaw
COPY pyproject.toml README.md ./
COPY src/ ./src/
# 可编辑安装保留项目当前按源码位置解析配置的约定。
RUN /opt/venv/bin/python -m pip install --no-cache-dir -e '.[gui]'
COPY docker/ /opt/dotclaw/docker/
RUN mkdir -p /app/data /app/skills /app/.dotclaw/agentConfig /workspace \
    && chown -R dotclaw:dotclaw /app /workspace
USER dotclaw
EXPOSE 8765
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/opt/venv/bin/python", "/opt/dotclaw/docker/healthcheck.py"]

FROM python-base AS development
USER root
# 开发版需要 Vite；Linux 依赖留在独立卷，避免使用宿主机的 node_modules。
COPY --from=frontend-build /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-build /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=frontend-build --chown=dotclaw:dotclaw /build/frontend/ /app/frontend/
USER dotclaw
ENV DOTCLAW_DEV=1
EXPOSE 5173
CMD ["/opt/venv/bin/python", "/opt/dotclaw/docker/dev.py"]

FROM python-base AS deployment
COPY --from=frontend-build --chown=dotclaw:dotclaw /build/frontend/dist/ /app/frontend/dist/
CMD ["/opt/venv/bin/python", "-m", "uvicorn", "dotclaw.channel.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
