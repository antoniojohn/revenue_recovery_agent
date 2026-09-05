FROM python:3.11-slim

# Keep Python from buffering stdout/stderr, so `docker compose logs`
# shows print() output immediately instead of batching it, and don't
# write .pyc files into a mounted volume.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so this layer is cached across code
# changes that don't touch requirements.txt.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# logs/, data/, and instance/ (SQLite settings DB) are created here so
# the image works standalone before any volume is mounted -
# docker-compose.yml mounts real volumes over these for persistence
# across container restarts and redeploys.
RUN mkdir -p logs data instance

EXPOSE 5000 5001

# No default CMD - each service in docker-compose.yml specifies its own
# command (webhook receiver, admin panel, reconcile loop, or a one-shot
# pipeline run), since this one image serves four different roles.
