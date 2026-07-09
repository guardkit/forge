"""Output-side DEPLOY + LIVE_GATE stage machinery (WS2-B8, scope-design §4).

The last-mile stages that take a merged artifact to a running, health-checked,
live-gated instance on the fleet's own hosts — extending the shipped FEAT-FMDR
runbook executor with the deploy step types (import_realm, inject_secrets,
seed_fixtures, warm_models, health_check, broker_preflight, run_live_gate), a
deploy-profile loader, an F7 deploy-record writer, the B7 deploy-domain
lifecycle publisher, and a swappable reservation-lease interface (scope Q2).

Inert in production until V1: the DEPLOY / LIVE_GATE stages default DISABLED
(``deploy.enabled=False``) and are dispatched only by
:class:`forge.deploy.stage.DeployStageRunner`, never by the greenfield
reasoning loop (they are filtered out of the Mode A/B/C permitted set —
``forge.pipeline.stage_taxonomy.POST_REVIEW_STAGES``).
"""

from forge.deploy.deploy_record import (
    DeployAddendum,
    DeployClaim,
    DeployRecord,
    DeployRecordError,
    render_deploy_record,
    write_deploy_record,
)
from forge.deploy.profile import (
    DeployProfile,
    DeployProfileError,
    load_deploy_profile,
    parse_deploy_profile,
)
from forge.deploy.reservation import (
    InProcessReservationLease,
    ReservationError,
    ReservationLease,
    UnconfiguredReservationLease,
)
from forge.deploy.stage import DeployStageResult, DeployStageRunner

__all__ = [
    "DeployProfile",
    "DeployProfileError",
    "load_deploy_profile",
    "parse_deploy_profile",
    "DeployRecord",
    "DeployClaim",
    "DeployAddendum",
    "DeployRecordError",
    "render_deploy_record",
    "write_deploy_record",
    "ReservationLease",
    "ReservationError",
    "InProcessReservationLease",
    "UnconfiguredReservationLease",
    "DeployStageRunner",
    "DeployStageResult",
]
