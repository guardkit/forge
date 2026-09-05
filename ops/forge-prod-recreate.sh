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
# Usage:  bash ops/forge-prod-recreate.sh            # gate, remove the old container, run the new one
#         DRY_RUN=1 bash ops/forge-prod-recreate.sh  # print the docker command (names only), change nothing
#         FORGE_CONFIG=/path/forge.yaml bash ...     # read the repository map from another config
#
# To change a setting (for example the LiteLLM base URL or key): sops ~/.config/fleet-secrets/forge/forge-prod.enc.env
set -euo pipefail

ENC="${FORGE_PROD_ENV_ENC:-$HOME/.config/fleet-secrets/forge/forge-prod.enc.env}"
IMAGE="${FORGE_IMAGE:-forge:latest}"
[ -r "$ENC" ] || { echo "settings of record not found: $ENC" >&2; exit 1; }

# The container's settings, by name. PATH / PYTHON_* are the image's own and are NOT passed.
NAMES=(
  FLEET_MEMORY_EMBED_DIMS FLEET_MEMORY_EMBED_MODEL FLEET_MEMORY_EMBED_URL FLEET_MEMORY_ENABLED FLEET_MEMORY_PG_DSN
  FORGE_AUTOBUILD_RUNNER_URL FORGE_HEALTHZ_PORT FORGE_LOG_LEVEL FORGE_NATS_URL
  GIT_AUTHOR_EMAIL GIT_AUTHOR_NAME GIT_COMMITTER_EMAIL GIT_COMMITTER_NAME
  GUARDKIT_STAMP_MODEL_MAX_TOKENS GUARDKIT_STAMP_MODEL_URL NODE_OPTIONS OPENAI_API_KEY OPENAI_BASE_URL
  PYTHONDONTWRITEBYTECODE PYTHONUNBUFFERED
)
ENV_FLAGS=""; for n in "${NAMES[@]}"; do ENV_FLAGS+=" -e $n"; done

# The repositories the container can build in, straight from the repository map. One '-v' per
# distinct checkout path; the map's two key spellings for the same repository collapse to one bind.
FORGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORGE_CONFIG="${FORGE_CONFIG:-$HOME/forge-state/forge.yaml}"
REPO_PATHS=$(uv run --project "$FORGE_ROOT" forge repo-paths --config "$FORGE_CONFIG") || {
  echo "could not read the repository map from $FORGE_CONFIG ('forge repo-paths' failed) - refusing to recreate forge-prod" >&2
  exit 1
}
REPO_BINDS=""
while IFS= read -r p; do
  [ -n "$p" ] || continue
  REPO_BINDS+=" -v $p:$p:rw"
done <<< "$REPO_PATHS"
[ -n "$REPO_BINDS" ] || {
  echo "the repository map in $FORGE_CONFIG names no checkouts, so forge-prod would have nowhere to build - refusing" >&2
  exit 1
}

RUN="docker run -d --name forge-prod --network host --restart unless-stopped --user forge --workdir /home/forge --entrypoint forge${ENV_FLAGS}${REPO_BINDS} \
 -v /home/richardwoollcott/forge-state:/var/forge:rw \
 -v /home/richardwoollcott/forge-prod-state/.forge:/home/forge/.forge:rw \
 $IMAGE --config /var/forge/forge.yaml serve"

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
