"""The coordinator's side of publication: the send, the attempts, the pick-up.

One-true-copy design pass, item 1: the first revision's item 2, the second
revision's A and B, and the third revision's E and G.

What the merge word does once both kinds of check have passed on the joined
result and publication is switched on:

1. writes "about to send" with the attempt and the exact inputs;
2. asks the publisher — a separate process holding the one credential that
   can write to a remote — carrying the turn number;
3. on published-and-contains-it, writes "done send" and answers "published,
   deployment pending". IT STOPS THERE. The deploy is the next stage;
4. on the one refusal worth trying again — the remote moved — sets the join
   aside under its own name, fetches a new commit, makes a new join on a name
   of its own, and runs BOTH kinds of check again on it. At most three;
5. on any other refusal, no credential, or a publisher it cannot reach:
   "publication pending" with the reason, and nothing sent.

PICKING UP a send reads the remote FIRST and asks whether the branch CONTAINS
the joined commit, never whether it IS it.

The "remote" here is a bare repository on disk. Nothing contacts anything
real, starts a service, builds an image or touches a sandbox.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_executor import (
    MergeExecutorDeps,
    execute_merge_deploy,
)
from forge.pipeline.publication_activation import WhatTheMachineSays
from forge.pipeline.publication_record import (
    RESULT_PUBLICATION_PENDING,
    RESULT_PUBLISHED_DEPLOYMENT_PENDING,
    STEP_SEND,
    PublicationRecordStore,
)
from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 - fixtures
    BUILD_ID,
    CORRELATION,
    FEATURE_ID,
    MAIN_SHA,
    REPO,
    _ensure_build,
    _FakeDeploy,
    _FakePublisher,
    _git,
    _JoinsForReal,
    _legs,
    _receipts_env,
    pool,
    repo_root,
)

#: A machine somebody has looked at and found every wall standing. Without
#: one of these, publication cannot switch on at all — which is where the
#: estate is today, and the safe side.
EVERY_WALL_STANDS = WhatTheMachineSays(
    a_sandbox_can_write_the_coordinators_settings_file=False,
    a_sandbox_can_see_the_ledger=False,
    a_sandbox_can_reach_the_publisher=False,
    the_credential_file_can_be_read_by_them=False,
    only_the_coordinator_is_on_the_publishers_network=True,
    looked_at_by="a stand-in, in a test",
)


@pytest.fixture
def config_with_publication_on(repo_root: Path) -> ForgeConfig:  # noqa: F811
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "approval": {"expected_approver": "rich"},
            "merge_executor": {"enabled": True},
            "publication": {
                "enabled": True,
                "publisher_url": "http://127.0.0.1:1",
                "builds_may_run_inside_the_coordinator": False,
                "publisher_credential_file": "/etc/forge-publisher/credential",
            },
        }
    )


@pytest.fixture
def config_with_publication_off(repo_root: Path) -> ForgeConfig:  # noqa: F811
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "approval": {"expected_approver": "rich"},
            "merge_executor": {"enabled": True},
        }
    )


class _APublisherThatSays:
    """A stand-in for the publisher's process. It records what it was asked.

    ``answers`` is one answer per request, in order; the last one is repeated
    if it is asked more often. ``before_answering`` is called with the request
    first, which is how "the remote moved between the check and the send" is
    driven: another hand really lands work on the bare repository.
    """

    def __init__(
        self,
        answers: list[dict[str, Any]],
        *,
        before_answering: Any = None,
    ) -> None:
        self.answers = answers
        self.before_answering = before_answering
        self.asked: list[dict[str, Any]] = []
        self.configs: list[Any] = []

    async def __call__(self, config: Any, request: dict[str, Any]) -> dict[str, Any]:
        self.configs.append(config)
        self.asked.append(dict(request))
        if self.before_answering is not None:
            self.before_answering(request)
        index = min(len(self.asked) - 1, len(self.answers) - 1)
        return dict(self.answers[index])


def _published(remote_now: str) -> dict[str, Any]:
    return {
        "published": True,
        "remote_now": remote_now,
        "contains_j": True,
        "refusal": None,
        "refusal_kind": None,
    }


def _the_remote_moved(remote_now: str = "b" * 40) -> dict[str, Any]:
    return {
        "published": False,
        "remote_now": remote_now,
        "contains_j": False,
        "refusal": (
            "the join was made onto an older commit and the branch does not "
            "contain it. Sending would move that branch sideways rather than "
            "forwards, so nothing was sent."
        ),
        "refusal_kind": "the-remote-moved",
    }


def _some_other_refusal(kind: str, said: str) -> dict[str, Any]:
    return {
        "published": False,
        "remote_now": None,
        "contains_j": False,
        "refusal": said,
        "refusal_kind": kind,
    }


def _deps(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,  # noqa: F811
    *,
    publisher: Any = None,
    machine: WhatTheMachineSays | None = EVERY_WALL_STANDS,
) -> tuple[MergeExecutorDeps, _FakeDeploy, _JoinsForReal, _FakePublisher]:
    bus = _FakePublisher()
    joins = _JoinsForReal()
    deploy = _FakeDeploy()
    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=bus,
        guardkit_run=joins,
        deploy_dispatcher=deploy,
        publisher=publisher,
        what_the_machine_says=machine,
    )
    return deps, deploy, joins, bus


async def _press(
    deps: MergeExecutorDeps, repo_root: Path  # noqa: F811
) -> Any:
    _ensure_build(deps.pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    return await execute_merge_deploy(
        deps=deps,
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        repo=REPO,
        repo_root=repo_root,
        expect_main_sha=MAIN_SHA,
        correlation_id=CORRELATION,
        decided_by="rich",
    )


def _record(pool: SqliteLifecyclePersistence) -> Any:  # noqa: F811
    return PublicationRecordStore(pool.connection).read(BUILD_ID)


def _lines(record: Any) -> list[str]:
    return [f"{line.kind} {line.step} ({line.attempt})" for line in record.lines]


def _somebody_else_lands_work(repo_root: Path, what: str) -> str:  # noqa: F811
    """Another hand pushes to the bare repository this repository came from."""
    bare = _git(repo_root, "remote", "get-url", "origin")
    other = repo_root.parent / f"other-{what}"
    subprocess.run(
        ["git", "clone", "-q", bare, str(other)], check=True, capture_output=True
    )
    (other / what).write_text("somebody else's work\n", encoding="utf-8")
    _git(other, "add", what)
    _git(other, "commit", "-q", "-m", f"somebody else landed {what}")
    _git(other, "push", "-q", "origin", "main")
    return _git(other, "rev-parse", "main")


class TestItSendsAndStopsAtPublished:
    @pytest.mark.asyncio
    async def test_the_remote_has_it_and_nothing_is_deployed(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        publisher = _APublisherThatSays([_published("c" * 40)])
        deps, deploy, joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "published-deployment-pending"
        assert outcome.status == "PASSED"
        assert "published" in outcome.detail
        assert "deployment pending" in outcome.detail
        assert "Nothing has been deployed" in outcome.detail
        # THE DEPLOY DID NOT RUN. Only the check and its tear-down.
        assert _legs(deploy) == ["candidate_check", "candidate_down"]
        assert "promote" not in _legs(deploy)

    @pytest.mark.asyncio
    async def test_the_record_says_about_to_send_then_done_send(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        publisher = _APublisherThatSays([_published("c" * 40)])
        deps, _deploy, _joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )

        await _press(deps, repo_root)

        record = _record(pool)
        assert _lines(record) == [
            "about to join (1)",
            "done join (1)",
            "done merge-checks (1)",
            "about to candidate-check (1)",
            "done candidate-check (1)",
            "about to send (1)",
            "done send (1)",
        ]
        assert record.result == RESULT_PUBLISHED_DEPLOYMENT_PENDING
        sent = [line for line in record.lines if line.step == STEP_SEND]
        assert sent[0].detail["j_commit"] == record.j_commit
        assert sent[1].detail["published"] is True
        assert sent[1].detail["ran_on"] == record.j_commit

    @pytest.mark.asyncio
    async def test_the_request_carries_the_turn_and_no_credential(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        publisher = _APublisherThatSays([_published("c" * 40)])
        deps, _deploy, _joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )

        await _press(deps, repo_root)

        assert len(publisher.asked) == 1
        asked = publisher.asked[0]
        assert set(asked) == {
            "project",
            "build_id",
            "turn",
            "j_commit",
            "target_branch",
        }
        assert asked["turn"] == _record(pool).turn
        assert asked["target_branch"] == "main"
        assert asked["project"] == REPO
        said = json.dumps(asked)
        for word in ("credential", "token", "password", "secret"):
            assert word not in said.lower()


class TestTheRemoteMovingIsTheOneRefusalWorthTryingAgain:
    @pytest.mark.asyncio
    async def test_a_second_attempt_with_a_new_join_and_both_checks_again(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        landed: list[str] = []

        def another_hand(_request: dict[str, Any]) -> None:
            if not landed:
                landed.append(_somebody_else_lands_work(repo_root, "between"))

        publisher = _APublisherThatSays(
            [_the_remote_moved(), _published("d" * 40)],
            before_answering=another_hand,
        )
        deps, deploy, joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "published-deployment-pending"
        assert len(publisher.asked) == 2
        # A NEW JOIN, on a name of its own, made by running the merge command
        # again — not the same commit sent twice.
        assert len(joins.calls) == 2
        targets = [
            call["args"][call["args"].index("--target") + 1] for call in joins.calls
        ]
        assert targets == [
            f"factory-integration/{FEATURE_ID}",
            f"factory-integration/{FEATURE_ID}-attempt-2",
        ]
        assert publisher.asked[0]["j_commit"] != publisher.asked[1]["j_commit"]
        # AND BOTH KINDS OF CHECK RAN AGAIN on the new joined result.
        assert _legs(deploy).count("candidate_check") == 2
        record = _record(pool)
        assert _lines(record).count("done merge-checks (2)") == 1
        assert "done candidate-check (2)" in _lines(record)
        # EVERY JOIN IS KEPT, under the name its own attempt gave it.
        assert _git(repo_root, "rev-parse", f"factory-integration/{FEATURE_ID}")
        assert _git(
            repo_root, "rev-parse", f"factory-integration/{FEATURE_ID}-attempt-2"
        )
        # AND THE RECORD'S OWN COMMITS ARE ONE CONSISTENT SET: the publisher
        # checks, for itself, that the joined commit is a merge of exactly the
        # RECORDED commit and the recorded build tip, so a record still
        # carrying the first attempt's commit would make the second attempt's
        # join look forged.
        second_join = _git(
            repo_root, "rev-parse", f"factory-integration/{FEATURE_ID}-attempt-2"
        )
        parents = _git(
            repo_root, "rev-list", "--parents", "-n", "1", second_join
        ).split()[1:]
        assert record.j_commit == second_join
        assert parents == [record.g_commit, record.build_tip]

    @pytest.mark.asyncio
    async def test_three_attempts_and_then_publication_pending(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        times: list[int] = []

        def another_hand(_request: dict[str, Any]) -> None:
            times.append(len(times))
            _somebody_else_lands_work(repo_root, f"again-{len(times)}")

        publisher = _APublisherThatSays(
            [_the_remote_moved()], before_answering=another_hand
        )
        deps, deploy, joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )

        outcome = await _press(deps, repo_root)

        assert len(publisher.asked) == 3
        assert len(joins.calls) == 3
        assert _legs(deploy).count("candidate_check") == 3
        assert outcome.result == "publication-pending"
        assert outcome.status == "GATED"
        assert "was not published" in outcome.detail
        assert "moved under every one of the 3 attempts" in outcome.detail
        assert _record(pool).result == RESULT_PUBLICATION_PENDING
        # EVERY JOINED COMMIT IS KEPT, under its own name.
        for name in (
            f"factory-integration/{FEATURE_ID}",
            f"factory-integration/{FEATURE_ID}-attempt-2",
            f"factory-integration/{FEATURE_ID}-attempt-3",
        ):
            assert _git(repo_root, "rev-parse", name)
        # Three different joined commits, one per attempt.
        assert len({asked["j_commit"] for asked in publisher.asked}) == 3
        # NOTHING WAS DEPLOYED, on any attempt.
        assert "promote" not in _legs(deploy)


class TestEveryOtherRefusalStopsAtOnce:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind, said",
        [
            ("there-is-no-credential", "the publisher holds no credential."),
            ("the-checks-are-not-recorded-as-passed", "does not show them passing."),
            ("the-turn-has-moved-on", "the worker has been replaced."),
            ("the-publisher-could-not-be-reached", "it could not be reached."),
        ],
    )
    async def test_one_attempt_and_the_reason_is_said(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        kind: str,
        said: str,
    ) -> None:
        publisher = _APublisherThatSays([_some_other_refusal(kind, said)])
        deps, deploy, joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )

        outcome = await _press(deps, repo_root)

        assert len(publisher.asked) == 1
        assert len(joins.calls) == 1
        assert outcome.result == "publication-pending"
        assert outcome.status == "GATED"
        assert said in outcome.detail
        assert "picked up where it stopped" in outcome.detail
        assert "promote" not in _legs(deploy)

    @pytest.mark.asyncio
    async def test_no_publisher_configured_at_all(
        self,
        repo_root: Path,  # noqa: F811
        pool: SqliteLifecyclePersistence,  # noqa: F811
    ) -> None:
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {REPO: str(repo_root)}},
                "approval": {"expected_approver": "rich"},
                "merge_executor": {"enabled": True},
                "publication": {
                    "enabled": True,
                    "builds_may_run_inside_the_coordinator": False,
                    # Named, because an unnamed credential file is itself a
                    # refusal (22 September 2026) and this test is about the
                    # OTHER thing being missing: nowhere to send the request.
                    "publisher_credential_file": "/etc/forge-publisher/credential",
                    # …and no publisher_url, which is the point of this test.
                },
            }
        )
        deps, _deploy, _joins, _bus = _deps(config, pool)

        outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        assert "no publisher is configured" in outcome.detail

    @pytest.mark.asyncio
    async def test_a_publisher_that_raises_is_a_refusal_not_a_crash(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        async def blows_up(_config: Any, _request: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("the socket went away")

        deps, _deploy, _joins, _bus = _deps(
            config_with_publication_on, pool, publisher=blows_up
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        assert "ended in an error" in outcome.detail
        assert _record(pool).result == RESULT_PUBLICATION_PENDING


class TestPickingUpASendWhoseAnswerWasLost:
    @pytest.mark.asyncio
    async def test_the_remote_is_read_first_and_it_is_already_there(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """The send landed; the coordinator stopped before its "done" line.

        The next press must not send again. It reads the remote FIRST — before
        it would join anything — and finds the branch contains the joined
        commit, so it marks it published and carries on.
        """
        # Press one: the send really lands on the bare repository, and the
        # publisher's answer is "lost" — the press is told nothing came back.
        landed: dict[str, str] = {}

        def send_for_real(request: dict[str, Any]) -> None:
            landed["j"] = request["j_commit"]
            _git(repo_root, "push", "-q", "origin", f"{request['j_commit']}:main")

        publisher = _APublisherThatSays(
            [
                _some_other_refusal(
                    "the-publisher-could-not-be-reached",
                    "the publisher could not be reached, so nothing is known "
                    "to have been sent.",
                )
            ],
            before_answering=send_for_real,
        )
        deps, _deploy, joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )
        first = await _press(deps, repo_root)
        assert first.result == "publication-pending"

        # Press two: a publisher that would refuse everything, so that a send
        # is impossible. It must never be asked.
        never = _APublisherThatSays(
            [_some_other_refusal("there-is-no-credential", "it holds none.")]
        )
        deps_two, deploy_two, joins_two, _bus_two = _deps(
            config_with_publication_on, pool, publisher=never
        )
        second = await _press(deps_two, repo_root)

        assert second.result == "published-deployment-pending"
        assert never.asked == []
        # NOTHING WAS JOINED AGAIN and nothing was checked again: it was
        # settled by looking at the remote.
        assert joins_two.calls == []
        assert _legs(deploy_two) == []
        record = _record(pool)
        assert record.result == RESULT_PUBLISHED_DEPLOYMENT_PENDING
        assert record.j_commit == landed["j"]
        assert len(joins.calls) == 1

    @pytest.mark.asyncio
    async def test_an_unanswered_about_to_send_whose_send_never_landed(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """The remote does not have it, so this press sends."""
        publisher = _APublisherThatSays(
            [
                _some_other_refusal(
                    "the-publisher-could-not-be-reached", "it was not there."
                )
            ]
        )
        deps, _deploy, _joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )
        await _press(deps, repo_root)

        sends_again = _APublisherThatSays([_published("e" * 40)])
        deps_two, _deploy_two, _joins_two, _bus_two = _deps(
            config_with_publication_on, pool, publisher=sends_again
        )
        second = await _press(deps_two, repo_root)

        assert second.result == "published-deployment-pending"
        assert len(sends_again.asked) == 1

    @pytest.mark.asyncio
    async def test_the_question_is_CONTAINS_and_not_IS(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """Somebody else adds to the branch on top of the send. Still published."""

        def send_for_real(request: dict[str, Any]) -> None:
            _git(repo_root, "push", "-q", "origin", f"{request['j_commit']}:main")

        publisher = _APublisherThatSays(
            [_some_other_refusal("the-publisher-could-not-be-reached", "lost.")],
            before_answering=send_for_real,
        )
        deps, _deploy, _joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )
        await _press(deps, repo_root)
        on_top = _somebody_else_lands_work(repo_root, "on-top")

        never = _APublisherThatSays([_some_other_refusal("x", "never.")])
        deps_two, _deploy_two, _joins_two, _bus_two = _deps(
            config_with_publication_on, pool, publisher=never
        )
        second = await _press(deps_two, repo_root)

        assert second.result == "published-deployment-pending"
        assert never.asked == []
        assert _git(repo_root, "rev-parse", "origin/main") == on_top

    @pytest.mark.asyncio
    async def test_a_published_record_is_not_joined_and_sent_all_over_again(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """A press after a successful one answers the same, and sends nothing."""

        def send_for_real(request: dict[str, Any]) -> None:
            _git(repo_root, "push", "-q", "origin", f"{request['j_commit']}:main")

        publisher = _APublisherThatSays(
            [_published("f" * 40)], before_answering=send_for_real
        )
        deps, _deploy, _joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )
        first = await _press(deps, repo_root)
        assert first.result == "published-deployment-pending"

        again = _APublisherThatSays([_published("f" * 40)])
        deps_two, deploy_two, joins_two, _bus_two = _deps(
            config_with_publication_on, pool, publisher=again
        )
        second = await _press(deps_two, repo_root)

        assert second.result == "published-deployment-pending"
        assert again.asked == []
        assert joins_two.calls == []
        assert _legs(deploy_two) == []
        # AND THE RECORD STILL SAYS WHAT HAPPENED. The second press's idea of
        # where the branch is now — which, for a published build, is the
        # joined commit itself — must not be written over the commit the join
        # was really made onto, or the record stops being a record.
        record = _record(pool)
        parents = _git(
            repo_root, "rev-list", "--parents", "-n", "1", str(record.j_commit)
        ).split()[1:]
        assert parents == [record.g_commit, record.build_tip]


class TestWithPublicationOffNothingIsSentAndTheReasonIsSaid:
    @pytest.mark.asyncio
    async def test_no_setting_turns_it_on(
        self,
        config_with_publication_off: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        publisher = _APublisherThatSays([_published("c" * 40)])
        deps, _deploy, _joins, _bus = _deps(
            config_with_publication_off, pool, publisher=publisher
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        assert outcome.status == "PASSED"
        assert "publication is switched off" in outcome.detail
        assert "no setting turns publication on" in outcome.detail
        assert publisher.asked == []

    @pytest.mark.asyncio
    async def test_the_setting_is_on_but_a_wall_is_down(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """Section G: the check refuses and says WHICH condition failed."""
        publisher = _APublisherThatSays([_published("c" * 40)])
        deps, _deploy, _joins, _bus = _deps(
            config_with_publication_on,
            pool,
            publisher=publisher,
            machine=WhatTheMachineSays(
                a_sandbox_can_write_the_coordinators_settings_file=False,
                a_sandbox_can_see_the_ledger=True,
                a_sandbox_can_reach_the_publisher=False,
                the_credential_file_can_be_read_by_them=False,
                only_the_coordinator_is_on_the_publishers_network=True,
            ),
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        assert "publication is switched off" in outcome.detail
        assert "a sandbox can see the ledger" in outcome.detail
        assert publisher.asked == []

    @pytest.mark.asyncio
    async def test_nobody_has_looked_at_the_machine_at_all(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """Which is where the estate stands today: publication stays off."""
        publisher = _APublisherThatSays([_published("c" * 40)])
        deps, _deploy, _joins, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher, machine=None
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        assert "nobody has looked" in outcome.detail
        assert publisher.asked == []


class TestNothingHalfCheckedIsEverSent:
    @pytest.mark.asyncio
    async def test_a_picked_up_join_whose_build_system_checks_never_ran(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """The press that reuses a join does not re-run them, so it must not send.

        Press one is killed after the join is made but before its checks can
        be reported, which is the shape that leaves a joined commit with only
        one kind of check ever run on it. Press two picks that join up — and
        refuses to send it, saying so.
        """
        from tests.forge.pipeline.test_merge_executor import _DiesWhileMerging

        publisher = _APublisherThatSays([_published("c" * 40)])
        bus = _FakePublisher()
        deploy = _FakeDeploy()
        deps = MergeExecutorDeps(
            config=config_with_publication_on,
            pool=pool,
            pipeline_publisher=bus,
            guardkit_run=_DiesWhileMerging(),
            deploy_dispatcher=deploy,
            publisher=publisher,
            what_the_machine_says=EVERY_WALL_STANDS,
        )
        with pytest.raises(KeyboardInterrupt):
            await _press(deps, repo_root)
        assert publisher.asked == []

        deps_two, deploy_two, joins_two, _bus = _deps(
            config_with_publication_on, pool, publisher=publisher
        )
        second = await _press(deps_two, repo_root)

        assert second.result == "publication-pending"
        assert second.status == "GATED"
        assert "It is NOT yet checked" in second.detail
        assert "nothing was sent to the remote" in second.detail
        # The live check DID run on it; the merge command did not.
        assert joins_two.calls == []
        assert _legs(deploy_two).count("candidate_check") == 1
        assert publisher.asked == []


class TestThePressAndThePublisherReadTheRecordTheSameWay:
    """The reviewer's eighth finding, 22 September 2026.

    The publisher counts a step only when its ``done`` line says it ran on
    exactly this joined commit. The press used to count a line that named no
    commit at all. Two readers of one record, two answers: the press could
    call a join checked and ask for a send, and the publisher could then
    refuse the very record the press had just read. The press now asks for the
    commit too.
    """

    def _forget_which_commit_the_checks_ran_on(
        self, pool: SqliteLifecyclePersistence  # noqa: F811
    ) -> None:
        """Take the commit off the build system's own checks' done line."""
        import json

        row = pool.connection.execute(
            "SELECT lines_json FROM publication_records WHERE build_id = ?",
            (BUILD_ID,),
        ).fetchone()
        lines = json.loads(row[0])
        found = 0
        for line in lines:
            if line.get("kind") == "done" and line.get("step") == "merge-checks":
                detail = line.get("detail") or {}
                detail.pop("ran_on", None)
                detail.pop("j_commit", None)
                line["detail"] = detail
                found += 1
        assert found == 1, "the press did not write the line this test edits"
        pool.connection.execute(
            "UPDATE publication_records SET lines_json = ? WHERE build_id = ?",
            (json.dumps(lines), BUILD_ID),
        )
        pool.connection.commit()

    @pytest.mark.asyncio
    async def test_a_done_line_naming_no_commit_is_not_a_check_on_this_one(
        self,
        config_with_publication_off: ForgeConfig,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        deps, _deploy, _joins, _bus = _deps(config_with_publication_off, pool)
        first = await _press(deps, repo_root)
        assert first.result == "publication-pending"
        assert "checked and ready to publish" in first.detail

        self._forget_which_commit_the_checks_ran_on(pool)

        publisher = _APublisherThatSays([_published("c" * 40)])
        deps_two, _deploy_two, joins_two, _bus_two = _deps(
            config_with_publication_on, pool, publisher=publisher
        )
        second = await _press(deps_two, repo_root)

        # The join is picked up, so the merge command is not run again and the
        # build system's own checks never run on this commit at all.
        assert joins_two.calls == []
        assert second.result == "publication-pending"
        assert "It is NOT yet checked" in second.detail
        # AND NOTHING WAS ASKED OF THE PUBLISHER, which is the point: the
        # press stops where the publisher would have refused it.
        assert publisher.asked == []

    @pytest.mark.asyncio
    async def test_a_done_line_naming_this_commit_still_counts(
        self,
        config_with_publication_off: ForgeConfig,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """The same two presses, with the line left alone: it is checked."""
        deps, _deploy, _joins, _bus = _deps(config_with_publication_off, pool)
        first = await _press(deps, repo_root)
        assert first.result == "publication-pending"

        publisher = _APublisherThatSays([_published("c" * 40)])
        deps_two, _deploy_two, joins_two, _bus_two = _deps(
            config_with_publication_on, pool, publisher=publisher
        )
        second = await _press(deps_two, repo_root)

        assert joins_two.calls == []
        assert second.result == "published-deployment-pending"
        assert len(publisher.asked) == 1


# ---------------------------------------------------------------------------
# The deploy the press makes once the work is published (C, F, I)
# ---------------------------------------------------------------------------


class _ADeployStepThatSays:
    """A stand-in deploy stage whose promote leg answers like a real one.

    It records the ownership it was handed and prints one
    ``<marker>=<identity>`` line, which is exactly the contract a project's own
    deploy step has. ``reports`` overrides what it claims is running, which is
    how "the step deployed something else" is driven.
    """

    def __init__(self, *, reports: str | None = None, marker: str = "DEPLOYED_IDENTITY") -> None:
        self.reports = reports
        self.marker = marker
        self.calls: list[dict[str, Any]] = []
        self.ownership: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls.append(kwargs)
        leg = kwargs.get("leg", "deploy")
        if leg == "candidate_check":
            from tests.forge.pipeline.test_merge_executor import GREEN_GATE

            return SimpleNamespace(
                outcome="complete",
                verdict="pass",
                failed_step=None,
                events=("DeployQueued",),
                detail={"gate_summary": dict(GREEN_GATE), "candidate": "standing"},
            )
        if leg == "candidate_down":
            return SimpleNamespace(
                outcome="complete", verdict=None, detail={"candidate": "torn-down"}
            )
        owns = dict(kwargs.get("deploy_ownership") or {})
        self.ownership.append(owns)
        running = self.reports if self.reports is not None else owns.get("identity")
        return SimpleNamespace(
            outcome="complete",
            verdict="pass",
            deploy_record_ref="docs/state/x.md",
            detail={
                "candidate": "torn-down",
                "deploy_output": (
                    f"handed={owns.get('identity')}\n{self.marker}={running}\n"
                ),
            },
        )


def _a_declaration(*, declared: bool = True) -> Any:
    from forge.pipeline.deployment_identity import IdentityDeclaration

    return IdentityDeclaration(
        setting="DEPLOY_IDENTITY", marker="DEPLOYED_IDENTITY", declared=declared
    )


def _deps_that_can_deploy(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,  # noqa: F811
    *,
    publisher: Any,
    deploy: Any,
    declared: bool = True,
    target: str = "acme/widget-shop::live",
) -> MergeExecutorDeps:
    from forge.pipeline.deployment_lock import DeploymentLockStore

    return MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=_FakePublisher(),
        guardkit_run=_JoinsForReal(),
        deploy_dispatcher=deploy,
        publisher=publisher,
        what_the_machine_says=EVERY_WALL_STANDS,
        deployment_lock=lambda: DeploymentLockStore(pool.connection),
        deployment_target=lambda repo, root: (target, _a_declaration(declared=declared)),
    )


class TestTheProjectSaysHowItWantsTheIdentity:
    """A project that declares nothing is not deployed blind. It is not deployed.

    ``IdentityDeclaration`` has said in its own words since it was written that
    "a project that declared nothing is NOT given a fabricated arrangement:
    the press records that the project declares no identity, and the deploy is
    refused rather than run blind". The press did not read the field, so no
    such refusal existed and the two defaults were used silently. It reads it
    now, and this is the refusal.
    """

    @pytest.mark.asyncio
    async def test_a_project_with_no_identity_block_is_published_and_not_deployed(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        publisher = _APublisherThatSays([_published("c" * 40)])
        deploy = _ADeployStepThatSays()
        deps = _deps_that_can_deploy(
            config_with_publication_on,
            pool,
            publisher=publisher,
            deploy=deploy,
            declared=False,
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "published-deployment-pending"
        assert "does not say how it wants the identity" in outcome.detail
        assert "identity block in deploy/profile.yaml" in outcome.detail
        # NOTHING WAS PROMOTED. The refusal comes before the lock is taken.
        assert [c.get("leg") for c in deploy.calls].count("promote") == 0
        assert _record(pool).result == RESULT_PUBLISHED_DEPLOYMENT_PENDING


class TestTheDeployPutsLiveExactlyWhatWasChecked:
    @pytest.mark.asyncio
    async def test_the_step_reports_the_identity_it_was_handed_and_it_is_running(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """The one path to the third result word, end to end through the press."""
        from forge.pipeline.publication_record import RESULT_MERGED_AND_RUNNING

        publisher = _APublisherThatSays([_published("c" * 40)])
        deploy = _ADeployStepThatSays()
        deps = _deps_that_can_deploy(
            config_with_publication_on, pool, publisher=publisher, deploy=deploy
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "merged-into-the-remote-and-running", outcome.detail
        assert outcome.status == "PASSED"
        assert "reported back the identity it was handed" in outcome.detail
        assert _record(pool).result == RESULT_MERGED_AND_RUNNING
        # THE OWNERSHIP THE EXECUTOR ENFORCES travelled with it: the target,
        # that target's own counter, the build it was granted to, and the name
        # the PROJECT said it wants the identity handed over in.
        owns = deploy.ownership[-1]
        assert owns["target"] == "acme/widget-shop::live"
        assert owns["target_counter"] == 1
        assert owns["identity_setting"] == "DEPLOY_IDENTITY"
        # ...and, on a target nothing has ever run on, the fact the executor
        # needs to tell a first deploy from a lost note.
        assert owns["something_is_running"] is False

    @pytest.mark.asyncio
    async def test_a_second_press_says_something_is_running_there_now(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """Once a deploy is confirmed, R is on the row and the next press says so.

        That one fact is what lets the executor tell "this target has never
        been deployed from here" from "my note for it has been lost", with
        nobody to ask.
        """
        publisher = _APublisherThatSays([_published("c" * 40)])
        deploy = _ADeployStepThatSays()
        deps = _deps_that_can_deploy(
            config_with_publication_on, pool, publisher=publisher, deploy=deploy
        )
        await _press(deps, repo_root)

        from forge.pipeline.deployment_lock import DeploymentLockStore

        row = DeploymentLockStore(pool.connection).read("acme/widget-shop::live")
        assert row.running_identity == deploy.ownership[-1]["identity"]
        assert row.nothing_is_running is False

    @pytest.mark.asyncio
    async def test_a_step_that_reports_a_different_identity_is_a_failed_deploy(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        publisher = _APublisherThatSays([_published("c" * 40)])
        deploy = _ADeployStepThatSays(reports="j-somebodyelse@0000")
        deps = _deps_that_can_deploy(
            config_with_publication_on, pool, publisher=publisher, deploy=deploy
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "merged-deploy-failed"
        assert outcome.status == "FAILED"
        assert "j-somebodyelse@0000" in outcome.detail
        assert "is NOT running" in outcome.detail

    @pytest.mark.asyncio
    async def test_a_step_that_reports_nothing_at_all_is_a_failed_deploy(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """Saying nothing is not the same as saying the wrong thing, and it is said."""
        publisher = _APublisherThatSays([_published("c" * 40)])
        deploy = _ADeployStepThatSays(marker="SOMETHING_ELSE")
        deps = _deps_that_can_deploy(
            config_with_publication_on, pool, publisher=publisher, deploy=deploy
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "merged-deploy-failed"
        assert "reported no identity at all" in outcome.detail
