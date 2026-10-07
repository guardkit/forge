"""Where an evidence window starts (7 October 2026).

A hit on a function's, method's or handler's own line must show the block of
lines written directly above it, where the route's method and path are
declared, whatever the language writes there. Neutral, made-up code in three
shapes: a decorated function, a method with attributes, and a route-table
entry. Nothing here is any real project's code.
"""

from __future__ import annotations

from forge.planning.code_evidence import (
    EVIDENCE_BLOCK_LINES_ABOVE,
    EVIDENCE_LINES_BEFORE,
    candidate_windows,
    window_start,
)


class _Files:
    """A reader holding a few files' text."""

    refused: dict[str, str] = {}

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)


def _window_round(path: str, text: str, hit_text: str) -> dict:
    """The one window round the line of ``text`` holding ``hit_text``."""
    number = next(n for n, line in enumerate(text.split("\n"), start=1) if hit_text in line)
    candidates, _hits, _not_read = candidate_windows(
        _Files({path: text}),
        [f"{path}:{number}"],
        words=["deactivate", "thing"],
        rank=lambda _place: 0,
        texts={},
    )
    assert len(candidates) == 1
    window = candidates[0]
    assert window["first_line"] <= number <= window["last_line"]
    return window


#: A decorated function: a long block of route options above the function.
DECORATED = (
    "def other():\n"
    "    return 1\n"
    "\n"
    "\n"
    "@things.patch(\n"
    '    "/{thing_id}/deactivate",\n'
    "    answer_model=ThingOut,\n"
    '    summary="Deactivate a thing",\n'
    "    answers={\n"
    '        200: {"description": "Thing deactivated"},\n'
    '        404: {"description": "Thing not found"},\n'
    '        409: {"description": "Thing already inactive"},\n'
    "    },\n"
    ")\n"
    "def deactivate_thing(thing_id):\n"
    '    """Deactivate a thing."""\n'
    "    return store.deactivate(thing_id)\n"
)

#: A method with attributes, as several typed languages write one.
ATTRIBUTED = (
    "public class ThingsController : ControllerBase\n"
    "{\n"
    "    private readonly IThingStore _store;\n"
    "\n"
    '    [HttpPatch("things/{thingId}/deactivate")]\n'
    "    [ProducesResponseType(StatusCodes.Status200OK)]\n"
    "    [ProducesResponseType(StatusCodes.Status404NotFound)]\n"
    "    [ProducesResponseType(StatusCodes.Status409Conflict)]\n"
    "    [ProducesResponseType(StatusCodes.Status503ServiceUnavailable)]\n"
    "    public async Task<IActionResult> DeactivateThing(string thingId)\n"
    "    {\n"
    "        var thing = await _store.Deactivate(thingId);\n"
    "        return Ok(thing);\n"
    "    }\n"
    "}\n"
)

#: A route-table entry: the path first, the handler named last.
ROUTE_TABLE = (
    "const things = express.Router();\n"
    "\n"
    "things.patch(\n"
    "  '/things/:thingId/deactivate',\n"
    "  authenticate,\n"
    "  validateThingId,\n"
    "  limitRate,\n"
    "  auditTrail,\n"
    "  handlers.deactivateThing,\n"
    ");\n"
    "\n"
    "module.exports = things;\n"
)


def test_a_decorated_functions_window_starts_at_its_route_options() -> None:
    window = _window_round("app/things.py", DECORATED, "def deactivate_thing(")
    assert window["first_line"] == 5
    assert window["text"].splitlines()[0] == "5: @things.patch("
    assert '"/{thing_id}/deactivate"' in window["text"]


def test_an_attributed_methods_window_starts_at_its_attributes() -> None:
    window = _window_round("src/ThingsController.cs", ATTRIBUTED, "DeactivateThing(")
    assert window["first_line"] == 5
    assert '[HttpPatch("things/{thingId}/deactivate")]' in window["text"]


def test_a_route_table_entrys_window_starts_at_its_path() -> None:
    window = _window_round("src/routes.js", ROUTE_TABLE, "handlers.deactivateThing")
    assert window["first_line"] == 3
    assert "things.patch(" in window["text"]
    assert "'/things/:thingId/deactivate'" in window["text"]


def test_a_window_never_starts_below_its_usual_leading_lines() -> None:
    # The block starts one line above the hit; the window still shows the
    # usual lines before it.
    lines = ["a", "b", "c", "d", "", "e", "hit", "f"]
    assert window_start(lines, 7) == 7 - EVIDENCE_LINES_BEFORE


def test_a_block_starting_further_up_than_the_limit_leaves_the_window_as_it_was() -> None:
    lines = ["x"] * (EVIDENCE_BLOCK_LINES_ABOVE + 10) + ["hit"]
    number = len(lines)
    assert window_start(lines, number) == number - EVIDENCE_LINES_BEFORE


def test_a_block_starting_at_the_limit_or_at_the_top_of_the_file_is_taken_whole() -> None:
    lines = [""] + ["x"] * EVIDENCE_BLOCK_LINES_ABOVE + ["hit"]
    number = len(lines)
    assert window_start(lines, number) == 2
    assert window_start(["x"] * 10 + ["hit"], 11) == 1
    # Lines of spaces are blank lines too.
    assert window_start(["x", "   ", "y", "z", "w", "v", "hit"], 7) == 3
