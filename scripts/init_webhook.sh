#!/bin/bash
set -e

# Define paths
TEMPLATE="/etc/webhook/hooks.json.template"
CONFIG="/etc/webhook/hooks.json"

echo "⚙️  Initializing Webhook..."

if [ -z "$WEBHOOK_SECRET" ]; then
    echo "❌ Error: WEBHOOK_SECRET environment variable is missing!"
    exit 1
fi

# Replace the placeholder in the template with the actual environment variable
# and write it to the active config file.
sed "s|{{WEBHOOK_SECRET}}|$WEBHOOK_SECRET|g" "$TEMPLATE" > "$CONFIG"

echo "✅ Secret injected successfully."
echo "🚀 Starting webhook listener..."

# Execute the command passed to the docker container (the webhook tool)
exec /usr/local/bin/webhook "$@"