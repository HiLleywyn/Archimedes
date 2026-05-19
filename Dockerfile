# Archimedes -- standalone AI chat bot.

# Stage 1: build the Node agent sidecar (the OpenRouter Agent SDK service).
FROM node:20-bookworm-slim AS sidecar
WORKDIR /sidecar
COPY agent-sidecar/package.json ./
RUN npm install --no-audit --no-fund
COPY agent-sidecar/tsconfig.json ./
COPY agent-sidecar/src ./src
RUN npm run build && npm prune --omit=dev

# Stage 2: the bot image.
FROM python:3.12-slim-bookworm

WORKDIR /app

# ripgrep backs the agent shell tool's content search; grep stays as a
# fallback. ca-certificates and libstdc++6 are runtime dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libstdc++6 ripgrep \
    && rm -rf /var/lib/apt/lists/*

# The Node runtime, lifted from the build image, runs the agent sidecar.
COPY --from=sidecar /usr/local/bin/node /usr/local/bin/node

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The built agent sidecar and its production dependencies.
COPY --from=sidecar /sidecar/dist ./agent-sidecar/dist
COPY --from=sidecar /sidecar/node_modules ./agent-sidecar/node_modules

# Plugin HTTP client defaults -- the guarded outbound HTTP surface handed to
# Lua plugins as arch.http. Baked into the image as visible, tunable defaults;
# an --env-file at run time overrides any of them.
ENV PLUGIN_HTTP_ENABLED=true \
    PLUGIN_HTTP_TIMEOUT_S=20 \
    PLUGIN_HTTP_MAX_BYTES=1048576 \
    PLUGIN_HTTP_MAX_REDIRECTS=3 \
    PLUGIN_HTTP_ALLOW_PRIVATE=false

CMD ["python", "main.py"]
