"""Concurrent builds on the build consumer, checked against a real broker.

Every test here talks to a throwaway ``nats-server`` started in its own
container on a random local port (label ``forge.test=concurrent-builds``)
and removed afterwards. Nothing connects to any other NATS server. When
Docker or the image is not available the tests are skipped, not failed.

What the checks prove:

* ``TestBuildLimitHolds`` — with the limit set to 1, 2, 4 and 8 the broker
  hands out exactly that many build messages and holds the rest back;
  acknowledging one lets exactly one more through.
* ``TestExistingDurableFollowsTheSetting`` — a durable that already exists
  is changed to the configured limit when the daemon attaches (1 to 8, then
  8 down to 2), because binding alone leaves the old value in place.
* ``TestRestartAndRedelivery`` — after a Forge restart, held builds stay held
  (not handed out twice) until ``ack_wait`` passes, then come back as
  redeliveries of the same messages; the waiting build is still held back.
* ``TestDuplicateAndLateCompletion`` — a build that completes twice, or
  completes late after its message was redelivered, releases its place once
  and its message is never handed out again.
* ``TestApprovalWaitDoesNotBlockOthers`` — the real daemon loop with limit 2:
  build A's dispatch never returns (an unanswered approval card), build B is
  still fetched and dispatched; on shutdown both are cancelled and neither
  is acknowledged.
* ``TestConsumerHealthWithSeveralOutstanding`` — the ack-slot check with
  several outstanding builds: a real held build is never reported as a
  phantom, and only when every outstanding message is gone does the boot
  cure run.

The estate bus is built ``FROM nats:2.11-alpine``. On that server (2.11.17
when this was written) deleting or purging a held message also clears its
outstanding acknowledgement, so a phantom cannot be made there by deleting
messages. ``nats:2.10`` still leaves the acknowledgement outstanding after
``delete_msg``, which is the phantom the boot cure exists for, so the
phantom checks use that image.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

nats = pytest.importorskip("nats")

from nats.js.api import StreamConfig  # noqa: E402
from nats.js.errors import NotFoundError  # noqa: E402

from forge.adapters.nats.consumer_health import inspect_ack_slot  # noqa: E402
from forge.cli import _serve_daemon  # noqa: E402
from forge.cli._serve_config import ServeConfig  # noqa: E402
from forge.cli._serve_daemon import (  # noqa: E402
    PIPELINE_STREAM_NAME,
    _attach_consumer,
    run_daemon,
)
from forge.cli._serve_state import SubscriptionState  # noqa: E402
from forge.pipeline.build_ack_handle import make_msg_ack_handle  # noqa: E402

pytestmark = pytest.mark.integration

ESTATE_IMAGE = "nats:2.11-alpine"
PHANTOM_IMAGE = "nats:2.10"
TEST_LABEL = "forge.test=concurrent-builds"
DURABLE = "forge-serve"


# ---------------------------------------------------------------------------
# Throwaway broker
# ---------------------------------------------------------------------------


def _docker_ready(image: str) -> str | None:
    """Return a reason to skip, or ``None`` when Docker and ``image`` are usable."""
    if shutil.which("docker") is None:
        return "docker is not installed"
    probe = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return f"docker image {image} is not available locally"
    return None


@dataclass
class _Broker:
    container_id: str
    url: str
    image: str


async def _connect(url: str) -> Any:
    return await nats.connect(
        servers=[url],
        connect_timeout=2,
        allow_reconnect=False,
        max_reconnect_attempts=0,
    )


def _start_broker(image: str) -> Iterator[_Broker]:
    reason = _docker_ready(image)
    if reason is not None:
        pytest.skip(reason)
    started = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--label", TEST_LABEL,
            "-p", "127.0.0.1::4222",
            image, "-js",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start {image}: {started.stderr.strip()}")
    container_id = started.stdout.strip()
    try:
        port_line = subprocess.run(
            ["docker", "port", container_id, "4222/tcp"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()[0]
        port = port_line.rsplit(":", 1)[1]
        url = f"nats://127.0.0.1:{port}"

        async def _wait_ready() -> None:
            deadline = time.monotonic() + 15
            while True:
                try:
                    nc = await _connect(url)
                except Exception:  # noqa: BLE001 — server still starting
                    if time.monotonic() > deadline:
                        raise
                    await asyncio.sleep(0.2)
                    continue
                await nc.close()
                return

        asyncio.run(_wait_ready())
        yield _Broker(container_id=container_id, url=url, image=image)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            check=False,
        )


@pytest.fixture(scope="module")
def estate_broker() -> Iterator[_Broker]:
    yield from _start_broker(ESTATE_IMAGE)


@pytest.fixture(scope="module")
def phantom_broker() -> Iterator[_Broker]:
    yield from _start_broker(PHANTOM_IMAGE)


async def _fresh_stream(nc: Any) -> Any:
    """Recreate the ``PIPELINE`` stream empty and return a JetStream context."""
    js = nc.jetstream()
    try:
        await js.delete_stream(PIPELINE_STREAM_NAME)
    except NotFoundError:
        pass
    await js.add_stream(
        StreamConfig(name=PIPELINE_STREAM_NAME, subjects=["pipeline.>"])
    )
    return js


async def _publish_builds(js: Any, count: int, *, prefix: str = "F") -> list[int]:
    """Publish ``count`` build-queued messages with an unrelated one between each.

    The unrelated messages mimic the real stream, which also carries the
    outbound lifecycle events, so sequence numbers of builds are not
    contiguous. Returns the stream sequences of the build messages.
    """
    seqs: list[int] = []
    for i in range(count):
        ack = await js.publish(f"pipeline.build-queued.{prefix}{i}", b"{}")
        seqs.append(ack.seq)
        await js.publish(f"pipeline.build-started.{prefix}{i}", b"{}")
    return seqs


async def _drain(sub: Any, *, timeout: float = 0.5, cap: int = 64) -> list[Any]:
    """Fetch one message at a time until the broker hands out no more."""
    got: list[Any] = []
    while len(got) < cap:
        try:
            msgs = await sub.fetch(1, timeout=timeout)
        except asyncio.TimeoutError:
            break
        got.extend(msgs)
    return got


# ---------------------------------------------------------------------------
# Server version (recorded for the review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estate_image_server_version_is_read_from_the_server(
    estate_broker: _Broker,
) -> None:
    nc = await _connect(estate_broker.url)
    try:
        version = nc._server_info.get("version", "")
        print(f"isolated nats-server version: {version}")
        assert version.startswith("2.11."), version
    finally:
        await nc.close()


# ---------------------------------------------------------------------------
# The limit holds
# ---------------------------------------------------------------------------


class TestBuildLimitHolds:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [1, 2, 4, 8])
    async def test_exactly_n_held_and_one_ack_releases_one(
        self, estate_broker: _Broker, limit: int
    ) -> None:
        nc = await _connect(estate_broker.url)
        try:
            js = await _fresh_stream(nc)
            await _publish_builds(js, 2 * limit)
            sub = await _attach_consumer(nc, DURABLE, max_ack_pending=limit)

            held = await _drain(sub)
            assert len(held) == limit, "the broker must hand out exactly N builds"
            info = await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.config.max_ack_pending == limit
            assert info.num_ack_pending == limit
            assert info.num_pending == limit, "build N+1 onwards must wait"

            await held[0].ack()
            released = await _drain(sub)
            assert len(released) == 1, "one acknowledgement releases exactly one"
            info = await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.num_ack_pending == limit
            assert info.num_pending == limit - 1
        finally:
            await nc.close()


class TestExistingDurableFollowsTheSetting:
    @pytest.mark.asyncio
    async def test_existing_durable_is_raised_then_lowered(
        self, estate_broker: _Broker
    ) -> None:
        nc = await _connect(estate_broker.url)
        try:
            js = await _fresh_stream(nc)
            await _publish_builds(js, 16)

            first = await _attach_consumer(nc, DURABLE, max_ack_pending=1)
            info = await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.config.max_ack_pending == 1
            await first.unsubscribe()

            # Upshift 1 -> 8 on the SAME durable.
            raised = await _attach_consumer(nc, DURABLE, max_ack_pending=8)
            info = await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.config.max_ack_pending == 8
            held = await _drain(raised)
            assert len(held) == 8
            await raised.unsubscribe()

            # Downshift 8 -> 2 with eight still outstanding: nothing new is
            # handed out until fewer than two are outstanding.
            lowered = await _attach_consumer(nc, DURABLE, max_ack_pending=2)
            info = await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.config.max_ack_pending == 2
            assert info.num_ack_pending == 8
            for msg in held[:6]:
                await msg.ack()
            assert await _drain(lowered) == [], "two still outstanding: none new"
            await held[6].ack()
            assert len(await _drain(lowered)) == 1, "below two: exactly one more"
        finally:
            await nc.close()


class TestRestartAndRedelivery:
    @pytest.mark.asyncio
    async def test_held_builds_survive_a_restart_and_redeliver_after_ack_wait(
        self, estate_broker: _Broker
    ) -> None:
        ack_wait = 2.0  # test-only; production keeps ACK_WAIT_SECONDS
        nc = await _connect(estate_broker.url)
        js = await _fresh_stream(nc)
        await _publish_builds(js, 3)
        sub = await _attach_consumer(
            nc, DURABLE, max_ack_pending=2, ack_wait_seconds=ack_wait
        )
        held = await _drain(sub, timeout=0.3)
        held_seqs = sorted(m.metadata.sequence.stream for m in held)
        assert len(held_seqs) == 2
        await nc.close()  # Forge "restarts" with both builds unacknowledged.

        nc2 = await _connect(estate_broker.url)
        try:
            js2 = nc2.jetstream()
            sub2 = await _attach_consumer(
                nc2, DURABLE, max_ack_pending=2, ack_wait_seconds=ack_wait
            )
            # Straight after the restart nothing new is handed out: the two
            # places are still held and the third build waits.
            info = await js2.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.num_ack_pending == 2
            assert info.num_pending == 1
            assert await _drain(sub2, timeout=0.3) == []

            await asyncio.sleep(ack_wait + 0.5)
            redelivered = await _drain(sub2, timeout=0.5)
            assert sorted(m.metadata.sequence.stream for m in redelivered) == held_seqs
            assert all(m.metadata.num_delivered >= 2 for m in redelivered)
            info = await js2.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.num_pending == 1, "the third build is still waiting"
        finally:
            await nc2.close()


class TestDuplicateAndLateCompletion:
    @pytest.mark.asyncio
    async def test_one_release_per_message(self, estate_broker: _Broker) -> None:
        ack_wait = 1.0  # test-only
        nc = await _connect(estate_broker.url)
        try:
            js = await _fresh_stream(nc)
            seqs = await _publish_builds(js, 2)
            sub = await _attach_consumer(
                nc, DURABLE, max_ack_pending=1, ack_wait_seconds=ack_wait
            )
            first = (await _drain(sub, timeout=0.3))[0]
            first_handle = make_msg_ack_handle(first)

            # The build outlives ack_wait: its message comes back.
            await asyncio.sleep(ack_wait + 0.5)
            again = (await _drain(sub, timeout=0.5))[0]
            assert again.metadata.sequence.stream == seqs[0]
            second_handle = make_msg_ack_handle(again)

            # Late completion from the first delivery, then a duplicate of
            # it, then the redelivered copy completes too.
            await first_handle.ack()
            await first_handle.ack()  # idempotent: no second wire ack
            await second_handle.ack()

            rest = await _drain(sub, timeout=0.5)
            assert [m.metadata.sequence.stream for m in rest] == [seqs[1]]
            await rest[0].ack()
            await asyncio.sleep(ack_wait + 0.5)
            assert await _drain(sub, timeout=0.5) == [], "nothing handed out twice"
            info = await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.num_ack_pending == 0
        finally:
            await nc.close()


# ---------------------------------------------------------------------------
# R1: an unanswered approval card must not stop other builds
# ---------------------------------------------------------------------------


class TestApprovalWaitDoesNotBlockOthers:
    @pytest.mark.asyncio
    async def test_build_b_dispatches_while_a_waits_forever(
        self, estate_broker: _Broker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setup = await _connect(estate_broker.url)
        js = await _fresh_stream(setup)
        await _publish_builds(js, 2)

        started: dict[str, asyncio.Event] = {
            "pipeline.build-queued.F0": asyncio.Event(),
            "pipeline.build-queued.F1": asyncio.Event(),
        }
        cancelled: list[str] = []

        async def _dispatch_waits_for_approval(msg: Any) -> None:
            # Stand-in for the pre-build approval card nobody answers: the
            # dispatch never returns on its own and never acknowledges.
            started[msg.subject].set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(msg.subject)
                raise

        monkeypatch.setattr(
            _serve_daemon, "dispatch_payload", _dispatch_waits_for_approval
        )
        client = await _connect(estate_broker.url)
        config = ServeConfig(nats_url=estate_broker.url, max_concurrent_builds=2)
        state = SubscriptionState()
        daemon = asyncio.create_task(run_daemon(config, state, client=client))
        try:
            await asyncio.wait_for(
                started["pipeline.build-queued.F0"].wait(), timeout=5
            )
            await asyncio.wait_for(
                started["pipeline.build-queued.F1"].wait(), timeout=5
            )
        finally:
            daemon.cancel()
            try:
                await asyncio.wait_for(daemon, timeout=10)
            except asyncio.CancelledError:
                pass

        try:
            assert sorted(cancelled) == sorted(started), (
                "shutdown must cancel every in-flight dispatch"
            )
            info = await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.num_ack_pending == 2, "neither build was acknowledged"
        finally:
            await setup.close()


# ---------------------------------------------------------------------------
# Consumer health with several outstanding builds
# ---------------------------------------------------------------------------


async def _hold(js: Any, nc: Any, count: int, limit: int) -> list[int]:
    seqs = await _publish_builds(js, count)
    sub = await _attach_consumer(nc, DURABLE, max_ack_pending=limit)
    held = await _drain(sub, timeout=0.3)
    assert len(held) == count
    await sub.unsubscribe()
    return seqs


class TestConsumerHealthWithSeveralOutstanding:
    @pytest.mark.asyncio
    async def test_estate_server_clears_removed_holds_itself(
        self, estate_broker: _Broker
    ) -> None:
        nc = await _connect(estate_broker.url)
        try:
            js = await _fresh_stream(nc)
            seqs = await _hold(js, nc, 2, limit=2)

            await js.delete_msg(PIPELINE_STREAM_NAME, seqs[1])
            report = await inspect_ack_slot(js, PIPELINE_STREAM_NAME, DURABLE)
            assert report.status == "held"
            assert report.num_ack_pending == 1

            await js.delete_msg(PIPELINE_STREAM_NAME, seqs[0])
            report = await inspect_ack_slot(js, PIPELINE_STREAM_NAME, DURABLE)
            assert report.status == "healthy"
        finally:
            await nc.close()

    @pytest.mark.asyncio
    async def test_one_gone_of_two_is_held_and_never_cured(
        self, phantom_broker: _Broker
    ) -> None:
        from forge.cli.serve import _ack_slot_boot_check

        nc = await _connect(phantom_broker.url)
        try:
            js = await _fresh_stream(nc)
            seqs = await _hold(js, nc, 2, limit=2)

            # Remove the LAST delivered build. The first is still a real,
            # held build; the old single-slot check probed only the last
            # delivered sequence and would have called this a phantom.
            await js.delete_msg(PIPELINE_STREAM_NAME, seqs[1])
            info = await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.num_ack_pending == 2  # this server keeps the stale hold

            report = await inspect_ack_slot(js, PIPELINE_STREAM_NAME, DURABLE)
            assert report.status == "held"
            assert report.pending_seq == seqs[0]

            state = SubscriptionState()
            await _ack_slot_boot_check(
                nc, ServeConfig(max_concurrent_builds=2), state
            )
            # No cure: the durable still exists with its held build.
            info = await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
            assert info.num_ack_pending == 2
        finally:
            await nc.close()

    @pytest.mark.asyncio
    async def test_all_gone_is_phantom_and_boot_cure_runs(
        self, phantom_broker: _Broker
    ) -> None:
        from forge.cli.serve import _ack_slot_boot_check

        nc = await _connect(phantom_broker.url)
        try:
            js = await _fresh_stream(nc)
            seqs = await _hold(js, nc, 2, limit=2)
            for seq in seqs:
                await js.delete_msg(PIPELINE_STREAM_NAME, seq)

            report = await inspect_ack_slot(js, PIPELINE_STREAM_NAME, DURABLE)
            assert report.status == "phantom"

            state = SubscriptionState()
            await _ack_slot_boot_check(
                nc, ServeConfig(max_concurrent_builds=2), state
            )
            with pytest.raises(NotFoundError):
                await js.consumer_info(PIPELINE_STREAM_NAME, DURABLE)
        finally:
            await nc.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("remove", [False, True])
    async def test_one_outstanding_behaves_as_before(
        self, phantom_broker: _Broker, remove: bool
    ) -> None:
        nc = await _connect(phantom_broker.url)
        try:
            js = await _fresh_stream(nc)
            seqs = await _hold(js, nc, 1, limit=1)
            if remove:
                await js.delete_msg(PIPELINE_STREAM_NAME, seqs[0])

            report = await inspect_ack_slot(js, PIPELINE_STREAM_NAME, DURABLE)
            assert report.status == ("phantom" if remove else "held")
            assert report.pending_seq == seqs[0]
        finally:
            await nc.close()

