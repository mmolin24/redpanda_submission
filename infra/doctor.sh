#!/bin/sh
set -u

scope="${1:-contributor}"
case "${scope}" in
  launch|contributor)
    ;;
  *)
    echo "Usage: $0 [launch|contributor]" >&2
    exit 2
    ;;
esac

script_directory="${0%/*}"
if [ "${script_directory}" = "$0" ]; then
  script_directory=.
fi
default_root="$(CDPATH= cd -- "${script_directory}/.." && pwd -P)"
doctor_root="${DOCTOR_ROOT:-${default_root}}"
PATH="${DOCTOR_PATH:-${PATH}}"
export PATH

: "${DOCKER_CLIENT_TIMEOUT:=5}"
: "${COMPOSE_HTTP_TIMEOUT:=5}"
export DOCKER_CLIENT_TIMEOUT COMPOSE_HTTP_TIMEOUT

failures=0

pass() {
  printf 'PASS %s %s: %s\n' "$1" "$2" "$3"
}

fail() {
  printf 'FAIL %s %s: %s\n' "$1" "$2" "$3"
  failures=$((failures + 1))
}

warn() {
  printf 'WARN %s %s: %s\n' "$1" "$2" "$3"
}

docker_available=false
compose_available=false
docker_endpoint_local=false
daemon_available=false

if command -v docker >/dev/null 2>&1 &&
  docker --version >/dev/null 2>&1; then
  docker_available=true
  pass launch docker-cli "Docker CLI is available."
else
  fail launch docker-cli "Install Docker with the Compose v2 plugin."
fi

if [ "${docker_available}" = true ]; then
  if docker compose version >/dev/null 2>&1; then
    compose_available=true
    pass launch compose-plugin "Docker Compose v2 is available."
  else
    fail launch compose-plugin "Install or enable the Docker Compose v2 plugin."
  fi

  docker_endpoint=""
  if [ -n "${DOCKER_HOST:-}" ]; then
    docker_endpoint="${DOCKER_HOST}"
  elif ! docker_endpoint="$(
      docker context inspect \
        --format '{{(index .Endpoints "docker").Host}}' 2>/dev/null
    )"; then
    docker_endpoint=""
  fi
  case "${docker_endpoint}" in
    unix:///*)
      docker_endpoint_local=true
      pass launch docker-endpoint "The Docker endpoint is a local Unix socket."
      ;;
    *)
      fail launch docker-endpoint "Select a local Unix-socket Docker context."
      ;;
  esac

  if [ "${docker_endpoint_local}" = true ]; then
    if docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
      daemon_available=true
      pass launch docker-daemon "The Docker daemon is responsive."
    else
      fail launch docker-daemon "Start a local Docker daemon."
    fi
  else
    fail launch docker-daemon "A local Docker daemon was not inspected."
  fi
fi

if [ "${daemon_available}" = true ]; then
  docker_operating_system="$(docker info --format '{{.OSType}}' 2>/dev/null || true)"
  if [ "${docker_operating_system}" = linux ]; then
    pass launch linux-containers "The daemon is using Linux containers."
  else
    fail launch linux-containers "Switch Docker to Linux-container mode."
  fi
fi

if [ "${compose_available}" = true ] &&
  [ "${docker_endpoint_local}" = true ]; then
  if [ -f "${doctor_root}/docker-compose.yml" ] &&
    (
      CDPATH= cd -- "${doctor_root}" &&
        docker compose --env-file /dev/null \
          -f "${doctor_root}/docker-compose.yml" \
          --profile '*' config --quiet
    ) >/dev/null 2>&1; then
    pass launch compose-model "All checked-in Compose profiles resolve without .env."
  else
    fail launch compose-model "Run from a complete checkout and inspect docker compose config."
  fi
elif [ "${compose_available}" = true ]; then
  fail launch compose-model "A non-local Docker endpoint was not inspected."
fi

if [ "${scope}" = contributor ]; then
  for contributor_tool in git uv curl; do
    if command -v "${contributor_tool}" >/dev/null 2>&1; then
      pass contributor "${contributor_tool}" "${contributor_tool} is available."
    else
      fail contributor "${contributor_tool}" "Install ${contributor_tool} for the full contributor gate."
    fi
  done

  if command -v make >/dev/null 2>&1 &&
    make_version="$(make --version 2>/dev/null)"; then
    case "${make_version}" in
      "GNU Make "*)
        pass contributor make "GNU Make is available."
        ;;
      *)
        fail contributor make "Install GNU Make for the full contributor gate."
        ;;
    esac
  else
    fail contributor make "Install GNU Make for the full contributor gate."
  fi

  if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
      >/dev/null 2>&1; then
      pass contributor python3 "Python 3.12 or newer is available."
    else
      fail contributor python3 "Install Python 3.12 or newer."
    fi
  else
    fail contributor python3 "Install Python 3.12 or newer."
  fi

  if [ "${docker_available}" = true ] &&
    docker buildx version >/dev/null 2>&1; then
    pass contributor buildx "Docker Buildx is available."
  else
    fail contributor buildx "Install or enable Docker Buildx."
  fi

  if command -v node >/dev/null 2>&1; then
    node_version="$(node --version 2>/dev/null || true)"
    node_core="${node_version#v}"
    node_major="${node_core%%.*}"
    node_remainder="${node_core#*.}"
    node_minor="${node_remainder%%.*}"
    node_patch="${node_remainder#*.}"
    node_patch="${node_patch%%[-+]*}"
    node_supported=false
    case "${node_version}" in
      v*.*.*)
        case "${node_major}:${node_minor}:${node_patch}" in
          *[!0-9:]*|:*|*::*|*:)
            ;;
          *)
            if [ "${#node_major}" -le 6 ] &&
              [ "${#node_minor}" -le 6 ] &&
              [ "${#node_patch}" -le 6 ]; then
              case "${node_major}:${node_minor}" in
                20:*)
                  if [ "${node_minor}" -ge 19 ]; then
                    node_supported=true
                  fi
                  ;;
                21:*)
                  ;;
                22:*)
                  if [ "${node_minor}" -ge 12 ]; then
                    node_supported=true
                  fi
                  ;;
                *)
                  if [ "${node_major}" -gt 22 ]; then
                    node_supported=true
                  fi
                  ;;
              esac
            fi
            ;;
        esac
        ;;
      *)
        ;;
    esac
    if [ "${node_supported}" = true ]; then
      pass contributor node "Node satisfies Vite's supported version range."
    else
      fail contributor node "Use Node 20.19+, skip 21, or use Node 22.12+."
    fi
  else
    fail contributor node "Install Node 20.19+ or 22.12+."
  fi

  if command -v pnpm >/dev/null 2>&1; then
    pnpm_version="$(pnpm --version 2>/dev/null || true)"
    if [ "${pnpm_version}" = 11.0.8 ]; then
      pass contributor pnpm "pnpm matches packageManager version 11.0.8."
    else
      fail contributor pnpm "Activate pnpm 11.0.8 through Corepack."
    fi
  else
    fail contributor pnpm "Activate pnpm 11.0.8 through Corepack."
  fi

  frontend_ready=true
  if [ ! -f "${doctor_root}/web/pnpm-lock.yaml" ] ||
    [ ! -f "${doctor_root}/web/node_modules/.pnpm/lock.yaml" ] ||
    ! command -v cmp >/dev/null 2>&1 ||
    ! cmp -s \
      "${doctor_root}/web/pnpm-lock.yaml" \
      "${doctor_root}/web/node_modules/.pnpm/lock.yaml"; then
    frontend_ready=false
  fi
  for frontend_binary in eslint prettier tsc vite vitest; do
    if [ ! -x "${doctor_root}/web/node_modules/.bin/${frontend_binary}" ]; then
      frontend_ready=false
      break
    fi
  done
  if [ "${frontend_ready}" = true ]; then
    pass contributor frontend-dependencies "Frozen frontend dependencies are installed."
  else
    fail contributor frontend-dependencies \
      "Run pnpm --dir web install --frozen-lockfile."
  fi

  if command -v git >/dev/null 2>&1; then
    if git -C "${doctor_root}" rev-parse --verify HEAD >/dev/null 2>&1; then
      pass contributor git-head "The checkout has a reviewable Git HEAD."
    else
      warn contributor git-head \
        "No Git HEAD exists; clean-clone readiness cannot be claimed before the initial commit."
    fi
  fi
fi

if [ "${failures}" -eq 0 ]; then
  printf 'Doctor %s passed.\n' "${scope}"
  exit 0
fi

printf 'Doctor %s failed with %s issue(s).\n' "${scope}" "${failures}"
exit 1
