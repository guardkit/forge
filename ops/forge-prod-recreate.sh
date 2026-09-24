#!/usr/bin/env bash
# Recreate the forge-prod container from its settings of record.
#
# Settings come from the sops-encrypted file below at run time; nothing is kept in plaintext and
# no value ever passes through this script's output. Written 2026-09-03 after the container had to
# be recreated from a saved 'docker inspect' because no settings file existed anywhere.
#
# Rule (binding): only run when every forge build is terminal. The script checks this itself when
# a forge-prod container is present and refuses otherwise.
#
# The repository binds are NOT written here. They are derived from the repository map in forge.yaml
# ('planning.target_repo_paths', read with 'forge repo-paths'), so registering a repository adds its
# bind automatically and the map stays the single source (register-repo spec 2026-09-05, rule 9).
# The two state binds below are fixed and stay as they are. If the map cannot be read, this script
# refuses to recreate rather than start forge-prod with the wrong set of repositories.
#
# That read runs as 'uv run --frozen --no-sync', deliberately: --frozen so reading the map can never
# rewrite the forge checkout's uv.lock, and --no-sync so it never re-installs the virtual environment
# (and never reaches the network to resolve dependencies) in the seconds before 'docker rm -f'. The
# container comes down only after a read that changes nothing.
#
# FORGE_IMAGE is required and has no default (2026-09-24). It used to default to 'forge:latest', which
# is NOT what forge-prod runs: on 24 September the container was running a tagged build from 19
# September while 'forge:latest' pointed at an image ten days older, so anyone running this script
# with nothing set would have quietly downgraded production. The script now refuses and prints both
# candidates — what is running now, and what the repository's release manifest names — and leaves the
# choice to the person running it.
#
# Usage:  FORGE_IMAGE=forge:<tag> bash ops/forge-prod-recreate.sh   # gate, remove the old container, run the new one
#         FORGE_IMAGE=forge:<tag> DRY_RUN=1 bash ...                # print the docker command (names only), change nothing
#         FORGE_CONFIG=/path/forge.yaml bash ...                    # read the repository map from another config
#         bash ops/forge-prod-recreate.sh                           # with no FORGE_IMAGE: refuse, and say what the choices are
#
# To change a setting (for example the LiteLLM base URL or key): sops ~/.config/fleet-secrets/forge/forge-prod.enc.env
set -euo pipefail

FORGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENC="${FORGE_PROD_ENV_ENC:-$HOME/.config/fleet-secrets/forge/forge-prod.enc.env}"

IMAGE="${FORGE_IMAGE:-}"
if [ -z "$IMAGE" ]; then
  {
    echo "FORGE_IMAGE is not set, and this script no longer picks an image for you."
    echo
    # What forge-prod is running right now. Read-only: 'docker inspect' only.
    running_id="$(docker inspect forge-prod --format '{{.Image}}' 2>/dev/null || true)"
    running_ref="$(docker inspect forge-prod --format '{{.Config.Image}}' 2>/dev/null || true)"
    if [ -n "$running_id" ]; then
      running_tags="$(docker image inspect "$running_id" --format '{{join .RepoTags ", "}}' 2>/dev/null || true)"
      echo "Running now:   forge-prod was started from '${running_ref:-unknown}'"
      echo "               image id ${running_id}"
      if [ -n "$running_tags" ]; then
        echo "               tags on that image: ${running_tags}"
      else
        echo "               that image carries no tags"
      fi
    else
      echo "Running now:   there is no forge-prod container here to read (or docker could not be asked)."
    fi
    echo
    # What this repository's release manifest names. Plain text reading, no YAML tool.
    manifest="$FORGE_ROOT/release/manifest.yaml"
    if [ -r "$manifest" ]; then
      m_name="$(sed -n 's/^image_name:[[:space:]]*//p' "$manifest" | head -1 | tr -d '"'"'"' ')"
      m_version="$(sed -n 's/^version:[[:space:]]*//p' "$manifest" | head -1 | tr -d '"'"'"' ')"
      if [ -n "$m_name" ] && [ -n "$m_version" ]; then
        echo "Release named: ${m_name}:${m_version}  (release/manifest.yaml)"
      else
        echo "Release named: release/manifest.yaml is there but names no image_name/version."
      fi
    else
      echo "Release named: there is no release/manifest.yaml in $FORGE_ROOT."
    fi
    echo
    echo "These two are often NOT the same image. Decide which one you mean, then run:"
    echo "  FORGE_IMAGE=<image> bash ops/forge-prod-recreate.sh"
  } >&2
  exit 1
fi

[ -r "$ENC" ] || { echo "settings of record not found: $ENC" >&2; exit 1; }

# The container's settings, by name. PATH / PYTHON_* are the image's own and are NOT passed.
NAMES=(
  FLEET_MEMORY_EMBED_DIMS FLEET_MEMORY_EMBED_MODEL FLEET_MEMORY_EMBED_URL FLEET_MEMORY_ENABLED FLEET_MEMORY_PG_DSN
  FORGE_AUTOBUILD_RUNNER_URL FORGE_HEALTHZ_PORT FORGE_LOG_LEVEL FORGE_NATS_URL
  GIT_AUTHOR_EMAIL GIT_AUTHOR_NAME GIT_COMMITTER_EMAIL GIT_COMMITTER_NAME
  # GUARDKIT_STAMP_MODEL (2026-09-06): the model the stamp normalizer's fallback asks, by the name the
  # router knows it under ("workhorse" on LiteLLM). Unset in the settings file = the code's default name.
  GUARDKIT_STAMP_MODEL GUARDKIT_STAMP_MODEL_MAX_TOKENS GUARDKIT_STAMP_MODEL_URL NODE_OPTIONS OPENAI_API_KEY OPENAI_BASE_URL
  PYTHONDONTWRITEBYTECODE PYTHONUNBUFFERED
)
RUN_ARGS=(docker run -d --name forge-prod --network host --restart unless-stopped
  --user forge --workdir /home/forge --entrypoint forge)
for n in "${NAMES[@]}"; do RUN_ARGS+=(-e "$n"); done

# The repositories the container can build in, straight from the repository map. One '-v' per
# distinct checkout path; the map's two key spellings for the same repository collapse to one bind.
FORGE_CONFIG="${FORGE_CONFIG:-$HOME/forge-state/forge.yaml}"
REPO_PATHS=$(uv run --frozen --no-sync --project "$FORGE_ROOT" forge repo-paths --config "$FORGE_CONFIG") || {
  echo "could not read the repository map from $FORGE_CONFIG ('forge repo-paths' failed) - refusing to recreate forge-prod" >&2
  exit 1
}
REPO_COUNT=0
while IFS= read -r p; do
  [ -n "$p" ] || continue
  RUN_ARGS+=(-v "$p:$p:rw")
  REPO_COUNT=$((REPO_COUNT + 1))
done <<< "$REPO_PATHS"
[ "$REPO_COUNT" -gt 0 ] || {
  echo "the repository map in $FORGE_CONFIG names no checkouts, so forge-prod would have nowhere to build - refusing" >&2
  exit 1
}

# The two state binds. They are fixed in SHAPE and follow the invoking account's home directory
# rather than one written-out home path (2026-09-24), so this script carries no machine's path.
FORGE_STATE_DIR="${FORGE_STATE_DIR:-$HOME/forge-state}"
FORGE_PROD_HOME_STATE="${FORGE_PROD_HOME_STATE:-$HOME/forge-prod-state/.forge}"

RUN_ARGS+=(-v "$FORGE_STATE_DIR:/var/forge:rw"
  -v "$FORGE_PROD_HOME_STATE:/home/forge/.forge:rw"
  "$IMAGE" --config /var/forge/forge.yaml serve)

# sops exec-env takes shell text. Preserve each argument, including spaces and
# literal shell characters in configured paths, using POSIX shell quoting.
# Simple words stay readable in DRY_RUN; this is also the command sops runs.
RUN=""
for arg in "${RUN_ARGS[@]}"; do
  case "$arg" in
    ''|*[!a-zA-Z0-9_@%+=:,./-]*)
      arg="'${arg//\'/\'\\\'\'}'" ;;
  esac
  RUN+="${RUN:+ }$arg"
done

if [ "${DRY_RUN:-0}" = "1" ]; then echo "$RUN"; exit 0; fi

if docker inspect forge-prod >/dev/null 2>&1; then
  STATUS=$(docker exec forge-prod forge --config /var/forge/forge.yaml status 2>&1) || { echo "estate gate: 'forge status' failed - refusing to touch forge-prod" >&2; exit 1; }
  echo "$STATUS" | grep -q BUILD || { echo "estate gate: unexpected 'forge status' output - refusing" >&2; exit 1; }
  echo "$STATUS" | grep -qE 'RUNNING|PAUSED|QUEUED' && { echo "estate gate: a build is RUNNING, PAUSED or QUEUED - refusing (only recreate when every build is terminal)" >&2; exit 1; }
  docker rm -f forge-prod >/dev/null
fi
# sops supplies the values to the docker client's environment; -e NAME copies each into the container.
sops exec-env "$ENC" "$RUN" >/dev/null
sleep 5; docker ps --filter name=forge-prod --format '{{.Names}} {{.Status}}'
