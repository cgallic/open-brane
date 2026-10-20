FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-core.txt ./
RUN pip install --no-cache-dir -r requirements-core.txt \
    && useradd --uid 10001 --create-home brane \
    && mkdir /data && chown brane:brane /data
COPY SCHEMA.sql brane.py ./
COPY schemas/ schemas/
COPY scripts/ scripts/
COPY docker-entrypoint.sh ./
USER brane
VOLUME ["/data"]
EXPOSE 7778
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7778/healthz', timeout=3)"
ENTRYPOINT ["/bin/sh", "/app/docker-entrypoint.sh"]
