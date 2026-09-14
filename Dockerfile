# Production image for the LeadForge AI webhook service.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first: this layer stays cached while application code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code, Alembic migrations (alembic/) and alembic.ini.
# Migrations are applied on startup by the FastAPI lifespan (app.main.startup_runtime).
COPY . .

# Railway injects PORT; 8080 matches app.main.DEFAULT_PORT for local runs.
ENV PORT=8080
EXPOSE 8080

# `exec` keeps uvicorn as PID 1 so Railway can stop the container gracefully.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
