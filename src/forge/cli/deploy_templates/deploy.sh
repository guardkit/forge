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
#                      on a candidate project OF THIS CHECK'S OWN (offset host
#                      port $CANDIDATE_PORT) with the candidate overlay,
#                      `up -d --build`, wait for health on the candidate port.
#                      NO rollback snapshot; the LIVE name is never touched
#                      (design §3 candidate-then-promote).
#   * Promote         : $PROMOTE truthy       -> snapshot the current LIVE image as
#                      the rollback tag, RE-TAG the candidate-built image as the
#                      live image (NO rebuild), then live `up -d --no-build`, wait
#                      for health on the live port.
#   * Candidate down  : $CANDIDATE_DOWN truthy -> `down -v --remove-orphans` on THE
#                      ONE candidate project this teardown was handed the
#                      identity of, and nothing else.
#
# ONE CHECK, ONE CANDIDATE (27 September 2026). This template used to name one
# shared candidate project, ${COMPOSE_PROJECT}-cand, for every build of this
# repository: two builds checking at once shared one compose project, one
# container and one built image, and the second to start replaced what the
# first was in the middle of checking. Worse, the teardown took that shared
# name down — so one build's cleanup removed another build's candidate and its
# database with it. That was driven, not imagined, on the project this template
# was copied from: three builds, three databases, one cleanup.
#
# So every candidate project this script makes begins with the prefix
# $CANDIDATE_PROJECT_PREFIX and ends with a token belonging to ONE check: the
# identity the factory handed over in $DEPLOY_IDENTITY (or $CANDIDATE_TOKEN).
# The teardown is handed the SAME identity and takes down that one project. A
# teardown handed no identity removes NOTHING and says so: this script never
# goes looking for candidates, because what it finds can belong to another
# build. The only thing here that removes a candidate it was not told the name
# of is the by-hand `sweep-candidates --remove-every-candidate` command, which
# the factory cannot reach — it runs this script with no arguments at all.
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
# Every candidate compose project of this repository's begins with this prefix
# and ends with a token belonging to ONE check. Each is a lifecycle namespace
# with its OWN network and its OWN db volume.
CANDIDATE_PROJECT_PREFIX="${CANDIDATE_PROJECT_PREFIX:-${COMPOSE_PROJECT}-cand}"
# The candidate overlay layered on top of $COMPOSE_FILE (remaps the app port).
CANDIDATE_COMPOSE_FILE="${CANDIDATE_COMPOSE_FILE:-deploy/docker-compose.candidate.yml}"
# The image prefix the PROMOTE re-tags from: the candidate image is pinned under
# "<prefix>:<identity>" by the check, a reference that names ONE artifact and
# can never be given to another.
IDENTITY_IMAGE_PREFIX="${IDENTITY_IMAGE_PREFIX:-@@PROJECT@@-app}"
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

# The image reference that names ONE artifact and can never be given to another:
# the prefix this repository uses, plus the identity the factory handed over
# with its one '@' turned into a dash (an image reference may not carry '@').
identity_ref() {
  printf '%s:%s\n' "${IDENTITY_IMAGE_PREFIX}" "${1//@/-}"
}

# THE TOKEN THAT MAKES A CANDIDATE PROJECT THIS CHECK'S OWN: the identity the
# factory handed over, reduced to what a compose project name may carry, so the
# teardown handed the same identity finds the same project. $CANDIDATE_TOKEN
# overrides it. With neither, this fails and the caller decides what to do --
# the check makes a fresh token, the teardown refuses.
candidate_token() {
  local raw="${CANDIDATE_TOKEN:-${DEPLOY_IDENTITY:-}}"
  [[ -z "${raw}" ]] && return 1
  raw="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-')"
  raw="${raw%%-}"
  [[ -z "${raw}" ]] && return 1
  printf '%s\n' "${raw}"
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
  # A throwaway sandbox copy on a candidate project OF THIS CHECK'S OWN, on the
  # offset host port. NO rollback snapshot is taken and the LIVE image/name is
  # NEVER touched: a failing candidate is simply torn down (candidate_down) with
  # the live leg untouched. Health is probed on the CANDIDATE port (the app
  # still listens on @@APP_PORT@@ inside the container; only the host publish
  # moves).
  local project token cand_image
  if ! token="$(candidate_token)"; then
    # No identity and no token handed over: a check nobody will promote. It
    # still gets a project of its own rather than a shared one.
    token="fresh-$(date -u +%Y%m%d%H%M%S)-$$"
    log "no DEPLOY_IDENTITY and no CANDIDATE_TOKEN were handed to this check, so it made a token of its own: ${token}. Nothing can tear this candidate down automatically; remove it by hand with: deploy/deploy.sh sweep-candidates --remove-every-candidate"
  fi
  project="${CANDIDATE_PROJECT_PREFIX}-${token}"
  # compose names a build-only image <project>-<service>, and this project name
  # is this check's own, so this image name is too.
  cand_image="${project}-app:latest"
  HEALTH_URL="http://localhost:${CANDIDATE_PORT}/health"
  log "MODE=candidate project=${project} port=${CANDIDATE_PORT} app_image=${cand_image}"
  log "candidate is a throwaway sandbox of this check's own: no rollback snapshot, the LIVE name is untouched, and no other build shares this compose project"
  docker compose -p "${project}" \
    -f "${COMPOSE_FILE}" -f "${CANDIDATE_COMPOSE_FILE}" up -d --build
  wait_for_health
  if [[ -n "${DEPLOY_IDENTITY:-}" ]]; then
    # PIN WHAT WAS CHECKED under a name nothing else can be given, so the
    # promote deploys this artifact rather than whatever a shared name points
    # at by then.
    docker tag "${cand_image}" "$(identity_ref "${DEPLOY_IDENTITY}")"
    log "named what is being checked: $(identity_ref "${DEPLOY_IDENTITY}")=$(image_id "$(identity_ref "${DEPLOY_IDENTITY}")")"
    printf 'CHECKED_ARTIFACT=%s\n' "$(image_id "${cand_image}")"
  fi
  log "after: ${cand_image}=$(image_id "${cand_image}")"
  log "candidate up + healthy on :${CANDIDATE_PORT} (project ${project})"
}

deploy_promote() {
  # Promote the candidate-built image to LIVE. Must NOT rebuild: it re-tags the
  # candidate image as the live image and brings the live project up --no-build,
  # snapshotting the current live image as the rollback tag FIRST (identical to
  # the normal-mode snapshot semantics). Health is probed on the LIVE port.
  local cand_id cur_id cand_ref
  if [[ -z "${DEPLOY_IDENTITY:-}" ]]; then
    # BY IDENTITY OR NOT AT ALL. This script promotes the exact artifact that
    # was checked, named by an identity that cannot be given to another image.
    # Promoting a shared candidate name instead is the defect this refuses:
    # a second build that re-pointed that name between the check and the
    # promote would have its work put live under this build's identity.
    log "FATAL: no DEPLOY_IDENTITY was handed to this promote, so there is no one artifact it could name. Nothing was promoted and the LIVE name is untouched. Hand it the same identity the check was handed."
    return 1
  fi
  cand_ref="$(identity_ref "${DEPLOY_IDENTITY}")"
  cand_id="$(image_id "${cand_ref}")"
  log "MODE=promote project=${COMPOSE_PROJECT} identity=${DEPLOY_IDENTITY} candidate_image=${cand_ref} -> live_image=${APP_IMAGE}"
  if [[ -z "${cand_id}" ]]; then
    # Loud terminal failure: nothing to promote. The candidate leg never built
    # (or was torn down). The LIVE name is untouched.
    log "FATAL: candidate image ${cand_ref} not found -- run the CANDIDATE leg first with this same identity; refusing to promote (LIVE untouched)"
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
  docker tag "${cand_ref}" "${APP_IMAGE}"
  log "promoted image: ${cand_ref} -> ${APP_IMAGE}=$(image_id "${APP_IMAGE}")"
  # 3) Bring the LIVE project up on the promoted image WITHOUT rebuilding.
  docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" up -d --no-build
  wait_for_health
  log "after: ${APP_IMAGE}=$(image_id "${APP_IMAGE}") (live serving promoted candidate ${cand_id})"
  # THE LINE FORGE READS BACK: what is now running here, under the identity it
  # handed over.
  printf 'DEPLOYED_IDENTITY=%s\n' "${DEPLOY_IDENTITY}"
  log "promote complete"
}

tear_one_candidate_down() {
  local project="$1"
  log "tearing ${project} down with its volumes"
  docker compose -p "${project}" \
    -f "${COMPOSE_FILE}" -f "${CANDIDATE_COMPOSE_FILE}" down -v --remove-orphans
  log "candidate ${project} torn down"
}

candidate_down() {
  # Teardown: remove THE ONE candidate project this teardown was told about,
  # its db volume and its orphans. Used when a candidate gate FAILS (live never
  # touched) or after a promote when candidate.keep is false. The LIVE project
  # is never named here.
  #
  # BY NAME OR NOT AT ALL. With no identity this refuses and removes nothing:
  # a teardown that names nothing has to go looking, and what it finds can
  # belong to another build's check.
  local project token
  if ! token="$(candidate_token)"; then
    log "FATAL: no identity and no token were handed to this teardown, so there is no one candidate it could name. Nothing was removed. This script never goes looking for candidates to remove: what it found could belong to another build's check, and removing those takes those builds' databases with them. Hand it the same identity the check was handed (DEPLOY_IDENTITY), or, to clear every candidate of this repository's by hand, run: deploy/deploy.sh sweep-candidates --remove-every-candidate"
    return 2
  fi
  project="${CANDIDATE_PROJECT_PREFIX}-${token}"
  log "MODE=candidate_down project=${project} (named by the identity this teardown was handed)"
  tear_one_candidate_down "${project}"
}

sweep_candidates_by_hand() {
  # THE ONLY THING IN THIS SCRIPT THAT REMOVES A CANDIDATE IT WAS NOT TOLD THE
  # NAME OF, and it is reachable only by a person running this script with two
  # words on the command line. The factory runs this script with NO arguments
  # at all (deploy/profile.yaml names the script and nothing else, and the
  # sandbox wrapper forwards no arguments either), so no request, no runbook
  # and no environment setting can reach this.
  local confirmed="$1" project found any
  if [[ "${confirmed}" != "--remove-every-candidate" ]]; then
    log "FATAL: sweep-candidates removes EVERY candidate project of ${CANDIDATE_PROJECT_PREFIX} and every one of their volumes, including candidates other builds are still checking. Say so explicitly: deploy/deploy.sh sweep-candidates --remove-every-candidate"
    return 2
  fi
  log "MODE=sweep-candidates BY HAND: asking docker which candidate projects of ${CANDIDATE_PROJECT_PREFIX} are up"
  found="$(docker compose ls --all -q 2>/dev/null || true)"
  any=0
  while IFS= read -r project; do
    [[ -z "${project}" ]] && continue
    case "${project}" in
      "${CANDIDATE_PROJECT_PREFIX}"-*) ;;
      *) continue ;;
    esac
    any=1
    tear_one_candidate_down "${project}"
  done <<<"${found}"
  if ((any == 0)); then
    log "no candidate project of ${CANDIDATE_PROJECT_PREFIX} is up; nothing to tear down"
  fi
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
  # THE FACTORY SENDS NO ARGUMENTS. It names this script in deploy/profile.yaml
  # and the modes ride in the environment; the sandbox wrapper forwards no
  # arguments either. So the one command that takes an argument -- the by-hand
  # sweep -- cannot be reached by anything the factory sends, and anything else
  # on the command line is a mistake worth saying out loud.
  if [[ "${1:-}" == "sweep-candidates" ]]; then
    sweep_candidates_by_hand "${2:-}"
    return
  fi
  if (($# > 0)); then
    log "FATAL: this script takes no arguments (the mode rides in the environment); the only by-hand command is 'sweep-candidates --remove-every-candidate'. Got: $*"
    return 2
  fi
  resolve_and_run
}

main "$@"
