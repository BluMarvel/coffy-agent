#!/bin/zsh
# Started by ~/Library/LaunchAgents/com.mtgroup.coffy-frontend.plist at login.
# Serves the Coffy web page on http://localhost:3001.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "/Users/mturn180/MTGroup LLC/Coffy/real_coffy/frontend" || exit 1

exec npm run dev -- --port 3001
