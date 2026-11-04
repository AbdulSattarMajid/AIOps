FROM python:3.11-slim

RUN useradd --uid 1000 --create-home --home-dir /tmp sandbox \
    && python -m pip install --no-cache-dir pytest

ENV HOME=/tmp
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
USER sandbox
