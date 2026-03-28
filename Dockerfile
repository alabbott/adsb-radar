FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

# Default command — override in docker-compose
CMD ["adsb-receiver"]
