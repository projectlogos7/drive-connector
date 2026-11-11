FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[content]"
RUN useradd -r -u 10001 nous
USER nous
EXPOSE 8080 8090
# One image, three roles: `serve` (HTTP API), `worker` (indexer + scheduler), `mcp --http` (Index MCP)
ENTRYPOINT ["nous-drive"]
CMD ["serve", "--port", "8080"]
