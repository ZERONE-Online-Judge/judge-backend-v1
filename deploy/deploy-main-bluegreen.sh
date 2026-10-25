#!/usr/bin/env sh
set -eu

DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BACKEND_DIR=$(CDPATH= cd -- "$DEPLOY_DIR/.." && pwd)
if [ "${ZOJ_DEPLOY_LOCK_HELD:-0}" != "1" ]; then
  exec flock --wait 2400 "$BACKEND_DIR/../service-backup-deploy.lock" \
    env ZOJ_DEPLOY_LOCK_HELD=1 "$0" "$@"
fi
BLUEGREEN="$DEPLOY_DIR/bluegreen.sh"
COMPOSE_FILE="$DEPLOY_DIR/compose.backend.yaml"

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

log() {
  printf '%s\n' "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

ensure_clean_worktree() {
  repo_dir="$1"
  repo_name="$2"
  if [ "${ALLOW_DIRTY:-0}" = "1" ]; then
    return
  fi
  dirty=$(git -C "$repo_dir" status --porcelain | grep -v ' deploy/nginx/api-upstream.conf$' || true)
  if [ -n "$dirty" ]; then
    echo "$repo_name has uncommitted changes. Commit/stash them or run with ALLOW_DIRTY=1." >&2
    printf '%s\n' "$dirty" >&2
    exit 1
  fi
}

pull_main() {
  repo_dir="$1"
  repo_name="$2"
  log "pull $repo_name origin main"
  git -C "$repo_dir" fetch origin main
  git -C "$repo_dir" checkout main
  git -C "$repo_dir" pull --ff-only origin main
}

inactive_color() {
  active="$1"
  case "$active" in
    blue) printf '%s\n' green ;;
    green) printf '%s\n' blue ;;
    *) echo "cannot choose inactive color because active color is unknown" >&2; exit 1 ;;
  esac
}

public_active_color() {
  health_url="${PUBLIC_HEALTH_URL:-http://127.0.0.1:6001/api/health}"
  response="$(curl -fsS "$health_url" 2>/dev/null || true)"
  printf '%s\n' "$response" | sed -n 's/.*"release_color"[[:space:]]*:[[:space:]]*"\(blue\|green\)".*/\1/p' | head -n 1
}

health_via_nginx() {
  expected_color="$1"
  health_url="${PUBLIC_HEALTH_URL:-http://127.0.0.1:6001/api/health}"
  tries=0
  last_response=""
  while :; do
    last_response="$(curl -fsS "$health_url" 2>/dev/null || true)"
    if printf '%s\n' "$last_response" | grep -Eq "\"release_color\"[[:space:]]*:[[:space:]]*\"$expected_color\""; then
      return 0
    fi
    tries=$((tries + 1))
    if [ "$tries" -ge 30 ]; then
      echo "public health check did not route to $expected_color: $health_url" >&2
      echo "last health response: ${last_response:-<empty>}" >&2
      return 1
    fi
    sleep 1
  done
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
Usage:
  ./deploy-main-bluegreen.sh

Environment:
  ALLOW_DIRTY=1        Allow deploy with local uncommitted changes.
  RELEASE_VERSION=x    Override release version. Default: backend git short sha.

Flow:
  check clean backend worktree -> git pull origin main ->
  deploy inactive API -> health check -> nginx switch -> stop previous API
EOF
  exit 0
fi

git -C "$BACKEND_DIR" update-index --skip-worktree deploy/nginx/api-upstream.conf 2>/dev/null || true

ensure_clean_worktree "$BACKEND_DIR" "backend_v1"

pull_main "$BACKEND_DIR" "backend_v1"

release_version="${RELEASE_VERSION:-$(git -C "$BACKEND_DIR" rev-parse --short=12 HEAD)}"

file_active="$("$BLUEGREEN" active || true)"
public_active="$(public_active_color || true)"
active="${public_active:-$file_active}"
if [ -n "$public_active" ] && [ -n "$file_active" ] && [ "$public_active" != "$file_active" ]; then
  log "warning: public active=$public_active differs from upstream file active=$file_active; using public active"
fi
target="$(inactive_color "$active")"

log "active=$active, target=$target, release=$release_version"

log "check database connection budget before changing services"
"$BLUEGREEN" check-capacity

log "ensure shared result cache"
compose up -d redis

log "ensure background workers"
RELEASE_VERSION="$release_version" compose up -d --build mail-worker notice-worker bundle-worker

# Trial payload handling must be active in the API before the new verifier queues jobs.
log "drain verification worker"
compose stop -t 190 verification-ai-worker

log "deploy api-$target"
RELEASE_VERSION="$release_version" "$BLUEGREEN" deploy "$target"

log "verify nginx routes to api-$target"
health_via_nginx "$target"

log "start verification worker against the deployed API"
if [ -f "$DEPLOY_DIR/env/playground.env" ]; then
  log "update isolated verification playground"
  docker build -f "$BACKEND_DIR/playground/Dockerfile.runner" -t zoj-verification-playground:1 "$BACKEND_DIR/playground"
  compose --profile verification-playground up -d --build playground-control
fi
RELEASE_VERSION="$release_version" compose up -d --build verification-ai-worker

if [ "$active" = "blue" ] || [ "$active" = "green" ]; then
  log "stop previous api-$active"
  "$BLUEGREEN" stop "$active"
fi

log "deployment complete: active=$target release=$release_version"
