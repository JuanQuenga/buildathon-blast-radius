# Blast Radius selector (MIT). Same base image the organisers labelled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0

WORKDIR /app
COPY selector/ /app/selector/
RUN --mount=type=bind,source=tools/prepare_runtime.py,target=/tmp/prepare_runtime.py PYTHONPATH=/app python /tmp/prepare_runtime.py
# Precompile (the runtime FS is read-only) and make everything readable by nobody.
RUN python -m compileall -q /app/selector && chmod -R a+rX /app

USER 65534:65534
CMD ["python", "-m", "selector.main"]
