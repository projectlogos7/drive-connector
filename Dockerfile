# 1) Build the Next.js web app (static export)
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web ./
RUN NODE_ENV=production npx next build

# 2) Python service (API + worker + MCP), serving the built web app at /app
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 NOUS_DRIVE_WEB_DIR=/app/web
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
COPY --from=web /web/out /app/web
RUN useradd -r -u 10001 nous && mkdir -p /home/nous && chown nous /home/nous
USER nous
ENV HOME=/home/nous
EXPOSE 8080 8090
# One image, three roles: `serve` (web app + API), `worker` (indexing), `mcp --http` (Index MCP)
ENTRYPOINT ["nous-drive"]
CMD ["serve", "--port", "8080"]
