FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

ARG SOURCE_SHA
ARG RELEASE_VERSION
ARG BUILD_TIME

LABEL org.opencontainers.image.source="https://github.com/appolon1908-hue/Codestra-Marketing-" \
      org.opencontainers.image.revision="${SOURCE_SHA}" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.created="${BUILD_TIME}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    CODESTRA_GIT_SHA=${SOURCE_SHA} \
    CODESTRA_RELEASE_ID=${RELEASE_VERSION} \
    CODESTRA_BUILD_TIMESTAMP=${BUILD_TIME} \
    CODESTRA_MIGRATION_REVISION=003_operations

WORKDIR /app
COPY pyproject.toml uv.lock requirements.lock ./
COPY app ./app
RUN case "${SOURCE_SHA}" in ????????????????????????????????????????) ;; *) exit 64 ;; esac \
    && python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && addgroup -S -g 10001 codestra \
    && adduser -S -D -H -u 10001 -G codestra codestra \
    && chown -R 10001:10001 /app

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/health/live || exit 1
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
