"""The one feature file a build reads (4 October 2026, design Part 6 point 1).

Jarvis sends ``feature_yaml_path`` relative to the repository. The consumer
used to resolve it against its own working folder and check only the string;
the runner then read ``.guardkit/features/<feature_id>.yaml`` regardless. Now a
relative path is resolved against the repository's REGISTERED checkout and must
be exactly that file; the allowlist then checks the resolved path. An absolute
path (the factory's own planning trigger, ``forge queue``, the fix journey) is
checked exactly as before.

Collaborators are mocks; nothing here touches a bus, a ledger or a checkout.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope

from forge.adapters.nats.pipeline_consumer import (
    REASON_PATH_OUTSIDE_ALLOWLIST,
    PipelineConsumerDeps,
    handle_message,
)
from forge.config.models import ForgeConfig

REPO = "synthetic/project"
FEATURE = "FEAT-AB12"


def _config(allowlist: list[Path], checkouts: dict[str, Path]) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(p) for p in allowlist]}},
            "planning": {
                "target_repo_paths": {k: str(v) for k, v in checkouts.items()}
            },
        }
    )


def _deps(config: ForgeConfig) -> tuple[PipelineConsumerDeps, dict[str, Any]]:
    dispatch = AsyncMock()
    publish = AsyncMock()
    rejections: list[tuple[str, str]] = []
    deps = PipelineConsumerDeps(
        forge_config=config,
        is_duplicate_terminal=AsyncMock(return_value=False),
        dispatch_build=dispatch,
        publish_build_failed=publish,
        record_build_rejection=lambda cid, reason: rejections.append((cid, reason)),
    )
    return deps, {"dispatch": dispatch, "publish": publish, "rejections": rejections}


def _msg(feature_yaml_path: str, *, repo: str = REPO) -> AsyncMock:
    payload = {
        "feature_id": FEATURE,
        "repo": repo,
        "branch": "feature/prepared",
        "feature_yaml_path": feature_yaml_path,
        "triggered_by": "cli",
        "originating_adapter": "cli-wrapper",
        "correlation_id": "corr-path-001",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    envelope = MessageEnvelope(
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        correlation_id="corr-path-001",
        payload=payload,
    )
    msg = AsyncMock()
    msg.data = envelope.model_dump_json().encode("utf-8")
    msg.ack = AsyncMock()
    return msg


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "repos" / "project"
    path.mkdir(parents=True)
    return path


@pytest.mark.asyncio
async def test_a_relative_path_is_resolved_against_the_registered_checkout(
    tmp_path: Path, checkout: Path
) -> None:
    deps, mocks = _deps(_config([tmp_path / "repos"], {REPO: checkout}))

    await handle_message(_msg(f".guardkit/features/{FEATURE}.yaml"), deps)

    mocks["dispatch"].assert_awaited_once()
    mocks["publish"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_allowlist_checks_the_resolved_path_not_the_string(
    tmp_path: Path, checkout: Path
) -> None:
    """The checkout is registered but outside the allowlist: refused, though
    the relative string alone would have resolved against this process's own
    working folder."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    deps, mocks = _deps(_config([other], {REPO: checkout}))
    msg = _msg(f".guardkit/features/{FEATURE}.yaml")

    await handle_message(msg, deps)

    mocks["dispatch"].assert_not_called()
    (failure, _fid), _kw = mocks["publish"].await_args
    assert failure.failure_reason == REASON_PATH_OUTSIDE_ALLOWLIST
    assert str(checkout / ".guardkit" / "features") in mocks["rejections"][0][1]
    msg.ack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "features/count-things/plan.yaml",
        ".guardkit/features/FEAT-0THER.yaml",
        ".guardkit/features/../features/FEAT-AB12.yaml.bak",
        "../outside/.guardkit/features/FEAT-AB12.yaml",
    ],
)
async def test_any_other_relative_path_is_refused_in_plain_words(
    tmp_path: Path, checkout: Path, path: str
) -> None:
    deps, mocks = _deps(_config([tmp_path / "repos"], {REPO: checkout}))
    msg = _msg(path)

    await handle_message(msg, deps)

    mocks["dispatch"].assert_not_called()
    (failure, _fid), _kw = mocks["publish"].await_args
    assert f".guardkit/features/{FEATURE}.yaml" in failure.failure_reason
    assert "the one file a build of" in mocks["rejections"][0][1]
    msg.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_normalised_spelling_of_the_right_file_is_accepted(
    tmp_path: Path, checkout: Path
) -> None:
    deps, mocks = _deps(_config([tmp_path / "repos"], {REPO: checkout}))

    await handle_message(_msg(f"./.guardkit/features/{FEATURE}.yaml"), deps)

    mocks["dispatch"].assert_awaited_once()


@pytest.mark.asyncio
async def test_a_relative_path_for_an_unregistered_repository_is_refused(
    tmp_path: Path, checkout: Path
) -> None:
    deps, mocks = _deps(_config([tmp_path / "repos"], {REPO: checkout}))
    msg = _msg(f".guardkit/features/{FEATURE}.yaml", repo="someone/else")

    await handle_message(msg, deps)

    mocks["dispatch"].assert_not_called()
    assert "someone/else is not registered" in mocks["rejections"][0][1]
    msg.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_absolute_path_is_checked_exactly_as_before(
    tmp_path: Path, checkout: Path
) -> None:
    deps, mocks = _deps(_config([tmp_path / "repos"], {REPO: checkout}))

    # Any absolute path inside the allowlist passes, whatever its name — the
    # factory's own planning trigger sends its plan tree's file this way.
    await handle_message(_msg(str(checkout / "features" / "plan.yaml")), deps)

    mocks["dispatch"].assert_awaited_once()
