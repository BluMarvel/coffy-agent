#!/bin/zsh
# Started by ~/Library/LaunchAgents/com.mtgroup.coffy-agent.plist at login.
# Runs the Coffy voice agent under the "local" deployment the frontend dispatches to.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "/Users/mturn180/MTGroup LLC/Coffy/real_coffy" || exit 1

# Wait for the network after login so the first connection to LiveKit Cloud doesn't fail.
until /sbin/ping -c1 -t2 1.1.1.1 >/dev/null 2>&1; do sleep 5; done

# --no-reload: dev mode restarts the agent whenever a source file changes, which
# drops a call in progress. `lk agent start` would avoid that too, but it cannot
# register under the "local" deployment the frontend dispatches to
# (AGENT_DEPLOYMENT in frontend/.env.local). Restart with
#   launchctl kickstart -k gui/$(id -u)/com.mtgroup.coffy-agent
# after changing the code.
exec lk agent dev --deployment local --no-reload
