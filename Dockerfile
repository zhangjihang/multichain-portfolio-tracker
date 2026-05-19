FROM python:3.13-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY src/ src/

# Install
RUN pip install --no-cache-dir .

# Data volume
VOLUME /app/data

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "portfolio_tracker"]
