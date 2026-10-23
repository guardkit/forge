"""The project's decision records from memory reach the planning writers.

23 October 2026. GuardKit's architecture commands save each decision record as
an ``adr`` payload tagged ``architecture`` in the project's memory project.
What these hold down:

* the door searches the run's own, granted memory project once, for exactly
  those records ranked against the request, and records what will be sent;
* the spec writer (first call and rewrite) and the plan writer get the same
  text, before the factory reference, and both receipts say which records;
* over the 16 KiB cap nothing is refused: the lowest-ranked records are left
  out by name, in the text and in the record;
* memory switched off, a name the repository was not granted, a failed search
  and no records all leave the writers' calls exactly as they were, with the
  outcome recorded plainly.

Nothing real is contacted: the memory store is a stand-in behind Forge's own
reader, and the writers are the stand-ins of the project-documents tests.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.fleet_memory.priors import (
    FleetMemoryPriorsConfig,
    FleetMemoryPriorsReader,
    _RetrievalBackend,
)
from forge.config.models import SandboxEntry
from forge.planning import memory_decisions
from forge.planning.factory_deployment_context import load_bundled_document
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning import test_driver_spec_digest_door as door
from tests.forge.planning.test_memory_name_at_the_door import (  # noqa: F401
    _local_remotes,  # the autouse fixture: the stand-in remote is a local folder
)
from tests.forge.planning.test_project_documents_for_the_writers import (  # noqa: F401
    AGENTS,
    DECLARES_NOTHING,
    _chain,
    _drafted,
    _events,
    _Tree,
    chain_store,
)

MEMORY = "widget_shop"


def _adr(identifier: str, score: float, *, decision: str = "Use one database.") -> Any:
    value = {
        "natural_key": f"adr:{MEMORY}:{identifier}",
        "identifier": identifier,
        "title": f"Decision {identifier}",
        "status": "accepted",
        "decision": decision,
        "alternatives": ["two databases"],
        "domain_tags": ["architecture"],
    }
    return SimpleNamespace(value=value, score=score)


class _Store:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> object:
        self.entered += 1
        return object()

    async def __aexit__(self, *_: Any) -> None:
        self.exited += 1


def _reader(search: Any) -> tuple[FleetMemoryPriorsReader, list[dict[str, Any]], _Store]:
    """Forge's own fleet-memory reader over a stand-in store and search."""
    requests: list[dict[str, Any]] = []
    store = _Store()

    class _Request:
        def __init__(self, **kwargs: Any) -> None:
            requests.append(kwargs)

    backend = _RetrievalBackend(
        settings_cls=lambda **kwargs: SimpleNamespace(**kwargs),  # type: ignore[arg-type]
        store_context=lambda _settings: store,
        request_cls=_Request,
        search=search,
    )
    config = FleetMemoryPriorsConfig(enabled=True, pg_dsn="stand-in", embed_url="stand-in")
    return FleetMemoryPriorsReader(config, backend=backend), requests, store


def _returning(items: list[Any]) -> Any:
    async def _search(_request: Any, _store: Any) -> list[Any]:
        return list(items)

    return _search


def _with_memory(h: SimpleNamespace, reader: Any, granted: list[str] | None) -> None:
    deps = h.driver._deps
    deps.memory_reader = reader
    if granted is not None:
        entry = SandboxEntry(
            name="widget-shop", sidecar_url="http://sandbox:1", runner_url="http://sandbox:2",
            memory_projects=granted,
        )
        deps.planning_config = deps.planning_config.model_copy(
            update={"sandboxes": {door.TARGET_REPO: entry}}
        )


def _outcomes(store: SqlitePlanningRunStore) -> list[dict[str, Any]]:
    return [e[memory_decisions.RECEIPT_KEY] for e in _events(store, memory_decisions.STAGE)]


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


def test_over_the_cap_the_lowest_ranked_records_are_left_out_and_named() -> None:
    big = "x" * 7000
    records = [
        {"natural_key": f"adr:{MEMORY}:A{i}", "score": 1.0 - i / 10,
         "value": _adr(f"A{i}", 0.0, decision=big).value}
        for i in range(4)
    ]
    result = memory_decisions.outcome(MEMORY, records, None)

    assert result["outcome"] == "given"
    assert [r["natural_key"] for r in result["records"]] == [
        f"adr:{MEMORY}:A0", f"adr:{MEMORY}:A1"]
    assert [d["natural_key"] for d in result["dropped"]] == [
        f"adr:{MEMORY}:A2", f"adr:{MEMORY}:A3"]
    kept_bytes = sum(r["bytes"] for r in result["records"])
    assert kept_bytes <= memory_decisions.BUDGET_BYTES == 16 * 1024
    assert "2 lower-ranked record(s) were left out" in result["detail"]
    assert f"adr:{MEMORY}:A3" in result["text"]  # the text says which, by name
    assert result["text"].count("### Decision") == 2
    assert result["search_limit"] == 10
    assert "at most the 10 best matches for the request" in result["detail"]
    assert "at most the 10 best matches for the request" in result["text"]


def test_under_the_cap_the_record_still_says_the_search_returns_ten_at_most() -> None:
    """Fleet-memory returns at most ten ranked matches, so a project with many
    records never trips the cap; the record must not read as if it held them all."""
    records = [{"natural_key": f"adr:{MEMORY}:A{i}", "score": 0.5,
                "value": _adr(f"A{i}", 0.5).value} for i in range(10)]
    result = memory_decisions.outcome(MEMORY, records, None)
    assert result["dropped"] == [] and result["search_limit"] == 10
    assert "at most the 10 best matches for the request" in result["detail"]
    assert "left out" not in result["detail"]
    empty = memory_decisions.outcome(MEMORY, [], None)
    assert empty["search_limit"] == 10 and "at most 10 best matches" in empty["detail"]


def test_a_record_renders_its_filled_fields_only() -> None:
    text = memory_decisions.render_record(_adr("ADR_1", 0.5).value)
    assert text == (
        "### Decision ADR_1\nStatus: accepted\nDecision: Use one database.\n"
        "Alternatives considered: two databases\n"
    )


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reader_searches_only_a_granted_name_for_the_typed_records() -> None:
    reader, requests, store = _reader(_returning([_adr("ADR_1", 0.9)]))

    refused, why = await reader.read_records(
        memory_project=MEMORY, granted_memory_projects=["other_project"],
        payload_types=["adr"], domain_tags=["architecture"], query="q",
        token_budget=10, purpose="test",
    )
    assert refused == [] and "not one its repository was granted" in str(why)
    assert requests == [] and store.entered == 0

    records, why = await reader.read_records(
        memory_project=MEMORY, granted_memory_projects=[MEMORY],
        payload_types=["adr"], domain_tags=["architecture"], query="add a thing",
        token_budget=10, purpose="test",
    )
    assert why is None
    assert [r["natural_key"] for r in records] == [f"adr:{MEMORY}:ADR_1"]
    assert requests[0]["project"] == MEMORY
    assert requests[0]["payload_types"] == ["adr"]
    assert requests[0]["domain_tags"] == ["architecture"]
    assert requests[0]["query"] == "add a thing"


@pytest.mark.asyncio
async def test_a_failed_search_is_an_answer_not_an_exception() -> None:
    async def _boom(_request: Any, _store: Any) -> list[Any]:
        raise ConnectionError("store unreachable")

    reader, _requests, _store = _reader(_boom)
    records, why = await reader.read_records(
        memory_project=MEMORY, granted_memory_projects=[MEMORY],
        payload_types=["adr"], domain_tags=["architecture"], query=None,
        token_budget=10, purpose="test",
    )
    assert records == [] and "ConnectionError" in str(why)


@pytest.mark.asyncio
async def test_cancellation_resets_the_store_and_is_raised_not_recorded() -> None:
    """A coordinator shutting down mid-search must not leave the run recorded
    as 'timed out': the store is reset and the cancellation goes on up."""
    async def _cancelled(_request: Any, _store: Any) -> list[Any]:
        raise asyncio.CancelledError

    reader, _requests, store = _reader(_cancelled)
    with pytest.raises(asyncio.CancelledError):
        await reader.read_records(
            memory_project=MEMORY, granted_memory_projects=[MEMORY],
            payload_types=["adr"], domain_tags=["architecture"], query=None,
            token_budget=10, purpose="test",
        )
    assert reader._store is None and reader._store_cm is None
    assert store.exited == 1


# ---------------------------------------------------------------------------
# Through the real chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_writers_get_the_records_and_both_receipts_say_which(
    chain_store: SqlitePlanningRunStore,
) -> None:
    tree = _Tree({".guardkit/config.yaml": DECLARES_NOTHING, "AGENTS.md": AGENTS})
    door._queue(chain_store)
    h = _chain(
        chain_store, tree,
        answers=[door._answer("reject", notes="tighten the second example"),
                 door._answer("approve", attempt=1)],
    )
    reader, requests, _store = _reader(_returning([_adr("ADR_1", 0.9), _adr("ADR_2", 0.4)]))
    _with_memory(h, reader, [MEMORY])

    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    (result,) = _outcomes(chain_store)  # searched once, at the door
    assert len(requests) == 1
    assert result["outcome"] == "given" and result["dropped"] == []
    expected = [result["text"], load_bundled_document().text]
    assert "### Decision ADR_1" in result["text"] and "### Decision ADR_2" in result["text"]
    first, rewrite = h.calls["spec"]
    assert first["context"] == expected and rewrite["context"] == expected
    assert [call["context"] for call in h.calls["plan"]] == [expected]

    receipt = memory_decisions.receipt(result)
    assert receipt["status"] == "sent" and "text" not in receipt
    assert [r["natural_key"] for r in receipt["records"]] == [
        f"adr:{MEMORY}:ADR_1", f"adr:{MEMORY}:ADR_2"]
    drafts = _drafted(chain_store)
    assert len(drafts) == 2
    assert all(d["spec_draft"]["memory_decisions"] == receipt for d in drafts)
    (plan_row,) = _events(chain_store, "feature-plan")
    assert plan_row["memory_decisions"] == receipt
    labels = [event["stage_label"] for event in chain_store.list_events(door.CID)]
    assert labels.index(memory_decisions.STAGE) < labels.index("product_owner")


def _boom_search() -> Any:
    async def _search(_request: Any, _store: Any) -> list[Any]:
        raise TimeoutError

    return _search


@pytest.mark.parametrize(
    ("reader_search", "granted", "outcome", "said"),
    [
        (None, None, "not-searched", "memory is not switched on"),
        ("records", [], "not-searched", "granted no memory"),
        ("records", ["other_project"], "not-searched", "not one its repository was granted"),
        ("failing", [MEMORY], "not-searched", "timed out"),
        ("empty", [MEMORY], "none-found", "found no adr records tagged architecture"),
    ],
    ids=["memory-off", "no-grant", "other-name", "search-fails", "no-records"],
)
@pytest.mark.asyncio
async def test_without_records_planning_is_unchanged_and_says_why(
    chain_store: SqlitePlanningRunStore,
    reader_search: str | None,
    granted: list[str] | None,
    outcome: str,
    said: str,
) -> None:
    tree = _Tree({".guardkit/config.yaml": DECLARES_NOTHING, "AGENTS.md": AGENTS})
    door._queue(chain_store)
    h = _chain(chain_store, tree, answers=[door._answer("approve")])
    reader = None
    if reader_search is not None:
        search = {"records": _returning([_adr("ADR_1", 0.9)]), "empty": _returning([]),
                  "failing": _boom_search()}[reader_search]
        reader, _requests, _store = _reader(search)
    _with_memory(h, reader, granted)

    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert all(call["context"] == [load_bundled_document().text]
               for call in h.calls["spec"] + h.calls["plan"])
    (result,) = _outcomes(chain_store)
    assert result["outcome"] == outcome and result["text"] is None
    assert said in result["detail"]
    (plan_row,) = _events(chain_store, "feature-plan")
    assert plan_row["memory_decisions"]["status"] == "none sent"
    assert plan_row["memory_decisions"]["detail"] == result["detail"]


@pytest.mark.asyncio
async def test_a_run_whose_door_predates_the_step_sends_and_records_nothing_new(
    chain_store: SqlitePlanningRunStore,
) -> None:
    """A run already past the door is not searched later: its writers keep the
    context they started with and its receipts carry no decision-record key."""
    tree = _Tree({".guardkit/config.yaml": DECLARES_NOTHING, "AGENTS.md": AGENTS})
    door._queue(chain_store)
    h = _chain(chain_store, tree, answers=[door._answer("approve")])
    reader, requests, _store = _reader(_returning([_adr("ADR_1", 0.9)]))
    _with_memory(h, reader, [MEMORY])
    recall = h.driver._recall_memory_decisions

    async def _skipped(*_args: Any, **_kwargs: Any) -> None:
        return None

    h.driver._recall_memory_decisions = _skipped  # the door as it was before
    await h.driver.drive(door.CID)
    h.driver._recall_memory_decisions = recall

    assert requests == [] and _outcomes(chain_store) == []
    assert all(call["context"] == [load_bundled_document().text]
               for call in h.calls["spec"] + h.calls["plan"])
    (plan_row,) = _events(chain_store, "feature-plan")
    assert "memory_decisions" not in plan_row
    assert json.dumps(_drafted(chain_store)).count("memory_decisions") == 0
