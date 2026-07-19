FROM python:3.12.13-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app
ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home app \
    && rm -rf /var/lib/apt/lists/*

# This file is exported from the committed uv.lock. Every direct and transitive
# runtime dependency is pinned, so a rebuild cannot silently pick newer wheels.
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --requirement requirements.lock

COPY --chown=app:app bot ./bot
COPY --chown=app:app prompt ./prompt
RUN mkdir -p /app/data && chown app:app /app/data

USER app:app
CMD ["python", "-m", "bot"]
