#!/usr/bin/env bash
# Update the site to the newest commits of both repositories.
#
# Fetches the branch of gramps-web and gramps-web-api and fast-forwards them.
# When they differ from what was last deployed successfully, builds the image
# while the site keeps running, restarts the containers and translates the
# new strings of the tree. Otherwise it only fetches, so the systemd timer
# next to this file can run it every few minutes (see README.md,
# "Автообновление"). A failed build is tried again by the next run.
#
#   ./update.sh           update if there is something new
#   ./update.sh --force   rebuild and restart even without new commits
#
# Local changes to tracked files, another checked out branch or a history that
# does not fast-forward stop the update: nothing is overwritten.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_DIR="$(dirname "$DEPLOY_DIR")"
WEB_DIR="$(dirname "$API_DIR")/gramps-web"
BRANCH="${TREE_BRANCH:-branch-admins}"
# rootless Docker: a non-interactive shell or a timer has no DOCKER_HOST set
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/$(id -u)/docker.sock}"
# revisions of the last successful update
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/tree-update/deployed"

log() { echo "[$(date '+%F %T')] $*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

# one update at a time, by hand or by the timer
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/tree-update.lock"
flock -n 9 || { log "Another update is running"; exit 0; }

for repo in "$WEB_DIR" "$API_DIR"; do
  name="$(basename "$repo")"
  [ "$(git -C "$repo" rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] \
    || fail "$name is not on branch $BRANCH"
  git -C "$repo" fetch --quiet origin "$BRANCH"
  if git -C "$repo" merge-base --is-ancestor "origin/$BRANCH" HEAD; then
    continue  # nothing new upstream (or commits here not pushed yet)
  fi
  [ -z "$(git -C "$repo" status --porcelain --untracked-files=no)" ] \
    || fail "$name has local changes; commit or discard them first"
  git -C "$repo" merge --ff-only --quiet "origin/$BRANCH" \
    || fail "$name: origin/$BRANCH does not fast-forward"
  log "$name: $(git -C "$repo" log --oneline -1)"
done

revisions="$(git -C "$WEB_DIR" rev-parse HEAD) $(git -C "$API_DIR" rev-parse HEAD)"
if [ "$revisions" = "$(cat "$STATE" 2>/dev/null)" ] && [ "${1:-}" != "--force" ]; then
  exit 0  # silent: the timer runs this often
fi

cd "$DEPLOY_DIR"
log "Building the image (the site keeps running)"
docker compose build
log "Restarting"
docker compose up -d

# the new strings of the tree: done once here, so readers do not wait for them
log "Waiting for the site"
ready=0
for _ in $(seq 60); do
  if docker compose exec -T grampsweb python3 -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:5000/ready')" \
    >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 5
done
[ "$ready" = 1 ] || fail "the site did not start in 5 minutes: docker compose logs grampsweb"
log "Translating new strings"
docker compose exec -T grampsweb python3 -m gramps_webapi \
  --config /app/config/config.cfg translate all \
  || log "Translation failed; pages translate their strings when shown"

# images of earlier builds, of this user's Docker only
docker image prune -f >/dev/null
mkdir -p "$(dirname "$STATE")"
echo "$revisions" >"$STATE"
log "Done: gramps-web $(git -C "$WEB_DIR" rev-parse --short HEAD)," \
  "gramps-web-api $(git -C "$API_DIR" rev-parse --short HEAD)"
