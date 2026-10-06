FROM python:3.12-slim

# Extra root CAs (optional). Behind a TLS-intercepting proxy put its root CA(s) as PEM files in
# docker/certs/*.crt (git-ignored; see docs/RUNBOOK.md). Without that directory this is a no-op.
# Every TLS client below trusts the system bundle: Python ssl/httpx (SSL_CERT_FILE), pip/requests
# (REQUESTS_CA_BUNDLE) and uv (native/system certificates). Verification is never disabled.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    UV_SYSTEM_CERTS=1
COPY docker /tmp/luna-docker
RUN if ls /tmp/luna-docker/certs/*.crt >/dev/null 2>&1; then \
        cp /tmp/luna-docker/certs/*.crt /usr/local/share/ca-certificates/ \
        && update-ca-certificates; \
    fi \
    && rm -rf /tmp/luna-docker

RUN pip install --no-cache-dir uv \
    && useradd --create-home --uid 1000 luna

ENV UV_NO_SYNC=1 UV_FROZEN=1 UV_NO_DEV=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false STREAMLIT_SERVER_HEADLESS=true

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY luna ./luna
COPY config ./config
COPY dashboard ./dashboard
RUN uv sync --frozen --no-dev \
    && mkdir -p /app/var \
    && chown -R luna:luna /app

USER luna
ENTRYPOINT ["uv", "run", "luna"]
