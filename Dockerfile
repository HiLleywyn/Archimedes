# Archimedes -- standalone AI chat bot.
FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Plugin HTTP client defaults -- the guarded outbound HTTP surface handed to
# Lua plugins as arch.http. Baked into the image as visible, tunable defaults;
# an --env-file at run time overrides any of them.
ENV PLUGIN_HTTP_ENABLED=true \
    PLUGIN_HTTP_TIMEOUT_S=20 \
    PLUGIN_HTTP_MAX_BYTES=1048576 \
    PLUGIN_HTTP_MAX_REDIRECTS=3 \
    PLUGIN_HTTP_ALLOW_PRIVATE=false

CMD ["python", "main.py"]
