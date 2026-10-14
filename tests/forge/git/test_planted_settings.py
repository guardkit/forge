"""Positive controls: the planted settings really do fire under plain git.

These pass today and must keep passing. Each one runs a git command the way
Forge ran it before the change (plain ``git`` with the inherited environment,
inside the project's clone) and shows the planted program running with the
factory's secret visible, or the planted credential or header reaching the
server. They prove the fixtures plant something real on this machine's git,
so that the helper tests, which expect nothing, are not passing by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.forge.git.git_marker_lab import SECRET_NAME, SECRET_VALUE, Lab
from tests.forge.git.network_cases import NETWORK_CASES, NetCase, NetLab, netlab  # noqa: F401
from tests.forge.git.test_project_git import FORGE_LOCAL_CASES, Case, _run_case, _plain_runner


@pytest.fixture()
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    made = Lab(tmp_path / "lab")
    monkeypatch.setenv("HOME", str(made.home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS",
                 "GIT_CONFIG_COUNT", "GIT_EXTERNAL_DIFF", "GIT_ASKPASS", "SSH_ASKPASS",
                 "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.delenv(name, raising=False)
    return made


@pytest.mark.parametrize("case", NETWORK_CASES, ids=[c.id for c in NETWORK_CASES])
def test_planted_network_settings_fire_with_plain_git(netlab: NetLab, case: NetCase) -> None:
    clone = netlab.clone()
    case.plant(netlab, clone)
    netlab.lab.fired()
    netlab.lab.plain(clone, *case.plain(netlab))
    fired = netlab.lab.fired()
    assert case.expect <= set(Lab.names(fired)), fired
    if case.expect:
        assert Lab.saw_secret(fired), fired
    if case.server_receives:
        assert case.server_receives in netlab.http.passwords(), netlab.http.seen
    if case.server_header:
        assert any(case.server_header in v for v in netlab.http.header_values("x-project-header"))
        assert any("planted-token" in v for v in netlab.http.header_values("authorization"))


@pytest.mark.parametrize("case", FORGE_LOCAL_CASES, ids=[c.id for c in FORGE_LOCAL_CASES])
def test_planted_local_settings_fire_with_plain_git(lab: Lab, case: Case) -> None:
    _run_case(lab, case, _plain_runner(lab))
    fired = lab.fired()
    assert case.expect <= set(Lab.names(fired)), fired
    assert Lab.saw_secret(fired), fired
