# Screenshot Service
# Build context: repo root  (docker build -f suyog/Dockerfile .)
FROM python:3.12-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99

# ── System deps + Chrome ──────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg unzip xvfb \
    fonts-liberation libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
    libwayland-client0 libx11-6 libx11-xcb1 libxcb1 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxkbcommon0 libxrandr2 libxshmfence1 libxtst6 \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Python deps ───────────────────────────────────────────────────────────────
COPY suyog/service_requirements.txt /tmp/service_requirements.txt
RUN pip install --no-cache-dir -r /tmp/service_requirements.txt

# ── Chromedriver (managed by SeleniumBase) ────────────────────────────────────
RUN seleniumbase get chromedriver --path

# ── App ───────────────────────────────────────────────────────────────────────
WORKDIR /app
COPY suyog/ /app/suyog/

COPY suyog/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
