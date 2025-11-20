FROM python:3.9-slim

# Install system dependencies
# ffmpeg: for recording
# supervisor: for process management
# git: useful for debugging or if dependencies need it
# iputils-ping: for the ping feature in the dashboard
# procps: for pgrep used in status check
RUN apt-get update && apt-get install -y \
    ffmpeg \
    supervisor \
    git \
    iputils-ping \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ app/
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create data directory for Box mount
RUN mkdir -p /data/Box

# Expose port
EXPOSE 2121

# Start supervisor
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
