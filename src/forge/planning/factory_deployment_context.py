"""Immutable factory interface evidence, retained in existing planning events.

Bookkeeping (step 2, 26 October 2026, census FRG-049): a record of this
evidence that is lost, corrupt or in conflict, or an installed reference that
differs from its manifest, is logged as advice and the writers are given no
factory reference for that run; it never ends a run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

DOCUMENT_ID = "factory-deployment-contract"
DOCUMENT_VERSION = "1"
RESOURCE_NAME = "factory_deployment_contract.md"
# Updated with the reviewed resource, never inferred from a run's current install.
DOCUMENT_SHA256 = "7c754b23a887f8d259687366f2f85605b708356463e63f31c591b8d7a6966712"
EXPECTATION_STAGE = "factory-deployment-context-expectation"
CAPTURE_STAGE = "factory-deployment-context"
DISPATCH_STAGE = "factory-deployment-context-dispatch"
LEGACY_STAGE = "factory-deployment-context-legacy"
RECEIPT_KEY = "factory_context"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactoryDeploymentDocument:
    document_id: str
    version: str
    text: str
    bytes: int
    sha256: str

    @classmethod
    def of(cls, text: str, *, version: str = DOCUMENT_VERSION) -> FactoryDeploymentDocument:
        raw = text.encode("utf-8")
        return cls(DOCUMENT_ID, version, text, len(raw), hashlib.sha256(raw).hexdigest())

    def receipt(self) -> dict[str, str]:
        return {"document_id": self.document_id, "version": self.version, "sha256": self.sha256}

    def to_record(self) -> dict[str, Any]:
        return {**self.receipt(), "text": self.text, "bytes": self.bytes}

    def context_text(self) -> str:
        return self.text

    @classmethod
    def from_record(cls, record: Any) -> FactoryDeploymentDocument:
        identity = _receipt(record)
        if not isinstance(record.get("text"), str) or not record["text"]:
            raise ValueError("captured document has no text")
        document = cls.of(record["text"], version=identity["version"])
        if type(record.get("bytes")) is not int or record["bytes"] != document.bytes:
            raise ValueError("captured document byte count differs")
        if identity != document.receipt():
            raise ValueError("captured document hash differs")
        return document


def _receipt(record: Any) -> dict[str, str]:
    if (not isinstance(record, dict) or record.get("document_id") != DOCUMENT_ID
            or not isinstance(record.get("version"), str) or not record["version"].strip()
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
        raise ValueError("factory context identity is malformed")
    return {key: record[key] for key in ("document_id", "version", "sha256")}


def load_bundled_document() -> FactoryDeploymentDocument:
    raw = files("forge.planning").joinpath(RESOURCE_NAME).read_bytes()
    document = FactoryDeploymentDocument.of(raw.decode("utf-8"))
    if document.sha256 != DOCUMENT_SHA256:
        raise ValueError("installed factory deployment reference differs from its manifest")
    return document


def _record(store: Any, correlation_id: str, stage: str, record: dict[str, Any]) -> None:
    store._record_event(
        correlation_id=correlation_id, stage_label=stage, status="recorded",
        actor_identity="planning-driver", details_json=json.dumps(record),
    )


def record_factory_dispatch(store: Any, correlation_id: str,
                            document: FactoryDeploymentDocument | None, *, stage: str) -> None:
    """Durable evidence before dispatch, including a crash before the draft receipt."""
    recorded = ensure_factory_context(store, correlation_id)
    if recorded != document:
        logger.warning(
            "planning run %s: the factory reference changed before the %s "
            "dispatch — ADVISORY; nothing is recorded for it",
            correlation_id,
            stage,
        )
        return
    if document is not None:
        _record(store, correlation_id, DISPATCH_STAGE,
                {RECEIPT_KEY: document.receipt(), "writer": stage})


def ensure_factory_context(store: Any, correlation_id: str) -> FactoryDeploymentDocument | None:
    """Validate the whole history; capture only a new run or an exact pre-dispatch recovery.

    Equal duplicate records are harmless; conflicting records never pick a winner.
    Only an old spec draft without factory evidence establishes legacy behavior.
    A history that cannot be supplied intact is bookkeeping (step 2): it is
    logged and ``None`` comes back, so the writers get no factory reference.
    """
    try:
        expectations: list[dict[str, str]] = []
        captures: list[FactoryDeploymentDocument] = []
        receipts: list[dict[str, str]] = []
        spec_dispatched = False
        legacy_recorded = False
        relevant = {EXPECTATION_STAGE, CAPTURE_STAGE, DISPATCH_STAGE,
                    LEGACY_STAGE, "feature-spec-draft", "feature-plan"}
        for event in store.list_events(correlation_id):
            stage = event["stage_label"]
            if stage not in relevant:
                continue
            details = json.loads(event["details_json"] or "{}")
            if not isinstance(details, dict):
                raise ValueError("factory context event is not an object")
            if stage == EXPECTATION_STAGE:
                expectations.append(_receipt(details.get(RECEIPT_KEY)))
            elif stage == CAPTURE_STAGE:
                captures.append(FactoryDeploymentDocument.from_record(details.get(RECEIPT_KEY)))
            elif stage == DISPATCH_STAGE:
                receipts.append(_receipt(details.get(RECEIPT_KEY)))
            elif stage == LEGACY_STAGE:
                legacy_recorded = True
            else:
                payload = details.get("spec_draft") if stage == "feature-spec-draft" else details
                if stage == "feature-spec-draft" and event["status"] == "drafted":
                    if not isinstance(payload, dict):
                        raise ValueError("spec dispatch receipt is malformed")
                    spec_dispatched = True
                if isinstance(payload, dict) and RECEIPT_KEY in payload:
                    receipts.append(_receipt(payload[RECEIPT_KEY]))
        if expectations and any(item != expectations[0] for item in expectations):
            raise ValueError("conflicting factory context expectations")
        if captures and any(item != captures[0] for item in captures):
            raise ValueError("conflicting factory context captures")
        expected = expectations[0] if expectations else None
        document = captures[0] if captures else None
        if expected is None:
            if document is not None or receipts:
                raise ValueError("factory context expectation is missing")
            if spec_dispatched:
                if not legacy_recorded:
                    _record(store, correlation_id, LEGACY_STAGE, {
                        "disposition": "legacy-spec-dispatch-without-factory-context",
                        "detail": "The existing run keeps its original planning context.",
                    })
                return None
            document = load_bundled_document()
            expected = document.receipt()
            _record(store, correlation_id, EXPECTATION_STAGE, {RECEIPT_KEY: expected})
            _record(store, correlation_id, CAPTURE_STAGE, {RECEIPT_KEY: document.to_record()})
        elif document is None:
            if spec_dispatched or receipts:
                raise ValueError("factory context capture is missing after dispatch")
            document = load_bundled_document()
            if document.receipt() != expected:
                raise ValueError("installed factory context does not match the interrupted capture expectation")
            _record(store, correlation_id, CAPTURE_STAGE, {RECEIPT_KEY: document.to_record()})
        if document.receipt() != expected or any(item != expected for item in receipts):
            raise ValueError("factory context capture or dispatch receipt conflicts with its expectation")
        return document
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError, OSError) as exc:
        logger.warning(
            "planning run %s: the factory deployment context recorded for this "
            "run cannot be supplied intact (%s) — ADVISORY; the writers are "
            "given no factory reference",
            correlation_id,
            exc,
        )
        return None
