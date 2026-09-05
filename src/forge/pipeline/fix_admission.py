"""Opening a fix journey: the one place a repair becomes a queued build.

Public surface
==============

- :func:`admit_fix_build` — the whole of what ``forge queue --mode c`` used
  to do inline: THE CAP LAW, the task-id check, the fix-task YAML's
  ``parent_feature``, the build row, the publish.
- :func:`admit_fix_row` — the queue's path into the same function: it mints
  the task id, writes the fix-task YAML beside the target repository's
  features, and then calls :func:`admit_fix_build`.
- :func:`republish_build_queued` — says a written-but-never-announced
  build's queued event again, rebuilt from the build row itself.
- :class:`FixAdmission` — what a successful admission hands back.
- :class:`FixAdmissionRefused` / :class:`FixPublishFailed` — the two ways it
  can end badly, each with a sentence a person can read.

Why it is a module and not a branch of the CLI
----------------------------------------------

Until now the only way to open a fix journey was to type
``forge queue --mode c`` — which is why the conductor has been idle since 4
August. The work queue can now admit a repair by itself, and the one thing
that must not happen is a second, subtly different statement of how a fix
journey opens. In particular THE CAP LAW — a fix journey whose budget
profile carries no review-cycle cap does not open, because the 2026-08-02
crossing ran about two hundred legs when nothing capped it — has to be the
same rule on both paths. So the steps live here, both callers call them, and
the law is read from :mod:`forge.config.conductor` exactly once per
admission.

**Never a shell-out.** The queue does not run ``forge queue`` in a
subprocess: it calls this function in process, with the queue row's own
correlation id, so the row, the build and every downstream receipt share one
spine.

The task id, and the file beside the features
---------------------------------------------

A fix journey's subject is a TASK id, not a feature id, and the wire's
pattern for one is narrow: ``TASK-`` followed by three to twelve upper-case
letters and digits. :func:`mint_fix_task_id` spells it
``TASK-<feature8>FIX<n>`` — the parent feature with its punctuation removed
and cut to eight characters, then ``FIX``, then the next free number — and
trims the feature half further if the whole would overflow twelve.

The fix-task YAML it writes is the drive-6 shape and nothing more: ``id``,
``name``, ``parent_feature``. It lands in the target repository's own
features directory (``.guardkit/features/``), which is where every other
leg of the journey looks for the file it was given.

References
----------
- ``docs/conductor-rewire-spec-2026-09-05.md`` rule 2.
"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


#: The wire's pattern for a fix journey's subject identifier. Mirrors
#: ``TASK_ID_PATTERN`` in ``nats_core.events._pipeline`` so a bad id is
#: refused here rather than one stack frame deeper.
TASK_ID_REGEX: re.Pattern[str] = re.compile(r"^TASK-[A-Z0-9]{3,12}$")

#: How long the identifier after ``TASK-`` may be.
MAX_TASK_SUFFIX_CHARS: int = 12

#: The word that separates the parent feature from the repair's number.
FIX_TOKEN: str = "FIX"

#: Where a target repository keeps the specs its legs are given.
FEATURES_DIR_PARTS: tuple[str, ...] = (".guardkit", "features")

#: NATS subject family for build-queued events.
BUILD_QUEUED_SUBJECT_PREFIX: str = "pipeline.build-queued"

#: Source id stamped on the envelope the queue's own admission publishes.
SOURCE_ID: str = "forge"

#: The events-row action written on the queue row when its build is open.
ADMITTED_BUILD_ACTION: str = "admitted_build"

#: The events-row action written on the queue row when the queue says a
#: written-but-never-dispatched build's queued event again (close-out item 2).
REPUBLISHED_ACTION: str = "republished"

#: The refusal reason that means "there is already a build for this".
DUPLICATE_REASON: str = "duplicate"

#: The refusals that mean "not now" rather than "not ever". Everything else a
#: refusal can say — an unknown repository, an uncapped profile, a malformed
#: fix-task file, a row that names no build — will say the same thing on the
#: next tick, so the queue closes the row instead of asking again for ever.
TRANSIENT_REFUSAL_REASONS: frozenset[str] = frozenset({DUPLICATE_REASON})


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FixAdmission:
    """What opening a fix journey produced."""

    build_id: str
    task_id: str
    feature_id: str
    correlation_id: str
    repo: str
    fix_task_path: str
    source_build_id: str | None = None
    published: bool = True


class FixAdmissionRefused(Exception):
    """The journey did not open, and nothing was written.

    Attributes:
        message: One plain sentence saying why.
        reason: A short machine word for the caller to map onto its own
            exit code — one of ``cap``, ``task-id``, ``fix-task-yaml``,
            ``parent-feature``, ``repo-not-allowed``, ``repo-unknown``,
            ``no-source-build`` or ``duplicate``.
        permanent: Whether trying again changes anything. A repository the
            configuration does not know, a budget profile with no cap, a row
            that names no build, a fix-task file that will not parse: every
            one of those refuses exactly the same way on the next tick, so
            the queue closes the row rather than offering it for ever. The
            one refusal that is NOT permanent is ``duplicate`` — another
            build for the same feature is in flight right now, and when it
            ends this row can go.
    """

    def __init__(
        self, message: str, *, reason: str, permanent: bool | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.permanent = (
            reason not in TRANSIENT_REFUSAL_REASONS if permanent is None else permanent
        )


class FixPublishFailed(Exception):
    """The build row was written but the pipeline was not told about it.

    The row is deliberately NOT rolled back: SQLite is the pipeline's truth
    and the on-boot reconciler redrives an orphaned row. Carries the
    admission so the caller can say what was written.
    """

    def __init__(self, message: str, *, admission: FixAdmission) -> None:
        super().__init__(message)
        self.message = message
        self.admission = admission


# ---------------------------------------------------------------------------
# The task id and the file beside the features
# ---------------------------------------------------------------------------


def features_dir(repo_path: Path | str) -> Path:
    """The directory a repository keeps its feature and fix-task specs in."""
    return Path(repo_path).joinpath(*FEATURES_DIR_PARTS)


def _feature_stem(feature_id: str) -> str:
    """The parent feature as up to eight upper-case letters and digits."""
    stripped = "".join(ch for ch in feature_id.upper() if ch.isalnum())
    return stripped[:8] or "FIX"


def mint_fix_task_id(feature_id: str, *, existing: Iterable[str] = ()) -> str:
    """``TASK-<feature8>FIX<n>`` — the next repair of this feature.

    ``existing`` is every task id already spoken for (the fix-task files
    already in the repository's features directory, say). The number is the
    first one not in that set, counting from 1, and the feature half is cut
    down as far as it has to be so the whole identifier stays inside the
    wire's twelve characters.
    """
    taken = {str(item).upper() for item in existing}
    stem = _feature_stem(feature_id)
    number = 1
    while True:
        tail = f"{FIX_TOKEN}{number}"
        room = MAX_TASK_SUFFIX_CHARS - len(tail)
        if room < 1:
            # A repair number long enough to crowd out the feature name is
            # not a numbering problem any more; say so rather than mint an
            # identifier nobody can read back to a feature.
            raise FixAdmissionRefused(
                f"there are already {number - 1} repairs of {feature_id} and "
                "no room left in a task identifier for another",
                reason="task-id",
                permanent=True,
            )
        candidate = f"TASK-{stem[:room]}{tail}"
        if candidate not in taken:
            return candidate
        number += 1


def existing_fix_task_ids(repo_path: Path | str) -> set[str]:
    """Every task id that already has a file in the repository's features."""
    directory = features_dir(repo_path)
    try:
        names = [path.stem.upper() for path in directory.glob("TASK-*.y*ml")]
    except OSError:  # pragma: no cover - unreadable directory
        return set()
    return set(names)


def write_fix_task_yaml(
    *,
    repo_path: Path | str,
    task_id: str,
    parent_feature: str,
    name: str,
) -> Path:
    """Write the three-field fix-task spec and return where it landed.

    The drive-6 shape and nothing else: ``id``, ``name``, ``parent_feature``.
    Written beside the repository's features, because that is where the legs
    of the journey are pointed.
    """
    import yaml

    directory = features_dir(repo_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.yaml"
    path.write_text(
        yaml.safe_dump(
            {"id": task_id, "name": name, "parent_feature": parent_feature},
            sort_keys=False,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    return path


def read_parent_feature(yaml_path: Path | str) -> str:
    """The ``parent_feature`` a fix-task YAML declares.

    Raises:
        FixAdmissionRefused: when the file cannot be read, is not a mapping,
            or declares no non-empty ``parent_feature``.
    """
    import yaml

    path = Path(yaml_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixAdmissionRefused(
            f"Cannot read fix-task YAML {str(path)!r}: {exc}",
            reason="fix-task-yaml",
            permanent=True,
        ) from exc

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise FixAdmissionRefused(
            f"Fix-task YAML {str(path)!r} is malformed: {exc}",
            reason="fix-task-yaml",
            permanent=True,
        ) from exc

    if not isinstance(data, dict):
        raise FixAdmissionRefused(
            f"Fix-task YAML {str(path)!r} must be a YAML mapping at the top level",
            reason="fix-task-yaml",
            permanent=True,
        )

    parent = data.get("parent_feature")
    if not isinstance(parent, str) or not parent.strip():
        raise FixAdmissionRefused(
            "Mode C requires the fix-task YAML to declare a non-empty "
            f"'parent_feature' field (string); got {parent!r} in {path}",
            reason="fix-task-yaml",
            permanent=True,
        )
    return parent


# ---------------------------------------------------------------------------
# The admission itself
# ---------------------------------------------------------------------------


async def admit_fix_build(
    *,
    config: Any,
    persistence: Any,
    task_id: str,
    fix_task_yaml: Path | str,
    repo_path: Path | str,
    correlation_id: str,
    publish: Callable[[str, bytes], Any],
    branch: str = "main",
    profile: str | None = None,
    uncapped_acknowledged: bool = False,
    max_turns: int | None = None,
    sdk_timeout_seconds: int | None = None,
    originating_user: str | None = None,
    triggered_by: str = "cli",
    originating_adapter: str | None = None,
    parent_request_id: str | None = None,
    source_build_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FixAdmission:
    """Open one fix journey: check it, write its row, tell the pipeline.

    In order, and every step before any side effect of the next:

    1. **THE CAP LAW** — an uncapped or unresolvable budget profile does not
       open a fix journey. Read from :mod:`forge.config.conductor`, the one
       statement of the rule both this and the daemon's router read.
    2. **The subject** must be a TASK identifier of the shape the wire
       allows.
    3. **The parent feature** comes from the fix-task YAML's
       ``parent_feature`` and is validated as a feature identifier.
    4. **The repository** must be one ``queue.repo_allowlist`` allows (an
       empty allowlist, the default, allows everything).
    5. **The row is written first, then published** — the write-then-publish
       discipline the whole pipeline keeps. A publish that fails raises
       :class:`FixPublishFailed` and the row deliberately stays.

    Args:
        publish: ``(subject, body) -> None`` or an awaitable of the same. The
            transports differ — the CLI opens a one-shot connection, the
            daemon has a live client — so the caller brings its own.
        source_build_id: The FAILED build this journey repairs, recorded for
            the caller's own audit trail. The journey itself finds the pack
            through the correlation id (``fix-<source build id>``).

    Returns:
        The :class:`FixAdmission` describing what was opened.

    Raises:
        FixAdmissionRefused: at any of steps 1 to 4, having written nothing.
        FixPublishFailed: when the row landed and the publish did not.
    """
    from forge.config.conductor import mode_c_cap_refusal_from_config
    from forge.lifecycle.identifiers import (
        InvalidIdentifierError,
        validate_feature_id,
    )
    from forge.lifecycle.modes import BuildMode
    from forge.lifecycle.persistence import DuplicateBuildError

    # 1. THE CAP LAW, before every side effect.
    cap_refusal = mode_c_cap_refusal_from_config(
        config, profile, uncapped_acknowledged=uncapped_acknowledged
    )
    if cap_refusal is not None:
        raise FixAdmissionRefused(
            cap_refusal.message, reason="cap", permanent=True
        )

    # 2. The subject.
    if not TASK_ID_REGEX.match(task_id):
        raise FixAdmissionRefused(
            "Mode C requires positional argument to match "
            f"{TASK_ID_REGEX.pattern}; got {task_id!r}",
            reason="task-id",
            permanent=True,
        )

    # 3. The parent feature, from the fix-task YAML.
    raw_parent = read_parent_feature(fix_task_yaml)
    try:
        feature_id = validate_feature_id(raw_parent)
    except InvalidIdentifierError as exc:
        raise FixAdmissionRefused(
            f"Invalid parent_feature in fix-task YAML ({exc.reason}): "
            f"{exc.value!r}",
            reason="parent-feature",
            permanent=True,
        ) from exc

    # 4. The repository.
    repo = Path(repo_path)
    if not _repo_allowed(repo, config):
        raise FixAdmissionRefused(
            f"Repository {str(repo)!r} is not in queue.repo_allowlist; "
            "refusing to enqueue (Group C path-allowlist refused).",
            reason="repo-not-allowed",
            permanent=True,
        )

    # 5. The payload, the row, then the publish.
    from nats_core.envelope import EventType, MessageEnvelope
    from nats_core.events import BuildQueuedPayload

    now = (clock or (lambda: datetime.now(UTC)))()
    queue_config = getattr(config, "queue", None)
    payload = BuildQueuedPayload(
        feature_id=feature_id,
        repo=repo_slug(repo),
        branch=branch,
        feature_yaml_path=str(Path(fix_task_yaml)),
        max_turns=(
            max_turns
            if max_turns is not None
            else getattr(queue_config, "default_max_turns", 5)
        ),
        sdk_timeout_seconds=(
            sdk_timeout_seconds
            if sdk_timeout_seconds is not None
            else getattr(queue_config, "default_sdk_timeout_seconds", 1800)
        ),
        triggered_by=triggered_by,
        originating_adapter=originating_adapter,
        originating_user=originating_user,
        correlation_id=correlation_id,
        parent_request_id=parent_request_id,
        requested_at=now,
        queued_at=now,
        mode=BuildMode.MODE_C.value,
        task_id=task_id,
    )

    if persistence.exists_active_build(feature_id):
        raise FixAdmissionRefused(
            f"duplicate build refused: an active build for {feature_id} "
            "is already in flight (Group C).",
            reason="duplicate",
            permanent=False,
        )

    try:
        build_id = persistence.queue_build(
            payload, mode=BuildMode.MODE_C, profile=profile
        )
    except DuplicateBuildError as exc:
        raise FixAdmissionRefused(
            f"duplicate build refused: {exc} (Group B).",
            reason="duplicate",
            permanent=False,
        ) from exc

    admission = FixAdmission(
        build_id=str(build_id),
        task_id=task_id,
        feature_id=feature_id,
        correlation_id=correlation_id,
        repo=payload.repo,
        fix_task_path=str(Path(fix_task_yaml)),
        source_build_id=source_build_id,
    )

    envelope = MessageEnvelope(
        source_id=SOURCE_ID,
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )
    subject = f"{BUILD_QUEUED_SUBJECT_PREFIX}.{feature_id}"
    try:
        result = publish(subject, envelope.model_dump_json().encode("utf-8"))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 — translated, never swallowed
        raise FixPublishFailed(
            f"Queued {feature_id} (build pending) but pipeline NOT NOTIFIED — "
            f"publish failed (messaging-layer): {exc}",
            admission=admission,
        ) from exc

    logger.info(
        "fix admission: opened %s for %s (task %s, repo %s, correlation id "
        "%s, profile %s)",
        admission.build_id,
        feature_id,
        task_id,
        admission.repo,
        correlation_id,
        profile,
    )
    return admission


async def admit_fix_row(
    *,
    config: Any,
    persistence: Any,
    store: Any,
    queue_id: int,
    correlation_id: str,
    sentence: str,
    target_repo: str | None,
    publish: Callable[[str, bytes], Any],
    originating_user: str | None = None,
    source_build_id: str | None = None,
    branch: str = "main",
    profile: str | None = None,
    actor_identity: str = "forge-work-queue",
    clock: Callable[[], datetime] | None = None,
) -> FixAdmission:
    """Turn one ``kind='fix'`` queue row into an open fix journey.

    Mints the task id, writes the fix-task YAML beside the target
    repository's features, and hands the rest to :func:`admit_fix_build`.
    Records what it opened against the queue row so the row and its build can
    be read back as one thing.

    Raises:
        FixAdmissionRefused: when the source build, the repository or the
            fix-task spec cannot be resolved, or the shared checks refuse.
        FixPublishFailed: when the row landed and the publish did not.
    """
    from forge.pipeline.fix_row_producer import source_build_id_from_correlation_id
    from forge.planning.target_repos import (
        refusal_message,
        resolve_target_repo,
    )

    source = source_build_id or source_build_id_from_correlation_id(correlation_id)
    if not source:
        raise FixAdmissionRefused(
            f"#{queue_id} does not name the build it is repairing, so there "
            "is no failed build to review.",
            reason="no-source-build",
            permanent=True,
        )

    row = persistence.get_build_row(source)
    if row is None:
        raise FixAdmissionRefused(
            f"#{queue_id} is a repair of {source}, and there is no such "
            "build on record any more.",
            reason="no-source-build",
            permanent=True,
        )
    parent_feature = str(getattr(row, "feature_id", "") or "")
    if not parent_feature:
        raise FixAdmissionRefused(
            f"the build {source} names no feature, so there is nothing for a "
            "repair to point at.",
            reason="parent-feature",
            permanent=True,
        )

    name = str(target_repo or getattr(row, "repo", "") or "")
    paths = dict(getattr(config.planning, "target_repo_paths", {}) or {})
    resolution = resolve_target_repo(name, paths)
    if resolution.name is None:
        raise FixAdmissionRefused(
            refusal_message(name, resolution, paths),
            reason="repo-unknown",
            permanent=True,
        )
    repo_path = Path(str(paths[resolution.name])).expanduser()

    task_id = mint_fix_task_id(
        parent_feature, existing=existing_fix_task_ids(repo_path)
    )
    fix_task_path = write_fix_task_yaml(
        repo_path=repo_path,
        task_id=task_id,
        parent_feature=parent_feature,
        name=_one_line(sentence),
    )

    admission = await admit_fix_build(
        config=config,
        persistence=persistence,
        task_id=task_id,
        fix_task_yaml=fix_task_path,
        repo_path=repo_path,
        correlation_id=correlation_id,
        publish=publish,
        branch=branch,
        profile=profile,
        max_turns=getattr(config.queue, "default_max_turns", None),
        sdk_timeout_seconds=getattr(config.queue, "default_sdk_timeout_seconds", None),
        originating_user=originating_user,
        triggered_by="forge-internal",
        source_build_id=source,
        clock=clock,
    )

    _record_admitted_build(
        store,
        queue_id=queue_id,
        admission=admission,
        actor_identity=actor_identity,
    )
    return admission


async def republish_build_queued(
    build: Any,
    *,
    publish: Callable[[str, bytes], Any],
) -> str:
    """Say the queued event again for a build row that was never announced.

    The write comes before the publish, deliberately, so a publish that fails
    leaves a real build row that nothing was ever told about. Everything the
    event says is on that row, so this rebuilds the SAME event from the row —
    same feature, same repository, same fix-task file, same correlation id,
    same task, same queued moment — and says it on the same subject. Saying it
    twice is safe: the build row it names already exists and is keyed by
    ``(feature_id, correlation_id)``, so a second hearing finds the same
    build rather than starting another one.

    Args:
        build: The ``builds`` row, however the caller reads rows — a
            ``sqlite3.Row``, a mapping, or the typed ``BuildRow``.
        publish: ``(subject, body) -> None`` or an awaitable of the same, the
            caller's own transport.

    Returns:
        The subject it published on.
    """
    from nats_core.envelope import EventType, MessageEnvelope
    from nats_core.events import BuildQueuedPayload

    feature_id = str(_row_value(build, "feature_id", ""))
    correlation_id = str(_row_value(build, "correlation_id", ""))
    queued_at = _as_datetime(_row_value(build, "queued_at"))
    mode = _row_value(build, "mode", "mode-c")
    payload = BuildQueuedPayload(
        feature_id=feature_id,
        repo=str(_row_value(build, "repo", "")),
        branch=str(_row_value(build, "branch", "main")),
        feature_yaml_path=str(_row_value(build, "feature_yaml_path", "")),
        max_turns=int(_row_value(build, "max_turns", 5)),
        sdk_timeout_seconds=int(_row_value(build, "sdk_timeout_seconds", 1800)),
        triggered_by=str(_row_value(build, "triggered_by", "forge-internal")),
        originating_adapter=_row_value(build, "originating_adapter"),
        originating_user=_row_value(build, "originating_user"),
        correlation_id=correlation_id,
        parent_request_id=_row_value(build, "parent_request_id"),
        requested_at=queued_at,
        queued_at=queued_at,
        mode=str(getattr(mode, "value", mode)),
        task_id=_row_value(build, "task_id"),
    )
    envelope = MessageEnvelope(
        source_id=SOURCE_ID,
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )
    subject = f"{BUILD_QUEUED_SUBJECT_PREFIX}.{feature_id}"
    result = publish(subject, envelope.model_dump_json().encode("utf-8"))
    if inspect.isawaitable(result):
        await result
    logger.info(
        "fix admission: said the queued event again for %s (%s, correlation "
        "id %s) — it was written and never announced",
        _row_value(build, "build_id", feature_id),
        feature_id,
        correlation_id,
    )
    return subject


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    """One field of a build row, whether it reads like a mapping or an object."""
    try:
        value = row[name]
    except (KeyError, IndexError, TypeError):
        value = getattr(row, name, None)
    return default if value is None else value


def _as_datetime(value: Any) -> datetime:
    """A moment from a build row: a datetime as it is, a string parsed, else now."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)


def _record_admitted_build(
    store: Any,
    *,
    queue_id: int,
    admission: FixAdmission,
    actor_identity: str,
) -> None:
    """Write down which build this row opened; never stop the journey for it."""
    try:
        store.record_event(
            queue_id=queue_id,
            action=ADMITTED_BUILD_ACTION,
            actor_identity=actor_identity,
            details={
                "build_id": admission.build_id,
                "task_id": admission.task_id,
                "feature_id": admission.feature_id,
                "source_build_id": admission.source_build_id,
                "fix_task_path": admission.fix_task_path,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a note never costs a journey
        logger.warning(
            "fix admission: could not record the build against #%d (%s: %s)",
            queue_id,
            type(exc).__name__,
            exc,
        )


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _sanitise_segment(segment: str) -> str:
    """Replace any character outside ``[A-Za-z0-9._-]`` with ``_``."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in segment)


def repo_slug(repo: Path | str) -> str:
    """An ``org/name`` slug for a checkout path, as the wire requires.

    The last two components of the resolved path, with anything outside
    ``[A-Za-z0-9._-]`` replaced — the same bridge ``forge queue`` has always
    used between a filesystem path and the wire's GitHub-shaped slug. A
    single-component path becomes ``local/<name>``.
    """
    resolved = Path(repo).expanduser().resolve()
    name = _sanitise_segment(resolved.name) or "repo"
    parent = resolved.parent.name
    org = _sanitise_segment(parent) if parent else "local"
    return f"{org or 'local'}/{name}"


def path_in_allowlist(repo: Path | str, allowlist: Iterable[Path | str]) -> bool:
    """Whether ``repo`` is a checkout the allowlist allows.

    Compared against the RESOLVED absolute path, and a nested checkout under
    an allowed root passes. An empty allowlist — the schema default — means
    no restriction, so everything passes.
    """
    entries = list(allowlist)
    if not entries:
        return True
    repo_resolved = Path(repo).expanduser().resolve()
    for entry in entries:
        try:
            entry_resolved = Path(entry).expanduser().resolve()
        except (OSError, RuntimeError):
            # Defensive — a pathological symlink loop in forge.yaml should
            # not crash an admission. Skip the bad entry.
            logger.warning("repo_allowlist entry %r could not be resolved", entry)
            continue
        if repo_resolved == entry_resolved:
            return True
        try:
            repo_resolved.relative_to(entry_resolved)
        except ValueError:
            continue
        return True
    return False


def _repo_allowed(repo: Path, config: Any) -> bool:
    """Whether ``queue.repo_allowlist`` allows this checkout (empty = all)."""
    allowlist = list(getattr(getattr(config, "queue", None), "repo_allowlist", []) or [])
    return path_in_allowlist(repo, allowlist)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


__all__ = [
    "ADMITTED_BUILD_ACTION",
    "DUPLICATE_REASON",
    "REPUBLISHED_ACTION",
    "TRANSIENT_REFUSAL_REASONS",
    "BUILD_QUEUED_SUBJECT_PREFIX",
    "FEATURES_DIR_PARTS",
    "FixAdmission",
    "FixAdmissionRefused",
    "FixPublishFailed",
    "MAX_TASK_SUFFIX_CHARS",
    "SOURCE_ID",
    "TASK_ID_REGEX",
    "admit_fix_build",
    "admit_fix_row",
    "existing_fix_task_ids",
    "features_dir",
    "mint_fix_task_id",
    "path_in_allowlist",
    "read_parent_feature",
    "repo_slug",
    "republish_build_queued",
    "write_fix_task_yaml",
]
