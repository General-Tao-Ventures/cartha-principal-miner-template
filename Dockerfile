FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for asyncpg and bittensor
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency file first for layer caching
COPY pyproject.toml ./

# Install Python dependencies (core + bittensor)
RUN pip install --no-cache-dir -e ".[bt]"

# Copy application code
COPY . .

# Default port for the API
EXPOSE 8100

# Default command: run the API server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
