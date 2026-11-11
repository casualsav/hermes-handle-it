#!/usr/bin/env bash
# Record the demo run on a virtual display: the browser Hermes drives on the left, Hermes's
# terminal (the user's side) on the right. Writes, into OUTDIR:
#   full.mp4   both sides, real speed
#   agent.mp4  the agent's screen (the support chat), cropped from full.mp4
#   user.mp4   the user's side (Hermes's terminal), cropped from full.mp4
#   markers.txt  seconds from the start of the recording for each step
# The two crops come from one recording, so their timestamps line up exactly.
# Needs Xvfb, tmux, ffmpeg, a Chromium, the plugin installed and enabled, and the demo login
# saved in Hermes's vault (origin http://127.0.0.1:8765, sam@example.com / northwind-demo).
#
#   demo/record/record.sh demo/raw
#
# HERMES_ARGS picks the model, e.g. HERMES_ARGS="--provider deepseek -m deepseek-chat".
# The user turns down the partial refund (second choice on the first approval card) and
# approves the full one (first choice on the second).
set -euo pipefail
OUTDIR=${1:?usage: record.sh OUTDIR}
mkdir -p "$OUTDIR"
OUT="$OUTDIR/full.mp4"
MARK="$OUTDIR/markers.txt"; : > "$MARK"
HERE=$(cd "$(dirname "$0")" && pwd)
SHOP_PORT=${SHOP_PORT:-8765}
MIRROR_PORT=${MIRROR_PORT:-8766}
SPEED=${SPEED:-0.6}
DISP=${DISP:-:99}
W=2240 H=940
CHROME=${CHROME:-$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | sort -V | tail -1)}
ASK="Northwind Outfitters' support chat is at http://127.0.0.1:${SHOP_PORT}/?login=1&speed=${SPEED} (sign in with my saved Northwind login). The zipper on my Ridgeline rain jacket broke after two weeks. Get me a full refund for order 1234. You can share the order number and my name, Sam Rivera."
TMP=$(mktemp -d)
pids=()
cleanup() {
  tmux kill-session -t hsdemo 2>/dev/null || true
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  rm -rf "${TMP:?}"
}
trap cleanup EXIT

Xvfb "$DISP" -screen 0 ${W}x${H}x24 >/dev/null 2>&1 & pids+=($!)
sleep 1
python3 "$HERE/../serve.py" --port "$SHOP_PORT" >/dev/null 2>&1 & pids+=($!)
tmux new-session -d -s hsdemo -x 122 -y 54 \
  "cd ~ && DISPLAY=$DISP AGENT_BROWSER_HEADED=1 hermes chat ${HERMES_ARGS:-}"
python3 "$HERE/term_mirror.py" hsdemo --port "$MIRROR_PORT" & pids+=($!)
sleep 1
DISPLAY=$DISP "$CHROME" --user-data-dir="$TMP/mirror" --no-sandbox --test-type --disable-infobars --no-first-run --disable-gpu \
  --app="http://127.0.0.1:${MIRROR_PORT}/" --window-position=1300,0 --window-size=940,${H} >/dev/null 2>&1 & pids+=($!)

wait_for() { timeout "$2" bash -c "until tmux capture-pane -p -t hsdemo | grep -q \"$1\"; do sleep 1; done"; }
wait_for "Welcome to Hermes" 60
sleep 10
ffmpeg -loglevel error -y -f x11grab -framerate 12 -video_size ${W}x${H} -i "$DISP" \
  -c:v libx264 -preset veryfast -crf 26 -pix_fmt yuv420p "$OUT" </dev/null & FF=$!
T0=$(date +%s.%N)
mark() { printf '%6.1f  %s\n' "$(echo "$(date +%s.%N) - $T0" | bc)" "$1" >> "$MARK"; }
sleep 2
# Type the ask like a person would.
for (( i=0; i<${#ASK}; i+=3 )); do tmux send-keys -t hsdemo -l "${ASK:i:3}"; sleep 0.04; done
sleep 1.5
tmux send-keys -t hsdemo Enter
# The prompt can swallow input typed while it is still starting: retype until the turn starts.
for _ in 1 2 3; do
  wait_for "Initializing agent\|preparing" 15 && break
  tmux send-keys -t hsdemo C-u
  tmux send-keys -t hsdemo -l "$ASK"; sleep 1; tmux send-keys -t hsdemo Enter
done
mark "user sent the ask"
card() {  # wait for approval card number $1 (earlier answered cards leave a "Clarify:" line)
  timeout 400 bash -c "until [ \$(tmux capture-pane -p -S -2000 -t hsdemo | grep -c 'Clarify:') -ge $(( $1 - 1 )) ] \
    && tmux capture-pane -p -t hsdemo | grep -q 'needs your input'; do sleep 1; done"
}
card 1; mark "approval 1 shown: the 20% partial-refund offer"
sleep 6
tmux send-keys -t hsdemo Down; sleep 0.6; tmux send-keys -t hsdemo Enter
mark "user turned the partial refund down"
card 2; mark "approval 2 shown: the full-refund offer"
sleep 6
tmux send-keys -t hsdemo Enter
mark "user accepted the full refund"
wait_for "Chat closed" 300; mark "chat closed, transcript saved"
sleep 25  # Hermes writes its answer to the user
mark "end"
kill -INT "$FF"; wait "$FF" || true
ffmpeg -loglevel error -y -i "$OUT" -vf "crop=1296:${H}:0:0" -c:v libx264 -preset veryfast -crf 24 "$OUTDIR/agent.mp4"
ffmpeg -loglevel error -y -i "$OUT" -vf "crop=940:${H}:1300:0" -c:v libx264 -preset veryfast -crf 24 "$OUTDIR/user.mp4"
echo "recorded $OUTDIR"; cat "$MARK"
