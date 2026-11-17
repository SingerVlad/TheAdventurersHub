FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0 \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && mkdir -p /data/foundry/imports \
    && chown -R app:app /data/foundry

COPY --chown=app:app app.py ./app.py

USER app
EXPOSE 5000

CMD ["sh", "-c", "exec gunicorn --workers ${WEB_CONCURRENCY:-2} --bind 0.0.0.0:5000 --access-logfile - --error-logfile - --timeout 240 app:app"]
