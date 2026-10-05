FROM python:3.12.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai UV_PYTHON_DOWNLOADS=0
COPY app /app
COPY engine /engine
COPY bundle.json /bundle.json
WORKDIR /app
RUN uv sync --locked --no-dev && cd /engine && uv sync --locked --no-dev \
    && /app/.venv/bin/python -m quantlab.scout.cloud_setup \
    && mkdir -p /data/scout && chown 1000:1000 /data/scout
USER 1000:1000
CMD ["/app/.venv/bin/python", "-m", "quantlab.scout.cloud_web"]
