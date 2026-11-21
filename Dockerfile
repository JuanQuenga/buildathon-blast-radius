# Blast Radius selector (MIT). Same base image the organisers labelled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0

WORKDIR /app
COPY selector/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/pin.tar.gz
RUN mkdir /app/sqlglot && tar xzf /tmp/pin.tar.gz --strip-components=1 -C /app/sqlglot && rm /tmp/pin.tar.gz
ENV OPENBLAS_NUM_THREADS=1 SKIP_INTEGRATION=0
COPY selector/heavy_cases.py /app/selector/heavy_cases.py
RUN cd /app/sqlglot && python -sB /app/selector/heavy_cases.py ds --record /app/ds.json.gz && python -sB /app/selector/heavy_cases.py ds --time-only --record /app/ds-costs.json.gz && python -sB /app/selector/heavy_cases.py parser --record /app/parser.json.gz
COPY selector/staged.py /app/selector/staged.py
RUN cd /app/sqlglot && python -sB /app/selector/staged.py record
COPY selector/ /app/selector/
ENV BLAST_HEAVY=1
RUN python -m selector.prepare_runtime
# Precompile (the runtime FS is read-only) and make everything readable by nobody.
RUN python -m compileall -q /app/selector && chmod -R a+rX /app

USER 65534:65534
CMD ["python", "-m", "selector.main"]
