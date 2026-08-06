#!/bin/bash
# Runs ON the robot (deployed alongside bridge.py/whitelist.py by
# start_nao_sensei.ps1). Kills any existing bridge.py process, then starts
# a fresh one fully detached from this SSH session so it keeps running
# after the connection closes — plain `ssh ... "python -u bridge.py"` with
# no nohup/disown was found to NOT reliably survive the local ssh client
# being killed (confirmed live 2026-08-06: the remote process kept running
# as an orphan with no controlling TTY, which is a lucky accident, not a
# guarantee — this script makes the detachment explicit instead).
cd "$(dirname "$0")"

pids=$(ps aux | grep '[b]ridge.py' | awk '{print $2}')
if [ -n "$pids" ]; then
    kill $pids
    sleep 1
fi

nohup python -u bridge.py > bridge.log 2>&1 < /dev/null &
disown 2>/dev/null

sleep 1
echo restarted
