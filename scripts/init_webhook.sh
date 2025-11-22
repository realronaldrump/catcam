#!/bin/bash
set -e

# Define paths
# CRITICAL FIX: Use the file from the /app mount (host directory) as the source.
# The previous method (copying to /etc/webhook inside Dockerfile) failed because 
# the base image declares /etc/webhook as a VOLUME, masking the copied file.
TEMPLATE="/app/webhook.json"
CONFIG="/etc/webhook/hooks.json"

echo "⚙️  Initializing Webhook..."

if [ -z "$WEBHOOK_SECRET" ]; then
    echo "❌ Error: WEBHOOK_SECRET environment variable is missing!"
    exit 1
fi

# Verify template exists before attempting to read it
if [ ! -f "$TEMPLATE" ]; then
    echo "❌ Error: Template file not found at $TEMPLATE"
    echo "   Ensure the host directory is mounted to /app in docker-compose.yml"
    exit 1
fi

# Replace the placeholder in the template with the actual environment variable
# and write it to the active config file.
sed "s|{{WEBHOOK_SECRET}}|$WEBHOOK_SECRET|g" "$TEMPLATE" > "$CONFIG"

echo "✅ Secret injected successfully."
echo "🚀 Starting webhook listener..."

# Execute the command passed to the docker container (the webhook tool)
exec /usr/local/bin/webhook "$@"