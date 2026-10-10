FROM python:3.12-slim
WORKDIR /app

# Build the isolated public API environment from its own manifest.
COPY apps/public_api/pyproject.toml ./
RUN pip install --no-cache-dir uv==0.11.29 \
    && uv sync --no-dev --no-install-project

COPY apps/public_api/src ./src
COPY apps/public_api/alembic ./alembic
COPY apps/public_api/alembic.ini ./
RUN uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
RUN useradd --uid 10002 --create-home publicmail
USER publicmail

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn public_mail_api.main:app --host 0.0.0.0 --port 8100 --workers 1 --no-proxy-headers --no-access-log"]