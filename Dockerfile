FROM python:3.10-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for container security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

# Copy dependencies and install using only verified binary packages
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --only-binary :all: --upgrade pip==25.0.1 && \
    pip install --no-cache-dir --only-binary :all: -r requirements.txt

# Copy application code
COPY home_service.py /app/home_service.py

# Switch to non-root user
USER appuser

# Expose service port
EXPOSE 5001

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:5001/health || exit 1

# Command to run home service
CMD ["uvicorn", "home_service:app", "--host", "0.0.0.0", "--port", "5001"]
