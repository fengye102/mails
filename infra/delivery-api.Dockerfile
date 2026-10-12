FROM python:3.12-slim
WORKDIR /app
COPY apps/delivery_api/pyproject.toml apps/delivery_api/alembic.ini ./
RUN pip install --no-cache-dir uv==0.11.29 && uv sync --no-dev --no-install-project
COPY apps/delivery_api/src ./src
COPY apps/delivery_api/alembic ./alembic
RUN uv sync --no-dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src"
RUN useradd --uid 10003 --create-home delivery
USER delivery
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn delivery_api.main:app --host 0.0.0.0 --port 8200 --workers 1 --no-proxy-headers --no-access-log"]
