FROM python:3.11-slim

# Install system dependencies
# ffmpeg: for recording
# iputils-ping: for camera ping check
# git: for auto-deploy script inside webhook container (if we used one image for all, but webhook is separate)
# Actually, webhook container needs git/docker client. 
# This Dockerfile is for 'web' and 'recorder'.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default command (overridden in compose)
CMD ["python", "src/main.py"]
