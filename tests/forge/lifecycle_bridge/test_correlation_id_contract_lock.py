"""ASSUM-009 contract-lock test for the correlation-id source (TASK-FRR-PEB-014).

Under the ratified Option C, the bridge runs in-forge and the
correlation-id source is :class:`BuildContext`, not the SSE event. This
test locks that contract. If a future review flips the option to D or
E, this test must be upgraded to a real cross-process validator that
rejects in-receive emits whose correlation-id does not match the
registered build.

Acceptance-criteria coverage map:

* AC-1: A single test constructs a :class:`BuildContext` with
  correlation-id ``"A"``, builds a :class:`StreamPart` the translator
  would normally accept, and asserts that
  :meth:`StreamEventTranslator.translate` produces an envelope whose
  ``correlation_id == "A"`` (sourced from the ``BuildContext``, not
  from the SSE event payload — even if the event payload itself carries
  a different ``correlation_id`` field, the translator MUST ignore it).
* AC-3: The test uses :func:`inspect.getsource` on
  :meth:`StreamEventTranslator.translate` (and its dispatch helpers)
  and asserts that no occurrence of ``correlation_id=stream_part.``
  (or any pattern reading the id from the event) appears in the
  source. This is the static-analysis invariant equivalent to the
  F010C AST guard, scoped to the translator.

Why this test is a no-op under Option C
---------------------------------------

The current translator always sources ``correlation_id`` from
:attr:`BuildContext.correlation_id` (see
:meth:`StreamEventTranslator._dispatch` — the ``cid`` local is read
once from ``context.correlation_id`` at the top of dispatch and threaded
through every payload constructor). There is no fallback path that
reads ``correlation_id`` from ``stream_part.event_data`` or
``stream_part.data``. The F010C AST guard already prevents
``_safe_publish_*`` calls from omitting ``correlation_id=``; this test
locks the *source* of that id at the translator boundary so a future
contributor cannot silently introduce a regression of the form::

    # FORBIDDEN — would source the id from the SSE event:
    correlation_id = stream_part.event_data.get(
        "correlation_id", context.correlation_id
    )

If such a fallback were added, both the dynamic assertion (the
envelope's ``correlation_id`` would equal the event-supplied value
when the context's value differs) AND the static assertion
(``correlation_id=stream_part.`` would appear in the source) would
fail this test.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

from langgraph_sdk.schema import StreamPart

from forge.lifecycle_bridge.bridge import BuildContext
from forge.lifecycle_bridge import translation as translation_module
from forge.lifecycle_bridge.translation import (
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)


def test_translate_sources_correlation_id_from_build_context_not_event() -> None:
    """ASSUM-009 contract-lock — correlation-id is sourced from BuildContext.

    Constructs a :class:`BuildContext` with correlation-id ``"A"`` and a
    :class:`StreamPart` whose embedded ``correlation_id`` field would
    differ (``"B-from-sse-event"``) if the translator ever started
    sourcing it from the event. Asserts:

    1. (Dynamic, AC-1) The emitted envelope's ``correlation_id`` equals
       ``"A"`` — the value from the :class:`BuildContext`, not the
       value embedded in the SSE event payload.
    2. (Static, AC-3) :func:`inspect.getsource` on the translator's
       :meth:`translate` method (and the entire :class:`StreamEventTranslator`
       class source) contains no occurrence of any pattern reading the
       correlation-id from the ``StreamPart`` event itself.
    """

    # --- Arrange ----------------------------------------------------------
    context = BuildContext(
        feature_id="FEAT-ASSUM-009",
        thread_id="thread-assum-009",
        run_id="run-assum-009",
        correlation_id="A",
        deadline_at=datetime.now(UTC) + timedelta(seconds=300),
    )

    # A StreamPart that the translator would normally accept (a "values"
    # event carrying a running_wave AutobuildState snapshot — yields a
    # BuildStartedPayload on first observation). The payload deliberately
    # carries a *different* correlation_id field at multiple plausible
    # locations in the event body so this test catches any regression that
    # reads from the event instead of the BuildContext.
    stream_part = StreamPart(
        event=VALUES_STREAM_EVENT,
        data={
            # A correlation_id at the top level that the translator MUST
            # NOT read.
            "correlation_id": "B-from-sse-event",
            "async_tasks": {
                "FEAT-ASSUM-009": {
                    "feature_id": "FEAT-ASSUM-009",
                    "build_id": "build-FEAT-ASSUM-009-20260507120000",
                    "lifecycle": "running_wave",
                    "wave_total": 1,
                    "wave_index": 0,
                    "task_index": 0,
                    "tasks_completed": 0,
                    "tasks_failed": 0,
                    # And one nested in the snapshot itself.
                    "correlation_id": "C-from-snapshot",
                },
            },
        },
    )

    translator = StreamEventTranslator()

    # --- Act --------------------------------------------------------------
    envelope = translator.translate(stream_part, context)

    # --- Assert (AC-1: dynamic — id sourced from BuildContext) ------------
    assert envelope is not None, (
        "ASSUM-009: a running_wave snapshot must yield an envelope so the "
        "correlation-id source is observable in this contract-lock test."
    )
    assert getattr(envelope, "correlation_id", None) == "A", (
        "ASSUM-009: translator MUST source correlation_id from "
        "BuildContext (got "
        f"{getattr(envelope, 'correlation_id', None)!r}, expected 'A'). "
        "If this fails, a code path now reads correlation_id from the SSE "
        "event payload — this regresses the Option C contract."
    )

    # --- Assert (AC-3: static — no source path reads from the event) ------
    # Static-analysis invariant: the translate() method (and its helpers
    # accessible via the class source) must not contain any occurrence of
    # an expression that reads correlation_id from the SSE event.
    translate_src = inspect.getsource(StreamEventTranslator.translate)
    class_src = inspect.getsource(StreamEventTranslator)
    module_src = inspect.getsource(translation_module)

    forbidden_substrings = (
        # Reads the id from the StreamPart attribute.
        "correlation_id=stream_part.",
        # Reads the id from a `data` dict pulled off the StreamPart.
        'data.get("correlation_id"',
        "data.get('correlation_id'",
        # Reads the id from the `event_data` attribute (the D/E shape).
        'stream_part.event_data.get("correlation_id"',
        "stream_part.event_data.get('correlation_id'",
        # Reads from a snapshot field (`snap.correlation_id`).
        "snap.correlation_id",
        "snapshot.correlation_id",
    )
    for needle in forbidden_substrings:
        assert needle not in translate_src, (
            f"ASSUM-009 contract-lock: forbidden source pattern {needle!r} "
            "appears in StreamEventTranslator.translate — the translator "
            "MUST source correlation_id from BuildContext only."
        )
        assert needle not in class_src, (
            f"ASSUM-009 contract-lock: forbidden source pattern {needle!r} "
            "appears in StreamEventTranslator class source — the translator "
            "MUST source correlation_id from BuildContext only."
        )
        assert needle not in module_src, (
            f"ASSUM-009 contract-lock: forbidden source pattern {needle!r} "
            "appears in translation module source — no helper may read "
            "correlation_id from the SSE event under Option C."
        )

    # Belt-and-braces: the dispatch path's `cid` MUST come from
    # `context.correlation_id`. Confirm the canonical assignment is
    # present so a refactor that renames `cid` is forced through this
    # test (and gets a chance to re-think the contract).
    assert "context.correlation_id" in class_src, (
        "ASSUM-009 contract-lock: expected `context.correlation_id` to be "
        "the sole correlation-id source in StreamEventTranslator. If this "
        "assertion fails after a rename, update the canonical accessor "
        "name in this test AND confirm the new accessor still reads from "
        "BuildContext (not from the SSE event)."
    )
