FROM node:24-slim AS dashboard-build

WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/index.html dashboard/tsconfig.json ./
COPY dashboard/src ./src
RUN npm run build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/axon

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations

# Build-context modes vary by host; the runtime user must traverse source dirs.
RUN chmod -R a+rX,go-w app migrations \
    && chmod a+r,go-w pyproject.toml alembic.ini \
    && python -m pip install --no-cache-dir . \
    && useradd --system --no-create-home --no-log-init axon

COPY --from=dashboard-build /build/dist ./dashboard/dist

USER axon

# Check imports from WORKDIR, as Alembic does, before publishing an image.
RUN python -c "from importlib.util import find_spec; modules = ('app.persistence.database', 'app.planner.worker', 'app.main'); missing = [name for name in modules if find_spec(name) is None]; assert not missing, missing"

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
