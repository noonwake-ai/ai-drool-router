# Multi-stage build: the dashboard is compiled once, then the runtime image only
# carries Python plus the built assets. No Node and no build tools at runtime.
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DROOL_DATA_DIR=/data \
    DROOL_CONFIG=/etc/drool-detector/config.json

WORKDIR /app
COPY detector/requirements.txt ./detector/requirements.txt
RUN pip install --no-cache-dir -r detector/requirements.txt

COPY detector/ ./detector/
COPY scripts/ ./scripts/
COPY config.example.json ./
COPY --from=web /web/dist ./web/dist

# The web and worker processes share one image. Which one runs is decided by the
# command, and only the worker is given the Sub2API key.
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd --system --create-home --shell /usr/sbin/nologin drool \
    && mkdir -p /data/public /data/private /data/controls /etc/drool-detector \
    && chown -R drool:drool /data /etc/drool-detector

USER drool
EXPOSE 4191
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4191/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["web"]
