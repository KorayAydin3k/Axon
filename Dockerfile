FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/axon

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations

RUN python -m pip install --no-cache-dir . \
    && useradd --system --no-create-home --no-log-init axon

USER axon
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
