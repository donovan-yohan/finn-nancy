FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv
FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/finn-nancy
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS runtime
ARG FINN_NANCY_BUILD_SHA
ARG FINN_NANCY_BUILD_TREE_SHA
LABEL org.opencontainers.image.title="finn-nancy" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision="${FINN_NANCY_BUILD_SHA}" \
      io.finn-nancy.source-tree="${FINN_NANCY_BUILD_TREE_SHA}"
ENV FINN_NANCY_BUILD_SHA=${FINN_NANCY_BUILD_SHA} \
    FINN_NANCY_BUILD_TREE_SHA=${FINN_NANCY_BUILD_TREE_SHA} \
    PATH=/opt/finn-nancy/.venv/bin:$PATH \
    PYTHONPATH=/opt/finn-nancy \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/tmp DATA_DIR=/var/lib/finn-nancy DB_PATH=/var/lib/finn-nancy/ledger.sqlite \
    ADDR=0.0.0.0:8080 STRICT_LOCAL_MODE=true PLAID_ENABLED=false EMBEDDINGS_ENABLED=false
WORKDIR /opt/finn-nancy
COPY --from=build /opt/finn-nancy/.venv ./.venv
COPY app ./app
COPY migrations ./migrations
COPY pyproject.toml LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY deploy/container_entrypoint.py ./deploy/container_entrypoint.py
RUN groupadd --gid 10001 finn-nancy && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp finn-nancy \
    && chmod -R a-w /opt/finn-nancy
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/finn-nancy/.venv/bin/python", "/opt/finn-nancy/deploy/container_entrypoint.py"]
