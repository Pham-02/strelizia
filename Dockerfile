FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STRELIZIA_HOME=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as a normal user. Strelizia's memory (patterns, baselines, alert history) lives in /data.
RUN useradd --create-home --uid 10001 strelizia && mkdir -p /data && chown strelizia /data
USER strelizia
VOLUME /data

ENTRYPOINT ["strelizia"]
CMD ["--help"]
