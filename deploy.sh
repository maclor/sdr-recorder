#!/usr/bin/env bash
#
# Wdrożenie obrazów sdr-recorder z GitHub Container Registry na serwer.
#
# Ten skrypt NIGDY nie wykonuje operacji na gicie. Aktualizację konfiguracji
# (docker-compose.yml, .env) robisz osobno, przez `git pull`; tutaj
# odpowiada wyłącznie za to, co jest w obrazach.
#
#   ./deploy.sh                 wdrożenie wersji z .env (domyślnie latest)
#   ./deploy.sh sha-abc1234     wdrożenie konkretnej wersji
#   ./deploy.sh --rollback      powrót do poprzednio udanej wersji
#   ./deploy.sh --list          historia wdrożeń
#   ./deploy.sh --status        co aktualnie działa
#
# Wymagania: docker z pluginem compose v2, katalog z docker-compose.yml i .env.
# Obrazy są w repo publicznym, więc `docker login` nie jest potrzebny.

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
die()  { printf '%sbłąd%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Użycie:
  ./deploy.sh [<tag>]      wdrożenie wersji (domyślnie z .env, czyli latest)
  ./deploy.sh --rollback   powrót do poprzednio udanej wersji
  ./deploy.sh --list       historia wdrożeń
  ./deploy.sh --status     aktualny stan kontenerów

Zmienne środowiskowe:
  HEALTH_TIMEOUT=120       ile sekund czekać na zdrowe kontenery
  NO_COLOR=1               bez kolorów

Przykład: git pull && ./deploy.sh sha-abc1234
EOF
}

# docker compose z przypiętym tagiem. Używamy wyłącznie pliku bazowego —
# docker-compose.override.yml (lokalny build) nigdy nie jest mieszany.
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
  command -v docker >/dev/null 2>&1 || die 'brak dockera'
  docker compose version >/dev/null 2>&1 || die "brak 'docker compose' (plugin v2)"
  [ -f "$COMPOSE_FILE" ] || die "brak $COMPOSE_FILE w $SCRIPT_DIR"
  if [ ! -f .env ]; then
    warn "brak .env — używam wartości domyślnych z $COMPOSE_FILE"
  fi
}

# --- historia -------------------------------------------------------------

history_tags() {
  [ -f "$HISTORY_FILE" ] || return 0
  grep -E '^[A-Za-z0-9._-]+$' "$HISTORY_FILE" 2>/dev/null || true
}

current_tag() { history_tags | tail -n1; }

# Tag przedostatni. Przy historii długości 0 lub 1 nie ma czego cofać,
# więc zwracamy pustkę — samo `tail -n2 | head -n1` przy jednym wpisie
# zwróciłoby tag bieżący, a nie żaden.
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

# Historia to lista wersji w kolejności wdrożenia, a nie log zdarzeń.
# Dlatego rollback nie dopisuje wersji, tylko cofa ostatni wpis — dzięki temu
# kolejne --rollback cofają się coraz dalej wstecz, a nie naprzemien.
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

# Uwaga na konstrukcję: NIE używamy tu `dc logs | grep -q`. Przy `set -o pipefail`
# grep -q zamyka pipe zaraz po trafieniu, `docker compose logs` dostaje SIGPIPE
# i cały pipeline zwraca 141 — czyli „nie znaleziono” mimo sukcesu. Ten sam
# fałszywy wynik dałby fałszywy alarm przy wdrożeniu.
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
      add_fatal "$svc: kontener nie istnieje"
      continue
    fi
    status="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo unknown)"
    if [ "$status" != 'running' ]; then
      add_fatal "$svc: status=$status"
      continue
    fi
    restarts="$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)"
    if [ "$restarts" != '0' ]; then
      add_fatal "$svc: $restarts restartów"
    fi
  done
}

check_web() {
  local port url
  if ! command -v curl >/dev/null 2>&1; then
    warn 'brak curl — pomijam sprawdzenie HTTP'
    return 0
  fi
  port="$(env_get WEB_PORT 8074)"
  url="http://127.0.0.1:${port}/"
  if ! curl -fsS -o /dev/null --max-time 5 "$url" 2>/dev/null; then
    add_fatal "web: brak odpowiedzi HTTP na $url"
  fi
}

check_transcriber() {
  if logs_contain transcriber 'using whisper binary'; then
    return 0
  fi
  add_fatal 'transcriber: brak logu "using whisper binary" (uszkodzony obraz lub model?)'
}

# Ostrzeżenie, a nie błąd: brak łączności z OpenWebRX to zwykle zły adres
# w .env, a rollback obrazu tego nie naprawi i tylko zaburzy diagnostykę.
check_recorder() {
  if ! logs_contain recorder 'receiver ready:'; then
    add_warn 'recorder: brak logu "receiver ready:" — sprawdź OPENWEBRX_WS_URL w .env i logi'
  fi
}

health_check() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) attempt=0
  log "health check (maks ${HEALTH_TIMEOUT}s)"

  # HEALTH_FATAL zerujemy na początku każdej iteracji, inaczej te same
  # problemy kumulowałyby się w komunikat powtórzony trzy razy.
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
      printf '%s  … czekam na kontenery%s\n' "$C_DIM" "$C_RESET" >&2
    fi
    sleep "$HEALTH_INTERVAL"
  done

  if [ -n "$HEALTH_FATAL" ]; then
    printf '%b' "$HEALTH_FATAL" >&2
    return 1
  fi

  ok 'kontenery działają, web odpowiada, transcriber wystartował'

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
    printf '\n%s--- %s (ostatnie 30 linii) ---%s\n' "$C_DIM" "$svc" "$C_RESET" >&2
    dc logs --tail 30 "$svc" 2>&1 | tail -n30 >&2 || true
  done
}

# --- wdrożenie ------------------------------------------------------------

report() {
  local tag="$1" port
  port="$(env_get WEB_PORT 8074)"
  printf '\n'
  dc images 2>/dev/null || true
  printf '\n'
  ok "wdrożono ${tag}"
  printf '    strona: http://<ten-serwer>:%s/\n' "$port"
}

do_deploy() {
  local tag="$1" mode="${2:-append}" prev=''
  DEPLOY_TAG="$tag"
  prev="$(current_tag)"

  log "pull ${tag}"
  if ! dc pull --quiet; then
    die "nie udało się pobrać obrazów ${tag} — czy build na GitHub Actions się powiódł?"
  fi

  log "up ${tag}"
  if ! dc up -d --remove-orphans; then
    warn 'docker compose up zakończył się błędem'
  fi

  if health_check; then
    remember "$tag" "$mode"
    report "$tag"
    return 0
  fi

  dump_logs
  if [ -n "$prev" ] && [ "$prev" != "$tag" ]; then
    warn "health check nieudany — rollback do ${prev}"
    DEPLOY_TAG="$prev"
    if dc up -d --remove-orphans; then
      if health_check; then
        ok "rollback zakończony, wróciłem do ${prev}"
      else
        warn 'stan po rollbacku też wymaga uwagi'
      fi
    else
      warn 'rollback nie powiódł się'
    fi
  else
    warn "brak wcześniejszej wersji w $HISTORY_FILE — nie mam dokąd wracać"
  fi
  return 1
}

show_history() {
  local tags total
  tags="$(history_tags)"
  if [ -z "$tags" ]; then
    log "brak historii wdrożeń ($HISTORY_FILE nie istnieje lub jest pusty)"
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
      printf '   %d. %s %s(bieżąca)%s\n' "$i" "$tag" "$C_GREEN" "$C_RESET"
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
        die "brak poprzedniego wdrożenia w $HISTORY_FILE"
      fi
      log "rollback do ${prev}"
      if ! do_deploy "$prev" rollback; then
        die "rollback do ${prev} nie powiódł się"
      fi
      ;;
    '')
      DEPLOY_TAG="$(env_get IMAGE_TAG latest)"
      if ! do_deploy "$DEPLOY_TAG"; then
        die "wdrożenie ${DEPLOY_TAG} nie powiodło się"
      fi
      ;;
    *)
      if ! do_deploy "$1"; then
        die "wdrożenie $1 nie powiodło się"
      fi
      ;;
  esac
}

main "$@"
