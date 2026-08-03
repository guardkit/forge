"""Unit tests for :mod:`forge.adapters.fleet_memory.priors`.

The unit tier runs WITHOUT ``fleet_memory`` installed (the forge venv
does not carry the ``memory`` extra): the reader's lazy import seam
(``_load_backend``) is monkeypatched with stub surfaces, which is
exactly the degradation boundary production relies on.

One ``Test*`` class per behaviour:

* ``TestFactoryTruthTable`` — the four loud compositions (OFF / DSN-trap
  refusal / import-failure DEGRADED / ON) and the never-raises contract.
* ``TestPriorMapping`` — per-item ``fm_search`` results map to
  :class:`PriorReference` (natural_key verbatim, stamped group_id,
  clamped relevance, bounded + redacted summary, cap 5).
* ``TestReadResilience`` — timeout resets the store handle, init
  failure retries, empty results are NOT an error.
* ``TestFirstReadRace`` — the asyncio.Lock collapses concurrent first
  reads into a single store open.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.fleet_memory.priors import (
    PRIORS_GROUP_ID,
    FleetMemoryPriorsConfig,
    FleetMemoryPriorsReader,
    _RetrievalBackend,
    build_priors_reader_from_env,
    load_priors_config_from_env,
)
from forge.gating.degraded import EmptyPriorsReader
from forge.gating.models import PriorReference
from forge.gating.wrappers import PriorsReader

_LOGGER_NAME = "forge.adapters.fleet_memory.priors"

_ENV_VARS = (
    "FLEET_MEMORY_ENABLED",
    "FLEET_MEMORY_PG_DSN",
    "FLEET_MEMORY_EMBED_URL",
    "FLEET_MEMORY_EMBED_MODEL",
    "FLEET_MEMORY_EMBED_DIMS",
    "FORGE_MEMORY_PROJECT",
    "FORGE_MEMORY_READ_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _config(**overrides: Any) -> FleetMemoryPriorsConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "pg_dsn": "postgresql://forge:secret@fleet-db:5432/memory",
        "embed_url": "http://embed-host:9000/v1",
        "read_timeout_s": 5.0,
    }
    defaults.update(overrides)
    return FleetMemoryPriorsConfig(**defaults)


def _item(
    natural_key: str = "build_outcome:guardkit:TASK_0001",
    *,
    score: float | None = 0.5,
    lessons: Any = None,
    content: Any = None,
) -> SimpleNamespace:
    value: dict[str, Any] = {"natural_key": natural_key}
    if lessons is not None:
        value["lessons"] = lessons
    if content is not None:
        value["content"] = content
    return SimpleNamespace(value=value, score=score)


class _StubStoreContext:
    """Async context manager standing in for ``async_store_context``."""

    def __init__(
        self,
        log: list[str],
        *,
        store: Any = None,
        enter_exc: Exception | None = None,
        enter_delay: float = 0.0,
    ) -> None:
        self._log = log
        self._store = store if store is not None else object()
        self._enter_exc = enter_exc
        self._enter_delay = enter_delay

    async def __aenter__(self) -> Any:
        if self._enter_delay:
            await asyncio.sleep(self._enter_delay)
        if self._enter_exc is not None:
            raise self._enter_exc
        self._log.append("enter")
        return self._store

    async def __aexit__(self, *_exc_info: Any) -> bool:
        self._log.append("exit")
        return False


class _StubRequest:
    """Records the kwargs the reader builds a SearchRequest with."""

    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        type(self).last_kwargs = kwargs


def _make_backend(
    *,
    search: Any,
    store_log: list[str] | None = None,
    settings_calls: list[dict[str, Any]] | None = None,
    enter_exc: Exception | None = None,
    enter_delay: float = 0.0,
) -> tuple[_RetrievalBackend, list[str], list[dict[str, Any]]]:
    log: list[str] = store_log if store_log is not None else []
    calls: list[dict[str, Any]] = (
        settings_calls if settings_calls is not None else []
    )

    def settings_cls(**kwargs: Any) -> SimpleNamespace:
        calls.append(dict(kwargs))
        return SimpleNamespace(
            **{k: v for k, v in kwargs.items() if not k.startswith("_")}
        )

    def store_context(_settings: Any) -> _StubStoreContext:
        log.append("open")
        return _StubStoreContext(
            log, enter_exc=enter_exc, enter_delay=enter_delay
        )

    backend = _RetrievalBackend(
        settings_cls=settings_cls,  # type: ignore[arg-type]
        store_context=store_context,
        request_cls=_StubRequest,
        search=search,
    )
    return backend, log, calls


async def _search_returning(items: list[Any]) -> Any:
    async def _search(_request: Any, _store: Any) -> list[Any]:
        return list(items)

    return _search


class TestFactoryTruthTable:
    """The factory composes loudly and never raises."""

    def test_off_returns_empty_reader_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            reader = build_priors_reader_from_env()
        assert isinstance(reader, EmptyPriorsReader)
        assert any(
            "memory: OFF" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_dsn_trap_forces_off_with_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("FLEET_MEMORY_ENABLED", "true")
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            config = load_priors_config_from_env()
            reader = build_priors_reader_from_env()
        # Refused outright — never a localhost fallback.
        assert config.enabled is False
        assert config.pg_dsn == ""
        assert isinstance(reader, EmptyPriorsReader)
        assert any(
            "FLEET_MEMORY_PG_DSN" in r.message and r.levelno == logging.ERROR
            for r in caplog.records
        )

    def test_embed_url_trap_forces_off_with_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("FLEET_MEMORY_ENABLED", "true")
        monkeypatch.setenv("FLEET_MEMORY_PG_DSN", "postgresql://x@nas:5433/m")
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            config = load_priors_config_from_env()
            reader = build_priors_reader_from_env()
        # Refused at compose — never an ON boot line followed by per-read
        # Settings failures (the trap named once, mirroring the DSN trap).
        assert config.enabled is False
        assert isinstance(reader, EmptyPriorsReader)
        assert any(
            "FLEET_MEMORY_EMBED_URL" in r.message and r.levelno == logging.ERROR
            for r in caplog.records
        )

    def test_import_failure_degrades_with_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("FLEET_MEMORY_ENABLED", "true")
        monkeypatch.setenv("FLEET_MEMORY_PG_DSN", "postgresql://f@h/db")
        monkeypatch.setenv("FLEET_MEMORY_EMBED_URL", "http://localhost:9000/v1")

        def _boom() -> Any:
            raise ImportError("No module named 'fleet_memory'")

        from forge.adapters.fleet_memory import priors as priors_module

        monkeypatch.setattr(priors_module, "_load_backend", _boom)
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            reader = build_priors_reader_from_env()
        assert isinstance(reader, EmptyPriorsReader)
        assert any(
            "memory: DEGRADED" in r.message and r.levelno == logging.ERROR
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_on_returns_fleet_reader_with_info_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("FLEET_MEMORY_ENABLED", "true")
        monkeypatch.setenv("FLEET_MEMORY_PG_DSN", "postgresql://f@h/db")
        monkeypatch.setenv("FLEET_MEMORY_EMBED_URL", "http://localhost:9000/v1")
        monkeypatch.setenv("FORGE_MEMORY_PROJECT", "guardkit")
        backend, _log, _calls = _make_backend(
            search=await _search_returning([])
        )
        from forge.adapters.fleet_memory import priors as priors_module

        monkeypatch.setattr(priors_module, "_load_backend", lambda: backend)
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            reader = build_priors_reader_from_env()
        assert isinstance(reader, FleetMemoryPriorsReader)
        assert any(
            "memory: ON (project=guardkit)" in r.message
            and r.levelno == logging.INFO
            for r in caplog.records
        )

    @pytest.mark.parametrize(
        "env",
        [
            {"FLEET_MEMORY_ENABLED": "banana"},
            {"FLEET_MEMORY_ENABLED": "TRUE", "FLEET_MEMORY_PG_DSN": "   "},
            {
                "FLEET_MEMORY_ENABLED": "true",
                "FLEET_MEMORY_PG_DSN": "postgresql://f@h/db",
                "FLEET_MEMORY_EMBED_DIMS": "not-an-int",
                "FORGE_MEMORY_READ_TIMEOUT_S": "soon",
            },
            {
                "FLEET_MEMORY_ENABLED": "true",
                "FLEET_MEMORY_PG_DSN": "postgresql://f@h/db",
                "FLEET_MEMORY_EMBED_URL": "",
                "FORGE_MEMORY_PROJECT": "",
            },
        ],
    )
    def test_factory_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
    ) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        reader = build_priors_reader_from_env()
        assert isinstance(reader, PriorsReader)

    def test_garbage_numeric_env_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FLEET_MEMORY_EMBED_DIMS", "wide")
        monkeypatch.setenv("FORGE_MEMORY_READ_TIMEOUT_S", "soon")
        config = load_priors_config_from_env()
        assert config.embed_dims == 1024
        assert config.read_timeout_s == 10.0


class TestPriorMapping:
    """fm_search items map per-item to PriorReference."""

    @pytest.mark.asyncio
    async def test_entity_id_is_natural_key_verbatim_and_group_stamped(
        self,
    ) -> None:
        backend, _log, _calls = _make_backend(
            search=await _search_returning(
                [
                    _item(
                        "build_outcome:guardkit:TASK_MEM08_012",
                        score=0.42,
                        lessons="lock the python floor",
                    )
                ]
            )
        )
        reader = FleetMemoryPriorsReader(_config(), backend=backend)

        priors = await reader.read_priors(
            target_kind="subagent",
            target_identifier="autobuild_runner",
            stage_label="autobuild",
            build_id="build-map-1",
        )

        assert len(priors) == 1
        prior = priors[0]
        # The pydantic Literal + extra="forbid" gate: constructing the
        # model at all proves the stamped constant is a legal group_id.
        assert isinstance(prior, PriorReference)
        assert prior.entity_id == "build_outcome:guardkit:TASK_MEM08_012"
        assert prior.group_id == PRIORS_GROUP_ID
        assert prior.relevance_score == pytest.approx(0.42)
        assert prior.summary == "lock the python floor"

    @pytest.mark.asyncio
    async def test_relevance_score_is_clamped_into_unit_interval(
        self,
    ) -> None:
        backend, _log, _calls = _make_backend(
            search=await _search_returning(
                [
                    _item("build_outcome:guardkit:HOT", score=1.7),
                    _item("build_outcome:guardkit:COLD", score=-0.3),
                ]
            )
        )
        reader = FleetMemoryPriorsReader(_config(), backend=backend)

        priors = await reader.read_priors(
            target_kind="subagent",
            target_identifier="autobuild_runner",
            stage_label="autobuild",
            build_id="build-clamp-1",
        )

        assert [p.relevance_score for p in priors] == [1.0, 0.0]

    @pytest.mark.asyncio
    async def test_summary_is_bounded_and_credential_scrubbed(self) -> None:
        # A DSN whose password is a 40-hex secret planted in the lessons
        # text: the summary must never carry the raw credential.
        secret = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
        lessons = (
            "connect with postgresql://forge:"
            + secret
            + "@fleet-db:5432/memory then "
            + "x" * 600
        )
        backend, _log, _calls = _make_backend(
            search=await _search_returning(
                [_item("warning:guardkit:DSN_LEAK", score=0.9, lessons=lessons)]
            )
        )
        reader = FleetMemoryPriorsReader(_config(), backend=backend)

        priors = await reader.read_priors(
            target_kind="subagent",
            target_identifier="autobuild_runner",
            stage_label="autobuild",
            build_id="build-redact-1",
        )

        summary = priors[0].summary
        assert secret not in summary
        assert "***REDACTED-HEX***" in summary
        # Bounded head: ~500 chars of source text (the redaction marker
        # may lengthen the scrubbed result slightly; the bound is on the
        # raw head that was carried forward).
        assert len(summary) <= 500 + len("***REDACTED-HEX***")

    @pytest.mark.asyncio
    async def test_content_is_summary_fallback_and_cap_is_five(self) -> None:
        items = [
            _item(
                f"build_outcome:guardkit:TASK_{i:04d}",
                score=0.5,
                content=f"outcome {i}",
            )
            for i in range(7)
        ]
        backend, _log, _calls = _make_backend(
            search=await _search_returning(items)
        )
        reader = FleetMemoryPriorsReader(_config(), backend=backend)

        priors = await reader.read_priors(
            target_kind="subagent",
            target_identifier="autobuild_runner",
            stage_label="autobuild",
            build_id="build-cap-1",
        )

        assert len(priors) == 5
        assert priors[0].summary == "outcome 0"

    @pytest.mark.asyncio
    async def test_request_shape_and_settings_hygiene(self) -> None:
        backend, _log, calls = _make_backend(
            search=await _search_returning([])
        )
        config = _config(project="guardkit", read_timeout_s=5.0)
        reader = FleetMemoryPriorsReader(config, backend=backend)

        await reader.read_priors(
            target_kind="subagent",
            target_identifier="autobuild_runner",
            stage_label="autobuild",
            build_id="build-shape-1",
        )

        kwargs = _StubRequest.last_kwargs
        assert kwargs is not None
        assert kwargs["project"] == "guardkit"
        assert kwargs["payload_types"] == ["build_outcome", "warning"]
        assert kwargs["token_budget"] == 2000
        assert kwargs["include_superseded"] is False
        assert kwargs["query"] == "subagent autobuild_runner autobuild outcome"
        # Settings hygiene: explicit kwargs only, env-file reading
        # disabled by construction (the forge/.env trap).
        assert calls[0]["_env_file"] is None
        assert calls[0]["pg_dsn"] == config.pg_dsn
        assert calls[0]["embed_url"] == config.embed_url
        assert calls[0]["embed_model"] == config.embed_model
        assert calls[0]["embed_dims"] == config.embed_dims

    def test_reader_satisfies_runtime_checkable_protocol(self) -> None:
        backend, _log, _calls = _make_backend(search=None)
        reader = FleetMemoryPriorsReader(_config(), backend=backend)
        assert isinstance(reader, PriorsReader)


class TestReadResilience:
    """Every failure mode degrades to [] and the store handle recovers."""

    @pytest.mark.asyncio
    async def test_timeout_returns_empty_resets_store_and_reconnects(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls = {"n": 0}

        async def _search(_request: Any, _store: Any) -> list[Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.Event().wait()  # never resolves
            return []

        backend, log, _settings = _make_backend(search=_search)
        reader = FleetMemoryPriorsReader(
            _config(read_timeout_s=0.05), backend=backend
        )

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            priors = await asyncio.wait_for(
                reader.read_priors(
                    target_kind="subagent",
                    target_identifier="autobuild_runner",
                    stage_label="autobuild",
                    build_id="build-slow-1",
                ),
                timeout=2.0,
            )

        assert priors == []
        record = next(
            r for r in caplog.records if r.levelno == logging.ERROR
        )
        assert "build-slow-1" in record.getMessage()
        assert "timed out" in record.getMessage()
        # Store handle reset — the next read reconnects fresh.
        assert reader._store is None
        assert log.count("open") == 1

        second = await reader.read_priors(
            target_kind="subagent",
            target_identifier="autobuild_runner",
            stage_label="autobuild",
            build_id="build-slow-2",
        )
        assert second == []
        assert log.count("open") == 2

    @pytest.mark.asyncio
    async def test_init_failure_returns_empty_and_next_call_retries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        attempts = {"n": 0}
        log: list[str] = []

        def settings_cls(**kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace()

        def store_context(_settings: Any) -> _StubStoreContext:
            attempts["n"] += 1
            log.append("open")
            exc = (
                RuntimeError("pool refused") if attempts["n"] == 1 else None
            )
            return _StubStoreContext(log, enter_exc=exc)

        backend = _RetrievalBackend(
            settings_cls=settings_cls,  # type: ignore[arg-type]
            store_context=store_context,
            request_cls=_StubRequest,
            search=await _search_returning([]),
        )
        reader = FleetMemoryPriorsReader(_config(), backend=backend)

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            first = await reader.read_priors(
                target_kind="subagent",
                target_identifier="autobuild_runner",
                stage_label="autobuild",
                build_id="build-init-1",
            )

        assert first == []
        assert reader._store is None
        assert any(
            "build-init-1" in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        )

        second = await reader.read_priors(
            target_kind="subagent",
            target_identifier="autobuild_runner",
            stage_label="autobuild",
            build_id="build-init-2",
        )
        assert second == []
        assert attempts["n"] == 2

    @pytest.mark.asyncio
    async def test_empty_store_is_normal_not_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend, _log, _calls = _make_backend(
            search=await _search_returning([])
        )
        reader = FleetMemoryPriorsReader(_config(), backend=backend)

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            priors = await reader.read_priors(
                target_kind="subagent",
                target_identifier="autobuild_runner",
                stage_label="autobuild",
                build_id="build-empty-1",
            )

        assert priors == []
        assert not [
            r for r in caplog.records if r.levelno >= logging.ERROR
        ]

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self) -> None:
        backend, log, _calls = _make_backend(
            search=await _search_returning([])
        )
        reader = FleetMemoryPriorsReader(_config(), backend=backend)
        await reader.read_priors(
            target_kind="subagent",
            target_identifier="autobuild_runner",
            stage_label="autobuild",
            build_id="build-close-1",
        )
        await reader.aclose()
        await reader.aclose()
        assert log.count("exit") == 1
        assert reader._store is None


class TestFirstReadRace:
    """Two concurrent first reads share one store open (asyncio.Lock)."""

    @pytest.mark.asyncio
    async def test_concurrent_first_reads_open_store_once(self) -> None:
        backend, log, _calls = _make_backend(
            search=await _search_returning([]), enter_delay=0.02
        )
        reader = FleetMemoryPriorsReader(_config(), backend=backend)

        async def _read(build_id: str) -> list[PriorReference]:
            return await reader.read_priors(
                target_kind="subagent",
                target_identifier="autobuild_runner",
                stage_label="autobuild",
                build_id=build_id,
            )

        first, second = await asyncio.gather(
            _read("build-race-1"), _read("build-race-2")
        )

        assert first == [] and second == []
        assert log.count("open") == 1
        assert log.count("enter") == 1
