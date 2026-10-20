"""Preview ports that do not clash with the project's own ports (20 October 2026).

Found live: a project given slot 1 (previews on 8960 and 8961) runs its own
live app on 8960 and its merge-check copy on 8961 inside its sandbox. A
preview port is the same number inside the sandbox as on the sandbox's
machine, so every preview failed at its first step with Docker's "port is
already allocated", and the card said only "it stopped at deploy_compose".

What these tests hold to:

* :func:`choose_preview_ports` takes, inside the slot's own block of ten, the
  first two ports the project does not use itself; the usual pair when there
  is no clash; a block with fewer than two free is refused in words;
* :func:`ports_the_project_uses` reads only what the deploy profile declares;
* :func:`sandbox_preview` refuses a preview port the project itself uses,
  before anything is started;
* a failed step that gave no reason gets one on the card only when the
  factory recognises the failure, in the factory's own words, and never the
  project's own output.

Every name, address and port owner here is made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite.connect import connect_writer
from forge.config.models import SandboxEntry
from forge.config.sandbox_slots import (
    PreviewPortsRefused,
    choose_preview_ports,
    preview_block,
    slot_ports,
)
from forge.deploy.profile import parse_deploy_profile, ports_the_project_uses
from forge.deploy.review_preview import (
    PreviewLeaseStore,
    ReviewPreviewService,
    known_step_failure,
    sandbox_preview,
)
from forge.deploy.stage import DeployStageResult
from forge.lifecycle import migrations
from tests.forge.deploy.test_review_preview import BASE_URL, FakeClock, FakeDispatch, FakeVenue
from tests.forge.deploy.test_review_preview_sandbox_ports import PUBLIC_HOST, _request

PROJECT = "guardkit/example-project"


def _profile(**sections: Any):
    """A profile shaped like the one found live: the live app and its
    merge-check copy on 8960 and 8961 inside the sandbox, previews written for
    the shared sandbox's 8950 and 8951."""
    raw: dict[str, Any] = {
        "env_id": "example",
        "compose": {"file": "compose.yaml", "script": "deploy/deploy.sh"},
        "sandbox": {
            "name": "example-project",
            "publish": [
                "127.0.0.1:8960:8960",
                "127.0.0.1:8961:8961",
                "127.0.0.1:8950:8950",
                "127.0.0.1:8951:8951",
            ],
        },
        "live_gate": {"driver": ["node", "check.mjs"], "env": {"APP_BASE_URL": "http://127.0.0.1:8960"}},
        "candidate": {
            "env": {
                "CANDIDATE_PORT": "8961",
                "APP_BASE_URL": "http://127.0.0.1:8961",
                "COMPOSE_PROJECT_NAME": "example-live-cand",
            }
        },
        "preview": {
            "enabled": True,
            "slots": [{"port": 8950, "internal_port": 8970}, {"port": 8951, "internal_port": 8971}],
        },
    }
    raw.update(sections)
    return parse_deploy_profile(raw)


def _entry(ports: list[int]) -> SandboxEntry:
    return SandboxEntry(
        name="example-project",
        sidecar_url="http://192.0.2.10:8935",
        runner_url="http://192.0.2.10:8934",
        git_door="http://192.0.2.1:31831/",
        preview_ports=ports,
    )


# ---------------------------------------------------------------------------
# Choosing the two ports


def test_a_clash_moves_the_previews_to_the_next_free_pair_in_the_slot_s_block():
    assert choose_preview_ports(1, {8960, 8961}) == (8962, 8963)
    assert choose_preview_ports(1, {8960}) == (8961, 8962)
    assert choose_preview_ports(1, {8961, 8963}) == (8960, 8962)


def test_no_clash_keeps_the_slot_s_usual_pair():
    for slot in (1, 2, 19):
        assert choose_preview_ports(slot, set()) == slot_ports(slot).previews
        # Ports outside the block never move them.
        assert choose_preview_ports(slot, {8950, 8951, 9999}) == slot_ports(slot).previews


def test_a_block_with_fewer_than_two_free_ports_is_refused_in_plain_words():
    with pytest.raises(PreviewPortsRefused) as refused:
        choose_preview_ports(1, set(range(8960, 8969)))
    words = str(refused.value)
    assert "slot 1's preview ports are 8960 to 8969" in words
    assert "uses 9 of them" in words and "give it another slot" in words


def test_each_slot_s_block_is_its_own():
    blocks = [set(preview_block(slot)) for slot in range(1, 20)]
    assert all(len(block) == 10 for block in blocks)
    assert len(set().union(*blocks)) == 190, "no two slots' blocks share a port"
    assert 8950 not in set().union(*blocks), "slot 0 (the shared sandbox) keeps 8950 to 8959"


# ---------------------------------------------------------------------------
# What the project declares


def test_the_project_s_own_ports_are_read_from_its_declarations():
    used = ports_the_project_uses(_profile())
    assert set(used) == {8960, 8961, 8970, 8971}
    assert used[8960] == "sandbox.publish 127.0.0.1:8960:8960"
    assert used[8970] == "preview.slots[0].internal_port"
    # The rules that publish the profile's own preview ports are the preview,
    # not the project's own app.
    assert 8950 not in used and 8951 not in used


def test_port_settings_and_local_addresses_count_and_nothing_else_does():
    profile = _profile(
        sandbox={"name": "example-project"},
        live_gate={
            "driver": ["node", "check.mjs"],
            "env": {
                "APP_BASE_URL": "http://localhost:8962",
                "MODEL_URL": "http://192.0.2.30:8963",
                "SHOWN_PORT_COUNT": "8964",
                "NOT_A_PORT_NAME": "8965",
            },
        },
        candidate={"env": {"CANDIDATE_PORT": "8966"}},
    )
    used = ports_the_project_uses(profile)
    assert used[8962] == "live_gate.env APP_BASE_URL"
    assert used[8966] == "candidate.env CANDIDATE_PORT"
    assert 8963 not in used, "another machine's port is not this sandbox's"
    assert 8964 not in used and 8965 not in used, "only PORT or a name ending _PORT is a port setting"


# ---------------------------------------------------------------------------
# The safety net: a preview port the project uses is refused before anything


def test_a_preview_port_the_project_uses_itself_is_refused_in_plain_words():
    profile = _profile()
    own, refused = sandbox_preview(
        PROJECT, profile.preview, {PROJECT: _entry([8960, 8961])}, profile=profile
    )
    assert own is None
    assert refused == (
        "guardkit/example-project's preview ports 8960 and 8961 are ports the project itself "
        "uses inside its sandbox example-project (8960: sandbox.publish 127.0.0.1:8960:8960; "
        "8961: sandbox.publish 127.0.0.1:8961:8961), so no preview is started: it would find "
        "the port already taken. To fix it, register the project again with forge "
        "register-repo --slot, which chooses preview ports the project does not use"
    )


def test_ports_the_project_does_not_use_pass_the_safety_net():
    profile = _profile()
    own, refused = sandbox_preview(
        PROJECT, profile.preview, {PROJECT: _entry([8962, 8963])}, profile=profile
    )
    assert refused is None
    assert [(slot.port, slot.internal_port) for slot in own.slots] == [(8962, 8970), (8963, 8971)]


def test_the_shared_sandbox_s_safety_net_names_the_profile_as_the_fix():
    profile = _profile(
        live_gate={"driver": ["node", "check.mjs"], "env": {"APP_BASE_URL": "http://127.0.0.1:8950"}}
    )
    shared = SandboxEntry(name="shared-sbx", sidecar_url="http://127.0.0.1:8925", runner_url="http://127.0.0.1:8924")
    own, refused = sandbox_preview(PROJECT, profile.preview, {PROJECT: shared}, profile=profile)
    assert own is None and "preview port 8950 is a port the project itself uses" in refused
    assert "(8950: live_gate.env APP_BASE_URL)" in refused
    assert refused.endswith("give the preview slot a port the project does not use in its deploy profile")


@pytest.fixture
def connection(tmp_path: Path):
    cx = connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.mark.asyncio
async def test_the_refusal_comes_before_any_compose_step(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    dispatch, venue, clock = FakeDispatch(), FakeVenue(), FakeClock()
    service = ReviewPreviewService(
        store=PreviewLeaseStore(connection),
        profile_reader=lambda _project: _profile(),
        venue_for=lambda _project: venue,
        deploy_dispatcher=dispatch,
        repo_paths={PROJECT: "/clone"},
        evidence_root=tmp_path / "evidence",
        evidence_base_url=BASE_URL,
        public_host=PUBLIC_HOST,
        clock=clock,
        sleep=clock.sleep,
        token_factory=lambda: "the-right-token",
        blocking_sleep=lambda _s: None,
        sandboxes=lambda: {PROJECT: _entry([8960, 8961])},
    )
    evidence = await service.prepare(_request(PROJECT, "build-1", "FEAT-0001"))
    assert evidence.state == "failed"
    assert "preview ports 8960 and 8961 are ports the project itself uses" in evidence.failure_reason
    assert dispatch.calls == [], "no compose step ran"
    assert venue.calls == [], "no tree was laid out in the sandbox"
    assert PreviewLeaseStore(connection).live() == []


# ---------------------------------------------------------------------------
# The card's reason for a failed step that gave none

DOCKER_SAID = (
    " Container example-pv0-api-1 Healthy\n"
    " Container example-pv0-proxy-1 Starting\n"
    "Error response from daemon: failed to set up container networking: driver failed "
    "programming external connectivity on endpoint example-pv0-proxy-1 (***REDACTED-HEX***): "
    "Bind for 127.0.0.1:8960 failed: port is already allocated\n"
)


def _failed(output: str) -> DeployStageResult:
    return DeployStageResult(
        outcome="failed",
        deploy_run_id="pvrun-000000000001",
        failed_step="deploy_compose",
        detail={"refusal": None, "deploy_output": output},
    )


def test_a_port_already_in_use_is_named_on_the_card_in_the_factory_s_words():
    assert ReviewPreviewService._why(_failed(DOCKER_SAID)) == (
        "it stopped at deploy_compose — port 8960 on 127.0.0.1 inside the sandbox was already "
        "in use, so Docker could not start the preview there (it said: port is already allocated)"
    )


def test_the_project_s_own_output_never_reaches_the_card():
    """A script's output can carry what it was handed (the preview's link token)
    or a password it made; none of it is copied onto the card."""
    said = "starting\npreview link https://preview.example.test:8960/?preview=a-secret-token-value\n"
    why = ReviewPreviewService._why(_failed(said))
    assert why == "it stopped at deploy_compose"
    assert "a-secret-token-value" not in why
    assert known_step_failure(said) is None
    assert known_step_failure(None) is None


def test_a_refusal_still_wins_over_the_output():
    result = DeployStageResult(
        outcome="failed",
        deploy_run_id="pvrun-000000000001",
        failed_step="deploy_compose",
        detail={"refusal": "sidecar refused (HTTP 400)", "deploy_output": DOCKER_SAID},
    )
    assert ReviewPreviewService._why(result) == "it stopped at deploy_compose — sidecar refused (HTTP 400)"
