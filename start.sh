#!/bin/bash
set -e

# Navigate to project directory
cd "$(dirname "$0")"

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv
fi

# Activate virtualenv
source .venv/bin/activate

# Install dependencies
echo "Ensuring dependencies are installed..."
pip install --quiet -r requirements.txt

# Run the Master Controller Bot
echo "Starting Telegram Multi-Account Controller Bot..."
python3 bot.py
