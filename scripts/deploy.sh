#!/bin/bash
set -e

echo "Received deployment trigger..."

# Navigate to app dir (mounted at /app)
cd /app

# Pull latest changes
echo "Pulling from git..."
git pull origin main

# Rebuild and restart containers
echo "Rebuilding and restarting containers..."
docker compose up -d --build --remove-orphans

echo "Deployment complete!"
