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
# Usage:  bash ops/forge-prod-recreate.sh            # gate, remove the old container, run the new one
#         DRY_RUN=1 bash ops/forge-prod-recreate.sh  # print the docker command (names only), change nothing
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
  GUARDKIT_STAMP_MODEL_MAX_TOKENS NODE_OPTIONS OPENAI_API_KEY OPENAI_BASE_URL
  PYTHONDONTWRITEBYTECODE PYTHONUNBUFFERED
)
ENV_FLAGS=""; for n in "${NAMES[@]}"; do ENV_FLAGS+=" -e $n"; done

RUN="docker run -d --name forge-prod --network host --restart unless-stopped --user forge --workdir /home/forge --entrypoint forge${ENV_FLAGS} \
 -v /home/richardwoollcott/Projects/appmilla_github/jarvis:/home/richardwoollcott/Projects/appmilla_github/jarvis:rw \
 -v /home/richardwoollcott/Projects/appmilla_github/study-tutor:/home/richardwoollcott/Projects/appmilla_github/study-tutor:rw \
 -v /home/richardwoollcott/Projects/appmilla_github/ts-api-test:/home/richardwoollcott/Projects/appmilla_github/ts-api-test:rw \
 -v /home/richardwoollcott/forge-state:/var/forge:rw \
 -v /home/richardwoollcott/forge-prod-state/.forge:/home/forge/.forge:rw \
 -v /home/richardwoollcott/Projects/appmilla_github/api_test:/home/richardwoollcott/Projects/appmilla_github/api_test:rw \
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
