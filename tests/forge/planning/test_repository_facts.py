"""The fact sheet says what the repository's sibling routes already do — and
only what it can prove."""

from __future__ import annotations

import subprocess
from pathlib import Path

from forge.planning.repository_facts import (
    routes_in_python_file,
    what_the_repository_already_does,
)

ROUTER = '''
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/count-today", response_model=UserCount)
async def count_today(db: AsyncSession = Depends(get_db)) -> UserCount:
    ...


@router.get("/count-by-domain", response_model=list[DomainCount])
async def count_by_domain(db: AsyncSession = Depends(get_db)):
    ...


@router.delete("/{user_id}", dependencies=[Depends(get_current_user)])
async def delete_user(user_id: int, db: AsyncSession = Depends(get_db)) -> None:
    ...
'''

SENTENCE = (
    "Add a GET /users/created-per-day endpoint that returns the number of "
    "users created on each of the last 7 days, oldest first."
)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed"],
        check=True,
    )
    return root


def test_routes_are_read_with_their_guards_and_shapes() -> None:
    facts = routes_in_python_file(ROUTER)
    by_path = {f.path: f for f in facts}
    assert set(by_path) == {"/users/count-today", "/users/count-by-domain", "/users/{user_id}"}
    assert by_path["/users/count-today"].auth is None
    assert by_path["/users/count-today"].returns == "an object (UserCount)"
    assert by_path["/users/count-by-domain"].returns == "an unwrapped list (list[DomainCount])"
    assert by_path["/users/{user_id}"].auth == "get_current_user"
    assert by_path["/users/{user_id}"].method == "DELETE"


def test_the_sheet_names_the_siblings_and_who_takes_a_token(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"src/users/router.py": ROUTER})
    sheet = what_the_repository_already_does(str(root), SENTENCE)
    assert sheet is not None
    assert "`src/users/router.py` defines GET /users/count-today, GET /users/count-by-domain" in sheet
    assert "Requires authentication: DELETE /users/{user_id} (get_current_user)" in sheet
    assert "The others declare no authentication dependency." in sheet


def test_no_guard_anywhere_is_said_in_one_sentence(tmp_path: Path) -> None:
    unguarded = ROUTER.replace(', dependencies=[Depends(get_current_user)]', "")
    root = _repo(tmp_path, {"src/users/router.py": unguarded})
    sheet = what_the_repository_already_does(str(root), SENTENCE)
    assert sheet is not None
    assert "None of them declares an authentication dependency." in sheet


def test_a_request_with_no_route_has_nothing_to_say(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"src/users/router.py": ROUTER})
    assert what_the_repository_already_does(str(root), "Make the tests faster.") is None


def test_a_repository_without_the_route_has_nothing_to_say(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "nothing here"})
    assert what_the_repository_already_does(str(root), SENTENCE) is None


def test_a_non_python_file_is_named_but_not_guessed_about(tmp_path: Path) -> None:
    ts = 'router.get("/users/count-today", handler);\nrouter.get("/users/count-by-domain", other);\n'
    root = _repo(tmp_path, {"src/routes/users.ts": ts})
    sheet = what_the_repository_already_does(str(root), SENTENCE)
    assert sheet is not None
    assert "`src/routes/users.ts` mentions /users/count-by-domain, /users/count-today" in sheet
    assert "was not read" in sheet


def test_nothing_here_can_raise(tmp_path: Path) -> None:
    assert what_the_repository_already_does(str(tmp_path / "missing"), SENTENCE) is None
    assert routes_in_python_file("def broken(:") == []


def test_documentation_that_merely_mentions_a_route_is_not_a_route_file(tmp_path: Path) -> None:
    """The first live fact sheet (2026-09-13) listed two
    `.claude/agents/fastapi-*.md` files, because "api" is inside "fastapi".
    They said "not read", so they misled nobody — but they were noise in front
    of the coach, and the words are matched as whole words now."""
    from forge.planning.repository_facts import _looks_like_routes

    assert not _looks_like_routes(".claude/agents/fastapi-specialist-ext.md")
    assert not _looks_like_routes("docs/API.md")
    assert not _looks_like_routes("qa/gates/registry.yaml")
    assert _looks_like_routes("src/users/router.py")
    assert _looks_like_routes("src/api/users.py")
    assert _looks_like_routes("app/controllers/users_controller.rb")
    assert _looks_like_routes("src/routes/users.ts")

    root = _repo(
        tmp_path,
        {
            "src/users/router.py": ROUTER,
            ".claude/agents/fastapi-specialist-ext.md": "mentions /users/count-today and /users/{user_id}\n",
            "docs/API.md": "GET /users/count-today returns a count\n",
        },
    )
    sheet = what_the_repository_already_does(str(root), SENTENCE)
    assert sheet is not None
    assert "src/users/router.py" in sheet
    assert ".claude/agents" not in sheet and "docs/API.md" not in sheet
