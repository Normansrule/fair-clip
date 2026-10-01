# clipwarden app (pipeline + dashboard). Python 3.12 on Debian trixie, pinned by digest.
FROM python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CLIPWARDEN_IN_CONTAINER=1 CLIPWARDEN_CONFIG=/data/clipwarden.toml
WORKDIR /app
# Hash-pinned dependencies: pip refuses anything whose hash doesn't match the lockfile.
COPY requirements-youtube.lock ./
RUN pip install --require-hashes -r requirements-youtube.lock
# Our own code runs from source (no build step, so no unpinned build tools are downloaded).
COPY src ./src
COPY config ./config
COPY examples ./examples
ENV PYTHONPATH=/app/src
RUN useradd --system --uid 10000 --no-create-home --shell /usr/sbin/nologin app
USER 10000:10000
EXPOSE 8765
ENTRYPOINT ["python", "-m", "clipwarden.cli"]
CMD ["dashboard"]
