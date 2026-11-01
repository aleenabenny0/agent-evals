# The docs agent service. The snapshot and the results are mounted, never copied in.
FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD ["/app/.venv/bin/python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
