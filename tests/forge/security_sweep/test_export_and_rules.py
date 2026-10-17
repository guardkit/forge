"""The export from the object store (Codex R12), the sweep's own fetch, the
factory's Compose rules (Codex R8) and the Opengrep rule-file builder."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from forge.security_sweep.repository import export_committed_files, fetch_mirror

from tests.forge.git.network_cases import NetLab, netlab  # noqa: F401 — the fixture

from .conftest import COMPOSE_RULES, REPO_ROOT, git, make_project

BUILDER = REPO_ROOT / "deploy" / "security" / "build_opengrep_rules.py"


class TestTheExport:
    def test_export_ignore_cannot_drop_a_lockfile_or_a_compose_file(self, tmp_path: Path) -> None:
        project = make_project(
            tmp_path,
            "app",
            {
                ".gitattributes": "web/package-lock.json export-ignore\ncompose.yaml export-ignore\n*.txt text eol=crlf\n",
                "web/package-lock.json": '{"lockfileVersion": 3}\n',
                "compose.yaml": "services:\n  app:\n    privileged: true\n",
                "notes.txt": "one\ntwo\n",
            },
        )
        commit = git(project.work, "rev-parse", "HEAD")
        out = tmp_path / "out"
        out.mkdir()
        report = export_committed_files(project.mirror, commit, out)
        assert (out / "web" / "package-lock.json").read_text() == '{"lockfileVersion": 3}\n'
        assert (out / "compose.yaml").exists()
        # Exact committed bytes: no attribute-driven line-ending conversion.
        assert (out / "notes.txt").read_bytes() == b"one\ntwo\n"
        assert report.files == 4

    def test_symlinks_and_submodules_are_skipped_and_recorded(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, "app", {"real.txt": "real\n"})
        os.symlink("/etc/passwd", project.work / "link")
        git(project.work, "add", "link")
        sub_commit = git(project.work, "rev-parse", "HEAD")
        git(project.work, "update-index", "--add", "--cacheinfo", f"160000,{sub_commit},vendor/lib")
        project.commit("link and submodule", stage=False)
        out = tmp_path / "out"
        out.mkdir()
        report = export_committed_files(project.mirror, git(project.work, "rev-parse", "HEAD"), out)
        assert report.symlinks == ["link"]
        assert report.submodules == ["vendor/lib"]
        assert not (out / "link").exists() and not os.path.islink(out / "link")
        assert not (out / "vendor").exists()

    def test_the_scanners_settings_files_are_left_out(self, tmp_path: Path) -> None:
        project = make_project(
            tmp_path,
            "app",
            {
                ".gitleaks.toml": "[allowlist]\npaths=['.*']\n",
                ".gitleaksignore": "x\n",
                "web/osv-scanner.toml": "[[IgnoredVulns]]\nid = 'GHSA-x'\n",
                ".semgrepignore": "*\n",
                "app.py": "x = 1\n",
            },
        )
        out = tmp_path / "out"
        out.mkdir()
        report = export_committed_files(project.mirror, git(project.work, "rev-parse", "HEAD"), out)
        assert sorted(report.settings_left_out) == [
            ".gitleaks.toml", ".gitleaksignore", ".semgrepignore", "web/osv-scanner.toml",
        ]
        assert sorted(p.name for p in out.rglob("*") if p.is_file()) == ["app.py"]


class TestTheSweepsOwnFetch:
    def test_it_fetches_the_default_branch_into_its_own_mirror(self, tmp_path: Path, netlab: NetLab) -> None:
        mirrors = tmp_path / "sweep" / "mirrors"
        got = fetch_mirror(netlab.recorded, mirrors)
        assert got.refusal is None
        assert got.commit == netlab.good_commit
        assert got.path is not None and got.path.parent == mirrors
        out = tmp_path / "out"
        out.mkdir()
        export_committed_files(got.path, got.commit, out)
        assert (out / "good.md").read_text() == "good\n"
        # Nothing went to the coordinator's own fetch copies.
        assert not netlab.copies.exists() or not any(netlab.copies.iterdir())

    def test_an_address_that_is_not_plain_https_is_refused(self, tmp_path: Path) -> None:
        got = fetch_mirror("https://user:s3cretvalue@example.invalid/org/app.git", tmp_path)
        assert got.refusal and "s3cretvalue" not in got.refusal


OPENGREP = shutil.which("opengrep") or os.environ.get("FORGE_TEST_OPENGREP")


def _opengrep(target: Path) -> list[tuple[str, str, int]]:
    home = target.parent / "home"
    home.mkdir(exist_ok=True)
    done = subprocess.run(
        [str(OPENGREP), "scan", "--config", str(COMPOSE_RULES), "--disable-nosem",
         "--no-git-ignore", "--disable-version-check", "--json", "--quiet", str(target)],
        capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": str(home)}, timeout=300,
    )
    assert done.returncode in (0, 1), done.stderr[-2000:]
    results = json.loads(done.stdout)["results"]
    return sorted(
        (r["check_id"].rsplit(".", 1)[-1], Path(r["path"]).name, r["start"]["line"]) for r in results
    )


RISKY = """services:
  risky:
    image: x
    privileged: true
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    security_opt:
      - seccomp:unconfined
      - apparmor=unconfined
      - label:disable
    network_mode: host
    pid: "host"
    cap_add:
      - SYS_ADMIN
      - ALL
  safe:
    image: y
    privileged: false
    volumes:
      - ./data:/data
      - type: bind
        source: ./config
        target: /config
    security_opt:
      - no-new-privileges:true
    network_mode: bridge
    pid: service:risky
    cap_add:
      - NET_BIND_SERVICE
    cap_drop:
      - ALL
"""


@pytest.mark.skipif(not OPENGREP, reason="opengrep is not installed (set FORGE_TEST_OPENGREP)")
class TestTheFactoryComposeRules:
    """R8: each rule matches the risky setting and not the safe one, with and
    without a ``version:`` field, and only in ``*compose*.y*ml`` files."""

    EXPECTED = [
        ("factory-compose-cap-add-all", 14),
        ("factory-compose-cap-add-all", 15),
        ("factory-compose-docker-socket", 6),
        ("factory-compose-network-host", 11),
        ("factory-compose-pid-host", 12),
        ("factory-compose-privileged", 4),
        ("factory-compose-security-opt-disabled", 8),
        ("factory-compose-security-opt-disabled", 9),
        ("factory-compose-security-opt-disabled", 10),
    ]

    @pytest.mark.parametrize("with_version", [False, True])
    def test_each_rule_matches_risky_and_not_safe(self, tmp_path: Path, with_version: bool) -> None:
        target = tmp_path / "tree"
        target.mkdir()
        text = ('version: "3.8"\n' + RISKY) if with_version else RISKY
        (target / "docker-compose.yml").write_text(text)
        shift = 1 if with_version else 0
        assert _opengrep(target) == sorted(
            (rule, "docker-compose.yml", line + shift) for rule, line in self.EXPECTED
        )

    def test_the_long_volume_form_is_found_too(self, tmp_path: Path) -> None:
        target = tmp_path / "tree"
        target.mkdir()
        (target / "compose.yaml").write_text(
            "services:\n  a:\n    volumes:\n      - type: bind\n        source: /var/run/docker.sock\n        target: /var/run/docker.sock\n"
        )
        assert _opengrep(target) == [("factory-compose-docker-socket", "compose.yaml", 5)]

    def test_files_not_named_compose_are_left_alone(self, tmp_path: Path) -> None:
        target = tmp_path / "tree"
        target.mkdir()
        (target / "stack.yaml").write_text(RISKY)
        assert _opengrep(target) == []


def _builder():
    spec = importlib.util.spec_from_file_location("build_opengrep_rules", BUILDER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestTheRuleFileBuilder:
    def _checkout(self, root: Path) -> Path:
        files = {
            "python/security/a.yaml": "rules:\n- id: keep-a\n  message: m\n  metadata: {category: security}\n",
            "python/style/b.yaml": "rules:\n- id: drop-b\n  message: m\n  metadata: {category: best-practice}\n",
            "python/security/a.test.yaml": "rules:\n- id: keep-a\n  metadata: {category: security}\n",
            ".github/security.yaml": "rules:\n- id: hidden\n  metadata: {category: security}\n",
            "yaml/x/c.yaml": "services: {}\n",
            "broken.yaml": "rules: [\n",
            "go/security/d.yaml": "rules:\n- id: keep-d\n  message: m\n  metadata: {category: security}\n- id: drop-d\n  metadata: {}\n",
        }
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return root

    def test_only_security_rules_from_rule_files_are_kept(self, tmp_path: Path) -> None:
        builder = _builder()
        rules = builder.security_rules(self._checkout(tmp_path / "rules"))
        assert [r["id"] for r in rules] == ["keep-d", "keep-a"]

    def test_the_output_is_the_same_bytes_every_time(self, tmp_path: Path) -> None:
        builder = _builder()
        root = self._checkout(tmp_path / "rules")
        one, two = tmp_path / "one.yaml", tmp_path / "two.yaml"
        assert builder.main(["x", str(root), str(one)]) == 0
        assert builder.main(["x", str(root), str(two)]) == 0
        assert one.read_bytes() == two.read_bytes()
        assert json.loads(one.read_text())["rules"][0]["id"] == "keep-d"

    def test_no_rules_is_a_failure_not_an_empty_file(self, tmp_path: Path) -> None:
        builder = _builder()
        empty = tmp_path / "empty"
        empty.mkdir()
        assert builder.main(["x", str(empty), str(tmp_path / "out.yaml")]) == 1
        assert not (tmp_path / "out.yaml").exists()
