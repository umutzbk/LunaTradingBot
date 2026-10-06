FROM python:3.12-slim

RUN pip install --no-cache-dir uv \
    && useradd --create-home --uid 1000 luna

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
