FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.27 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/opt/venv/bin:$PATH"

# system deps: build tools for native wheels; ffmpeg/libs for audio codecs (livekit
# plugins); git for the one dependency installed from a repo rather than PyPI
# (livekit-plugins-raya — see pyproject.toml).
#
# No postgres client, on purpose: every query here goes through asyncpg, and an
# ad-hoc dump wants the `postgres` image, whose client matches the server exactly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

# pre-download the plugin model weights that are fetched rather than shipped —
# today that is Silero VAD — so cold start never reaches the network. The audio
# turn detector is not among them: its weights are compiled into the
# livekit-local-inference wheel. Tolerate absence in case plugins change.
RUN python -m livekit.agents download-files || echo "download-files skipped"

COPY . .

EXPOSE 8000 9091
