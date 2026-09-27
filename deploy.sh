#!/usr/bin/env bash
#
# Deploys the sdr-recorder images from GitHub Container Registry on the server.
#
# This script NEVER runs git commands. Updating the configuration
# (docker-compose.yml, .env) is a separate step via `git pull`; this script is
# only responsible for what is inside the images.
#
#   ./deploy.sh                 deploy the version from .env (latest by default)
#   ./deploy.sh sha-abc1234     deploy a specific version
#   ./deploy.sh --rollback      go back to the last known-good version
#   ./deploy.sh --list          deployment history
#   ./deploy.sh --status        what is running right now
#
# Requirements: docker with the compose v2 plugin, a directory containing
# docker-compose.yml and .env. Images live in a public repo, so `docker login`
# is not required.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="docker-compose.yml"
HISTORY_FILE=".deploy-history"
HISTORY_KEEP=20
SERVICES=(recorder web transcriber)
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
HEALTH_INTERVAL=3

DEPLOY_TAG="latest"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_DIM=$'\033[2m'
else
  C_RESET=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_DIM=''
fi

log()  { printf '%s==>%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s   !%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  ./deploy.sh [<tag>]      deploy a version (from .env by default, i.e. latest)
  ./deploy.sh --rollback   go back to the last known-good version
  ./deploy.sh --list       deployment history
  ./deploy.sh --status     current container state

Environment variables:
  HEALTH_TIMEOUT=120       seconds to wait for containers to become healthy
  NO_COLOR=1               disable colored output

Example: git pull && ./deploy.sh sha-abc1234
EOF
}

# docker compose with a pinned tag. We deliberately use the base file only --
# docker-compose.override.yml (the local build path) is never merged in.
dc() {
  IMAGE_TAG="$DEPLOY_TAG" docker compose -f "$COMPOSE_FILE" "$@"
}

env_get() {
  local key="$1" default="${2:-}" val=''
  if [ -f .env ]; then
    val="$(grep -E "^[[:space:]]*${key}=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
    val="${val%$'\r'}"
    val="${val#\"}"
    val="${val%\"}"
  fi
  printf '%s' "${val:-$default}"
}

require_tools() {
  command -v docker >/dev/null 2>&1 || die 'docker not found'
  docker compose version >/dev/null 2>&1 || die "'docker compose' not found (v2 plugin)"
  [ -f "$COMPOSE_FILE" ] || die "$COMPOSE_FILE not found in $SCRIPT_DIR"
  if [ ! -f .env ]; then
    warn "no .env -- falling back to the defaults in $COMPOSE_FILE"
  fi
}

# --- history --------------------------------------------------------------

history_tags() {
  [ -f "$HISTORY_FILE" ] || return 0
  grep -E '^[A-Za-z0-9._-]+$' "$HISTORY_FILE" 2>/dev/null || true
}

current_tag() { history_tags | tail -n1; }

# The second-to-last tag. With a history of 0 or 1 entries there is nothing to
# go back to, so return nothing -- a plain `tail -n2 | head -n1` on a single
# entry would yield the *current* tag instead of no tag at all.
previous_tag() {
  local tags
  tags="$(history_tags)"
  if [ "$(printf '%s\n' "$tags" | grep -c . || true)" -lt 2 ]; then
    return 0
  fi
  printf '%s\n' "$tags" | tail -n2 | head -n1
}

prune_history() {
  local count
  count="$(history_tags | wc -l | tr -d ' ')"
  if [ "$count" -gt "$HISTORY_KEEP" ]; then
    history_tags | tail -n"$HISTORY_KEEP" >"$HISTORY_FILE.tmp"
    mv "$HISTORY_FILE.tmp" "$HISTORY_FILE"
  fi
}

# History is a list of versions in deployment order, not a log of events.
# A rollback therefore drops the last entry instead of appending one, so
# repeated --rollback walks further back instead of ping-ponging.
remember() {
  local tag="$1" mode="${2:-append}"
  if [ "$mode" = 'rollback' ]; then
    sed '$d' "$HISTORY_FILE" >"$HISTORY_FILE.tmp"
    mv "$HISTORY_FILE.tmp" "$HISTORY_FILE"
  elif [ "$(current_tag)" != "$tag" ]; then
    printf '%s\n' "$tag" >>"$HISTORY_FILE"
  fi
  prune_history
}

# --- health check ---------------------------------------------------------

HEALTH_FATAL=''
HEALTH_WARN=''

add_fatal() { HEALTH_FATAL="${HEALTH_FATAL}  - $*"$'\n'; }
add_warn()  { HEALTH_WARN="${HEALTH_WARN}  - $*"$'\n'; }

# Note the construction: we deliberately avoid `dc logs | grep -q`. Under
# `set -o pipefail` grep -q closes the pipe as soon as it matches, so
# `docker compose logs` gets SIGPIPE and the whole pipeline returns 141 --
# i.e. "not found" despite a successful match. That false negative would fire
# a bogus failure on every deploy with realistic log volumes.
logs_contain() {
  local out
  out="$(dc logs --tail 300 "$1" 2>&1 || true)"
  case "$out" in
    *"$2"*) return 0 ;;
    *) return 1 ;;
  esac
}

check_containers() {
  local svc cid status restarts
  for svc in "${SERVICES[@]}"; do
    cid="$(dc ps -q "$svc" 2>/dev/null | head -n1 || true)"
    if [ -z "$cid" ]; then
      add_fatal "$svc: container does not exist"
      continue
    fi
    status="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo unknown)"
    if [ "$status" != 'running' ]; then
      add_fatal "$svc: status=$status"
      continue
    fi
    restarts="$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)"
    if [ "$restarts" != '0' ]; then
      add_fatal "$svc: $restarts restarts"
    fi
  done
}

check_web() {
  local port url
  if ! command -v curl >/dev/null 2>&1; then
    warn 'curl not found -- skipping the HTTP check'
    return 0
  fi
  port="$(env_get WEB_PORT 8074)"
  url="http://127.0.0.1:${port}/"
  if ! curl -fsS -o /dev/null --max-time 5 "$url" 2>/dev/null; then
    add_fatal "web: no HTTP response on $url"
  fi
}

check_transcriber() {
  if logs_contain transcriber 'using whisper binary'; then
    return 0
  fi
  add_fatal 'transcriber: missing "using whisper binary" log line (broken image or model?)'
}

# A warning, not a failure: no connection to OpenWebRX is almost always a bad
# address in .env, and rolling the image back would not fix it -- it would only
# muddy the diagnostics.
check_recorder() {
  if ! logs_contain recorder 'receiver ready:'; then
    add_warn 'recorder: missing "receiver ready:" log line -- check OPENWEBRX_WS_URL in .env and the logs'
  fi
}

health_check() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) attempt=0
  log "health check (max ${HEALTH_TIMEOUT}s)"

  # Reset HEALTH_FATAL at the start of every iteration, otherwise the same
  # problems accumulate into one message repeated several times.
  while :; do
    attempt=$((attempt + 1))
    HEALTH_FATAL=''
    check_containers
    check_web
    check_transcriber
    if [ -z "$HEALTH_FATAL" ]; then
      break
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      break
    fi
    if [ "$attempt" -eq 1 ]; then
      printf '%b' "$HEALTH_FATAL" >&2
      printf '%s  ... waiting for containers%s\n' "$C_DIM" "$C_RESET" >&2
    fi
    sleep "$HEALTH_INTERVAL"
  done

  if [ -n "$HEALTH_FATAL" ]; then
    printf '%b' "$HEALTH_FATAL" >&2
    return 1
  fi

  ok 'containers up, web responding, transcriber started'

  HEALTH_WARN=''
  check_recorder
  if [ -n "$HEALTH_WARN" ]; then
    printf '%b' "$HEALTH_WARN" >&2
  fi
  return 0
}

dump_logs() {
  local svc
  for svc in "${SERVICES[@]}"; do
    printf '\n%s--- %s (last 30 lines) ---%s\n' "$C_DIM" "$svc" "$C_RESET" >&2
    dc logs --tail 30 "$svc" 2>&1 | tail -n30 >&2 || true
  done
}

# --- deploy ---------------------------------------------------------------

report() {
  local tag="$1" port
  port="$(env_get WEB_PORT 8074)"
  printf '\n'
  dc images 2>/dev/null || true
  printf '\n'
  ok "deployed ${tag}"
  printf '    web UI: http://<this-host>:%s/\n' "$port"
}

do_deploy() {
  local tag="$1" mode="${2:-append}" prev=''
  DEPLOY_TAG="$tag"
  prev="$(current_tag)"

  log "pull ${tag}"
  if ! dc pull --quiet; then
    die "could not pull the ${tag} images -- did the GitHub Actions build succeed?"
  fi

  log "up ${tag}"
  if ! dc up -d --remove-orphans; then
    warn 'docker compose up failed'
  fi

  if health_check; then
    remember "$tag" "$mode"
    report "$tag"
    return 0
  fi

  dump_logs
  if [ -n "$prev" ] && [ "$prev" != "$tag" ]; then
    warn "health check failed -- rolling back to ${prev}"
    DEPLOY_TAG="$prev"
    if dc up -d --remove-orphans; then
      if health_check; then
        ok "rollback done, back on ${prev}"
      else
        warn 'state after the rollback needs attention too'
      fi
    else
      warn 'rollback failed'
    fi
  else
    warn "no earlier version in $HISTORY_FILE -- nothing to go back to"
  fi
  return 1
}

show_history() {
  local tags total
  tags="$(history_tags)"
  if [ -z "$tags" ]; then
    log "no deployment history ($HISTORY_FILE is missing or empty)"
    return 0
  fi
  total="$(printf '%s\n' "$tags" | grep -c . || true)"
  local i=0
  while IFS= read -r tag; do
    if [ -z "$tag" ]; then
      continue
    fi
    i=$((i + 1))
    if [ "$i" -eq "$total" ]; then
      printf '   %d. %s %s(current)%s\n' "$i" "$tag" "$C_GREEN" "$C_RESET"
    else
      printf '   %d. %s\n' "$i" "$tag"
    fi
  done <<<"$tags"
}

show_status() {
  dc ps 2>/dev/null || true
  printf '\n'
  dc images 2>/dev/null || true
  printf '\n'
  show_history
}

main() {
  require_tools

  case "${1-}" in
    -h | --help)
      usage
      ;;
    --list)
      show_history
      ;;
    --status)
      DEPLOY_TAG="$(current_tag)"
      if [ -z "$DEPLOY_TAG" ]; then
        DEPLOY_TAG="$(env_get IMAGE_TAG latest)"
      fi
      show_status
      ;;
    --rollback)
      local prev
      prev="$(previous_tag)"
      if [ -z "$prev" ]; then
        die "no previous deployment in $HISTORY_FILE"
      fi
      log "rolling back to ${prev}"
      if ! do_deploy "$prev" rollback; then
        die "rollback to ${prev} failed"
      fi
      ;;
    '')
      DEPLOY_TAG="$(env_get IMAGE_TAG latest)"
      if ! do_deploy "$DEPLOY_TAG"; then
        die "deploy of ${DEPLOY_TAG} failed"
      fi
      ;;
    *)
      if ! do_deploy "$1"; then
        die "deploy of $1 failed"
      fi
      ;;
  esac
}

main "$@"
