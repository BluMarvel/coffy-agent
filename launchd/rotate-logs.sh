#!/bin/zsh
# Run daily by ~/Library/LaunchAgents/com.mtgroup.coffy-logrotate.plist.
# Rotates the agent and frontend logs once they pass 5 MB, keeping five gzipped
# copies. launchd holds these files open, so they are copied and truncated in
# place: renaming them (as newsyslog does) would leave launchd writing to the old
# file. logs/approvals.jsonl, the approval audit log, is never rotated here.
setopt null_glob
cd "/Users/mturn180/MTGroup LLC/Coffy/real_coffy/logs" || exit 1

limit=$((5 * 1024 * 1024))
for log in agent.out.log agent.err.log frontend.out.log frontend.err.log; do
  [[ -f $log ]] || continue
  (( $(stat -f %z "$log") > limit )) || continue
  for i in 4 3 2 1; do
    [[ -f $log.$i.gz ]] && mv "$log.$i.gz" "$log.$((i + 1)).gz"
  done
  cp "$log" "$log.1" && : > "$log" && gzip -f "$log.1"
done
