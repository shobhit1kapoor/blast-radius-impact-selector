FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS mapbuilder

WORKDIR /app
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir -r /tmp/requirements.lock \
    && python -m pip install --no-cache-dir coverage==7.17.0 pytest-cov==7.1.0
COPY fetch_source.py /app/fetch_source.py
RUN python /app/fetch_source.py
COPY run_coverage.py /app/run_coverage.py
COPY .coveragerc /app/.coveragerc
COPY test_context_plugin.py /app/test_context_plugin.py
COPY sitecustomize.py /app/sitecustomize.py
RUN python /app/run_coverage.py
COPY build_map.py /app/build_map.py
RUN python /app/build_map.py

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY selector.py /app/selector.py
COPY --from=mapbuilder /app/impact_map.json.gz /app/impact_map.json.gz
CMD ["python", "/app/selector.py"]
