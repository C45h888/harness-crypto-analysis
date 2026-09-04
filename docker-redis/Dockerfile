FROM redis:7.4-alpine

# Telemetry hygiene spec: a tiny Python cleaner runs alongside
# redis-server inside this container. Both share /data (the
# volume) and the local unix socket on /tmp/redis.sock. No
# docker socket is mounted anywhere.

# python3 + pip (the alpine-3.x package name is py3-pip; newer
# alpine has py-pip; try both). The redis PyPI package is
# required by the cleaner script.
RUN apk add --no-cache python3 py3-pip py3-redis 2>/dev/null \
 || apk add --no-cache python3 py-pip 2>/dev/null \
 && pip3 install --no-cache-dir --break-system-packages redis==5.0.4 \
 || pip install --no-cache-dir --break-system-packages redis==5.0.4

COPY telemetry_hygiene_local.py /usr/local/bin/telemetry_hygiene_local.py
COPY redis-entrypoint.sh      /usr/local/bin/redis-entrypoint.sh
RUN chmod +x /usr/local/bin/redis-entrypoint.sh

# Sane defaults; compose env_file can override.
ENV TELEMETRY_HYGIENE_INTERVAL_S=300 \
    TELEMETRY_HYGIENE_MIN_AGE_S=3600 \
    REDIS_HYGIENE_URL=unix:///tmp/redis.sock \
    TELEMETRY_HYGIENE_DRY_RUN=false

EXPOSE 6379
CMD ["/usr/local/bin/redis-entrypoint.sh"]