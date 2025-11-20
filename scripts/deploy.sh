#!/bin/bash
set -e

echo "Received deployment trigger..."

# Navigate to app dir (mounted at /app)
cd /app

# Configure git to handle pulls
git config pull.rebase false

# Pull latest changes (force if needed)
echo "Pulling from git..."
git fetch origin
git reset --hard origin/main

# Rebuild and restart containers
echo "Rebuilding and restarting containers..."
docker compose up -d --build --remove-orphans

echo "Deployment complete!"
