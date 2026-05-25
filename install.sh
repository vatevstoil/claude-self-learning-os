#!/usr/bin/env bash
# Install Claude Self-Learning OS into ~/.claude
set -e
DEST="$HOME/.claude"
mkdir -p "$DEST/scripts" "$DEST/skills" "$DEST/logs"
cp -r scripts/* "$DEST/scripts/"
cp -r skills/* "$DEST/skills/"
[ -f .env ] && cp .env "$DEST/.env" && echo "Copied .env -> $DEST/.env"
echo "Installed scripts + skills to $DEST"
echo "Next: create your Pinecone index (1024-dim, multilingual-e5-large),"
echo "      add config/wiki-map.example.json -> your vault as _shared/wiki-map.json,"
echo "      and schedule automation_dispatcher.py (cron). See docs/SYSTEM_GUIDE.md."
