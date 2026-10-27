"""The factory's bundled deployment reference, given to the spec and plan writers.

The reference ships with Forge as ``factory_deployment_contract.md``. It is
read from the installed package and handed to the writers as context. The
receipts, hash checks and history validation that used to bind it to each
run went in the cull (27 October 2026); a reference that cannot be read is
simply not given.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

DOCUMENT_ID = "factory-deployment-contract"
DOCUMENT_VERSION = "1"
RESOURCE_NAME = "factory_deployment_contract.md"


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

    def context_text(self) -> str:
        return self.text


def load_bundled_document() -> FactoryDeploymentDocument:
    raw = files("forge.planning").joinpath(RESOURCE_NAME).read_bytes()
    return FactoryDeploymentDocument.of(raw.decode("utf-8"))


def ensure_factory_context(store: Any, correlation_id: str) -> FactoryDeploymentDocument | None:
    """The bundled reference for this run's writers, or ``None`` when the
    installed copy cannot be read. ``store`` and ``correlation_id`` are kept
    for the callers' existing signature."""
    del store, correlation_id
    try:
        return load_bundled_document()
    except (OSError, UnicodeError):
        return None
