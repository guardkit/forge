#!/usr/bin/env bash
#
# @@NAME@@ deploy script. Written when this repository was registered, copied
# from api_test's own deploy script with this repository's compose project name
# and ports substituted. It is the script that runs INSIDE the repository's
# Docker Sandbox; the profile names deploy/sandbox-deploy.sh, and that wrapper
# runs this one inside the sandbox.
#
# CONTRACT (forge.executor.shell_steps.deploy_compose / _run_script_step):
#   * The script is invoked as a bare subprocess with NO argv:
#       subprocess.run([program], cwd=<step.params["cwd"]>, env=<os.environ (+ ENV_FILE)>)
#     so ALL inputs arrive via the ENVIRONMENT, never via command-line args.
#   * `cwd` is the profile's `cwd` (deploy/profile.yaml -> cwd:). We ALSO self-
#     anchor to the repo root via BASH_SOURCE so the compose file is found even
#     if the caller's cwd differs.
#   * `env_file` (profile compose.env_file) is exposed as $ENV_FILE (a PATH; the
#     runner never reads it — we source it here if present). This profile sets no
#     env_file, so $ENV_FILE is normally unset.
#
# MODE SIGNAL (see the C4 blocker note below). Exactly ONE mode env may be
# truthy; two or more is refused LOUDLY (deny-by-default, no guessing):
#   * Normal deploy  : all mode envs unset/false -> snapshot current image as the
#                      rollback tag, then `up -d --build`, then wait for health.
#   * O-32 revert     : $REVERT truthy       -> re-tag $ROLLBACK_IMAGE_REF as the
#                      compose image tag, then `up -d --no-build` (the ROLLBACK
#                      image serves), then wait for health.
#   * Candidate       : $CANDIDATE truthy    -> bring a THROWAWAY sandbox copy up
#                      on the -cand project (offset host port $CANDIDATE_PORT) with
#                      the candidate overlay, `up -d --build`, wait for health on
#                      the candidate port. NO rollback snapshot; the LIVE name is
#                      never touched (design §3 candidate-then-promote).
#   * Promote         : $PROMOTE truthy       -> snapshot the current LIVE image as
#                      the rollback tag, RE-TAG the candidate-built image as the
#                      live image (NO rebuild), then live `up -d --no-build`, wait
#                      for health on the live port.
#   * Candidate down  : $CANDIDATE_DOWN truthy -> `down -v --remove-orphans` on the
#                      -cand project (teardown of the sandbox + its db volume).
#
#   The forge revert runbook (runbook_builder.build_revert_runbook) puts
#   `revert: True` and `rollback_image_ref` in the deploy_compose STEP PARAMS;
#   shell_steps.deploy_compose threads them to this script as REVERT=1 and
#   ROLLBACK_IMAGE_REF=<tag> (forge commit deff3c4f, 2026-07-16 — the O-32
#   revert-signal fix this lane surfaced). The revert logic below is proven by
#   deploy/tests/run_deploy_tests.sh against a PATH-shimmed fake docker.
#
# SAFETY: this script is run only by the factory's deploy step, and only inside
# this repository's Docker Sandbox. Prove any change to it against a fake docker
# on PATH — never against the live @@PROJECT@@ project.
set -euo pipefail

# --- anchor to the repo root (where docker-compose.yml lives) ----------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- config (env-overridable; the defaults are this repository's layout) -----
COMPOSE_PROJECT="${COMPOSE_PROJECT:-@@PROJECT@@}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
# The image the `app` service resolves to. The compose `app` service has
# `build: .` and no explicit `image:`, so compose names the built image
# <project>-<service> = @@PROJECT@@-app:latest.
APP_IMAGE="${APP_IMAGE:-@@PROJECT@@-app:latest}"
# The kept rollback tag this script maintains; MUST match profile.rollback_image_ref.
ROLLBACK_IMAGE_REF="${ROLLBACK_IMAGE_REF:-@@PROJECT@@-app:rollback-pre-deploy}"
# --- candidate-then-promote sandbox config (design §3) -----------------------
# Offset host port the candidate publishes (the app port plus one).
CANDIDATE_PORT="${CANDIDATE_PORT:-@@CANDIDATE_PORT@@}"
# The -cand compose project: a lifecycle namespace with its OWN network + db.
CANDIDATE_PROJECT="${CANDIDATE_PROJECT:-${COMPOSE_PROJECT}-cand}"
# The candidate overlay layered on top of $COMPOSE_FILE (remaps the app port).
CANDIDATE_COMPOSE_FILE="${CANDIDATE_COMPOSE_FILE:-deploy/docker-compose.candidate.yml}"
# The image `docker compose -p <cand project> build` produces for the app service:
# compose names build-only images <project>-<service>, so @@PROJECT@@-cand-app:latest.
# PROMOTE re-tags THIS as $APP_IMAGE so the live `up --no-build` serves it.
CANDIDATE_APP_IMAGE="${CANDIDATE_APP_IMAGE:-${CANDIDATE_PROJECT}-app:latest}"
# Health wait (curl the app /health until it reports the DB connected).
HEALTH_URL="${HEALTH_URL:-http://localhost:@@APP_PORT@@/health}"
HEALTH_EXPECT="${HEALTH_EXPECT:-\"database\":\"connected\"}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-120}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-3}"

# Optional env file (forge exposes its PATH via $ENV_FILE); source if present.
if [[ -n "${ENV_FILE:-}" && -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

log() { printf '[deploy.sh] %s\n' "$*"; }

# Echo the image id for a ref, or empty string if the ref is absent.
image_id() {
  docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true
}

# Truthy test for the env var NAMED by $1 (indirect expansion), so one helper
# serves every mode flag: REVERT / CANDIDATE / PROMOTE / CANDIDATE_DOWN.
is_truthy() {
  case "${!1:-}" in
    1 | true | TRUE | yes | YES) return 0 ;;
    *) return 1 ;;
  esac
}

# Poll HEALTH_URL until the body contains HEALTH_EXPECT; fail loud on timeout.
wait_for_health() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  local body=""
  log "waiting for health: ${HEALTH_URL} to contain [${HEALTH_EXPECT}] (timeout ${HEALTH_TIMEOUT_SECONDS}s)"
  while ((SECONDS < deadline)); do
    if body="$(curl -fsS "${HEALTH_URL}" 2>/dev/null)" \
      && printf '%s' "${body}" | grep -qF -- "${HEALTH_EXPECT}"; then
      log "health OK: ${body}"
      return 0
    fi
    sleep "${HEALTH_INTERVAL_SECONDS}"
  done
  log "FATAL: ${HEALTH_URL} did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s"
  return 1
}

deploy_normal() {
  local cur_id
  cur_id="$(image_id "${APP_IMAGE}")"
  log "MODE=normal project=${COMPOSE_PROJECT} app_image=${APP_IMAGE}"
  log "before: ${APP_IMAGE}=${cur_id:-<none>} rollback=${ROLLBACK_IMAGE_REF}=$(image_id "${ROLLBACK_IMAGE_REF}")"
  if [[ -n "${cur_id}" ]]; then
    # Snapshot the currently-running build as the rollback image BEFORE we
    # replace it, so an O-32 revert can bring the prior build back up.
    docker tag "${APP_IMAGE}" "${ROLLBACK_IMAGE_REF}"
    log "snapshotted rollback: ${ROLLBACK_IMAGE_REF}=$(image_id "${ROLLBACK_IMAGE_REF}")"
  else
    # First-ever deploy: nothing running to snapshot (|| true per the contract).
    docker tag "${APP_IMAGE}" "${ROLLBACK_IMAGE_REF}" || true
    log "no current ${APP_IMAGE} to snapshot (first deploy)"
  fi
  docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" up -d --build
  wait_for_health
  log "after: ${APP_IMAGE}=$(image_id "${APP_IMAGE}")"
  log "deploy complete"
}

deploy_revert() {
  local rb_id
  rb_id="$(image_id "${ROLLBACK_IMAGE_REF}")"
  log "MODE=revert project=${COMPOSE_PROJECT} rollback_image_ref=${ROLLBACK_IMAGE_REF}"
  if [[ -z "${rb_id}" ]]; then
    # Loud terminal failure: no kept image to revert to (mirrors forge's own
    # missing-rollback loud fail in stage._run_revert).
    log "FATAL: rollback image ${ROLLBACK_IMAGE_REF} not found -- cannot revert; refusing to keep serving the unverified build"
    return 1
  fi
  log "before: ${APP_IMAGE}=$(image_id "${APP_IMAGE}") rollback=${ROLLBACK_IMAGE_REF}=${rb_id}"
  # Re-tag the kept rollback image as the compose image tag so `up --no-build`
  # brings the ROLLBACK image up (no rebuild -- we re-serve a known-good image).
  docker tag "${ROLLBACK_IMAGE_REF}" "${APP_IMAGE}"
  log "re-tagged ${ROLLBACK_IMAGE_REF} -> ${APP_IMAGE}=$(image_id "${APP_IMAGE}")"
  docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" up -d --no-build
  wait_for_health
  log "after: ${APP_IMAGE}=$(image_id "${APP_IMAGE}") (serving rollback ${rb_id})"
  log "revert complete"
}

deploy_candidate() {
  # A throwaway sandbox copy on the -cand project + offset host port. NO rollback
  # snapshot is taken and the LIVE image/name is NEVER touched: a failing
  # candidate is simply torn down (candidate_down) with the live leg untouched.
  # Health is probed on the CANDIDATE port (the app still listens on @@APP_PORT@@ inside
  # the container; only the host publish moves).
  HEALTH_URL="http://localhost:${CANDIDATE_PORT}/health"
  log "MODE=candidate project=${CANDIDATE_PROJECT} port=${CANDIDATE_PORT} app_image=${CANDIDATE_APP_IMAGE}"
  log "candidate is a throwaway sandbox: no rollback snapshot, the LIVE name is untouched"
  docker compose -p "${CANDIDATE_PROJECT}" \
    -f "${COMPOSE_FILE}" -f "${CANDIDATE_COMPOSE_FILE}" up -d --build
  wait_for_health
  log "after: ${CANDIDATE_APP_IMAGE}=$(image_id "${CANDIDATE_APP_IMAGE}")"
  log "candidate up + healthy on :${CANDIDATE_PORT}"
}

deploy_promote() {
  # Promote the candidate-built image to LIVE. Must NOT rebuild: it re-tags the
  # candidate image as the live image and brings the live project up --no-build,
  # snapshotting the current live image as the rollback tag FIRST (identical to
  # the normal-mode snapshot semantics). Health is probed on the LIVE port.
  local cand_id cur_id
  cand_id="$(image_id "${CANDIDATE_APP_IMAGE}")"
  log "MODE=promote project=${COMPOSE_PROJECT} candidate_image=${CANDIDATE_APP_IMAGE} -> live_image=${APP_IMAGE}"
  if [[ -z "${cand_id}" ]]; then
    # Loud terminal failure: nothing to promote. The candidate leg never built
    # (or was torn down). The LIVE name is untouched.
    log "FATAL: candidate image ${CANDIDATE_APP_IMAGE} not found -- run the CANDIDATE leg first; refusing to promote (LIVE untouched)"
    return 1
  fi
  cur_id="$(image_id "${APP_IMAGE}")"
  # 1) Snapshot the current LIVE build as the rollback tag BEFORE we overwrite it.
  if [[ -n "${cur_id}" ]]; then
    docker tag "${APP_IMAGE}" "${ROLLBACK_IMAGE_REF}"
    log "snapshotted rollback: ${ROLLBACK_IMAGE_REF}=$(image_id "${ROLLBACK_IMAGE_REF}")"
  else
    docker tag "${APP_IMAGE}" "${ROLLBACK_IMAGE_REF}" || true
    log "no current ${APP_IMAGE} to snapshot (first promote)"
  fi
  # 2) Re-tag the candidate-built image as the live image tag -- NO rebuild.
  docker tag "${CANDIDATE_APP_IMAGE}" "${APP_IMAGE}"
  log "promoted image: ${CANDIDATE_APP_IMAGE} -> ${APP_IMAGE}=$(image_id "${APP_IMAGE}")"
  # 3) Bring the LIVE project up on the promoted image WITHOUT rebuilding.
  docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" up -d --no-build
  wait_for_health
  log "after: ${APP_IMAGE}=$(image_id "${APP_IMAGE}") (live serving promoted candidate ${cand_id})"
  log "promote complete"
}

candidate_down() {
  # Teardown helper: remove the -cand project + its db volume + orphans. Used
  # when a candidate gate FAILS (live never touched) or after a promote when
  # candidate.keep is false. The LIVE project is never named here.
  log "MODE=candidate_down project=${CANDIDATE_PROJECT} (tearing the sandbox down with volumes)"
  docker compose -p "${CANDIDATE_PROJECT}" \
    -f "${COMPOSE_FILE}" -f "${CANDIDATE_COMPOSE_FILE}" down -v --remove-orphans
  log "candidate ${CANDIDATE_PROJECT} torn down"
}

# Resolve the single active mode from the truthy flags; refuse ambiguity loudly.
resolve_and_run() {
  local modes=()
  if is_truthy REVERT; then modes+=("revert"); fi
  if is_truthy CANDIDATE; then modes+=("candidate"); fi
  if is_truthy PROMOTE; then modes+=("promote"); fi
  if is_truthy CANDIDATE_DOWN; then modes+=("candidate_down"); fi
  if ((${#modes[@]} > 1)); then
    log "FATAL: ambiguous mode signal (${modes[*]}); set EXACTLY ONE of REVERT / CANDIDATE / PROMOTE / CANDIDATE_DOWN (or none for a normal deploy). Refusing."
    return 2
  fi
  case "${modes[0]:-normal}" in
    revert) deploy_revert ;;
    candidate) deploy_candidate ;;
    promote) deploy_promote ;;
    candidate_down) candidate_down ;;
    normal) deploy_normal ;;
  esac
}

main() {
  log "repo_root=${REPO_ROOT}"
  resolve_and_run
}

main "$@"
