FROM python:3.12-slim
WORKDIR /app
COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN pip install --no-cache-dir uv==0.11.29 && uv sync --frozen --no-dev --no-install-project
COPY apps/api/src ./src
COPY apps/api/alembic ./alembic
COPY apps/api/alembic.ini ./
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home mails
ENV PATH="/app/.venv/bin:$PATH"
USER mails
