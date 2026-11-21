# Blast Radius selector (MIT). Same base image the organisers labelled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0

WORKDIR /app
COPY selector/requirements.lock selector/wheels.lock /tmp/
RUN pip install --disable-pip-version-check --no-cache-dir --no-deps -c /tmp/requirements.lock -r /tmp/wheels.lock
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/pin.tar.gz
RUN mkdir /app/sqlglot && tar xzf /tmp/pin.tar.gz --strip-components=1 -C /app/sqlglot && rm /tmp/pin.tar.gz
ENV OPENBLAS_NUM_THREADS=1 PYTHON_CPU_COUNT=1 SKIP_INTEGRATION=0
COPY selector/ /app/selector/
# Only pin-derived probe inputs and traces from the frozen Python environment.
ENV BLAST_HEAVY=1
RUN python -m selector.prepare_runtime
# Precompile (the runtime FS is read-only) and make everything readable by nobody.
RUN python -m compileall -q /app/selector /app/sqlglot/sqlglot /app/sqlglot/tests && chmod -R a+rX /app

ENV MALLOC_ARENA_MAX=2
USER 65534:65534
CMD ["python", "-m", "selector.main"]

# Practice diagnostic only. Choose an explicitly qualified final entry.
ENV BLAST_PRACTICE_DIAGNOSTIC=D1
