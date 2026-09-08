# region MODULE_CONTRACT
# PURPOSE: Verify release workflow contracts and real, offline Commitizen bumps.
# SCOPE: Execute the draft workflow's bump shell in disposable Git repositories;
#        assert version, lockfile, changelog, tag, no-op, and publication contracts.
# KEYWORDS: release automation, Commitizen, uv, changelog, trusted publishing
# endregion MODULE_CONTRACT

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit
import yaml

__all__: list[str] = []

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_OLD_NOTES = "Previously released changes must remain in the full changelog."
_NEW_NOTES = "exercise release automation"


def _workflow(filename: str) -> dict:
    # BaseLoader preserves GitHub's `on` key instead of treating it as a boolean.
    return yaml.load(
        (_ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _shell(
    repo: Path, env: dict[str, str], shell: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", shell],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _bump(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    steps = _workflow("release-draft.yml")["jobs"]["release"]["steps"]
    shell = next(step["run"] for step in steps if step.get("id") == "cz")
    return _shell(repo, env, shell)


def _outputs(env: dict[str, str]) -> dict[str, str]:
    output = Path(env["GITHUB_OUTPUT"])
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.fixture
def release_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    for filename in ("pyproject.toml", "uv.lock", "README.md"):
        shutil.copyfile(_ROOT / filename, repo / filename)
    version = tomlkit.parse((repo / "pyproject.toml").read_text())["project"]["version"]
    (repo / "CHANGELOG.md").write_text(
        f"## v{version} (2025-01-01)\n\n{_OLD_NOTES}\n", encoding="utf-8"
    )
    (repo / ".gitignore").write_text(".CHANGELOG-CURRENT.md\n", encoding="utf-8")
    output = tmp_path / "github-output"
    output.touch()
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        UV_PROJECT_ENVIRONMENT=sys.prefix,
        UV_PYTHON=sys.executable,
        UV_PYTHON_DOWNLOADS="never",
        UV_OFFLINE="1",
        GITHUB_OUTPUT=str(output),
    )
    _git(repo, env, "init", "--initial-branch=master")
    for key, value in (
        ("user.name", "Release Test"),
        ("user.email", "release-test@example.invalid"),
        ("core.hooksPath", str(repo / ".git" / "empty-hooks")),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        _git(repo, env, "config", key, value)
    _git(repo, env, "add", ".")
    _git(repo, env, "commit", "-m", "chore: baseline")
    _git(repo, env, "tag", f"v{version}")
    return repo, env


@pytest.mark.parametrize(
    ("message", "increment"),
    [
        (f"fix: {_NEW_NOTES}", "patch"),
        (f"feat: {_NEW_NOTES}", "minor"),
        (f"feat!: {_NEW_NOTES}", "major"),
        (
            f"refactor: {_NEW_NOTES}\n\nBREAKING CHANGE: remove the old scheduler API",
            "major",
        ),
    ],
)
def test_bump_keeps_version_lockfile_changelog_and_tag_together(
    release_repo: tuple[Path, dict[str, str]], message: str, increment: str
) -> None:
    repo, env = release_repo
    before_project = tomlkit.parse((repo / "pyproject.toml").read_text()).unwrap()
    before_lock = tomlkit.parse((repo / "uv.lock").read_text()).unwrap()
    major, minor, patch = map(int, before_project["project"]["version"].split("."))
    expected_version = {
        "patch": f"{major}.{minor}.{patch + 1}",
        "minor": f"{major}.{minor + 1}.0",
        "major": f"{major + 1}.0.0",
    }[increment]
    _git(repo, env, "commit", "--allow-empty", "-m", message)
    previous_commit = _git(repo, env, "rev-parse", "HEAD")

    result = _bump(repo, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _outputs(env) == {"bumped": "true", "version": expected_version}
    before_project["project"]["version"] = expected_version
    assert (
        tomlkit.parse((repo / "pyproject.toml").read_text()).unwrap() == before_project
    )
    root_packages = [
        package
        for package in before_lock["package"]
        if package["name"] == before_project["project"]["name"]
    ]
    assert len(root_packages) == 1
    root_packages[0]["version"] = expected_version
    # Compare the entire document: dependency packages and lock metadata stay intact.
    assert tomlkit.parse((repo / "uv.lock").read_text()).unwrap() == before_lock
    tag = f"v{expected_version}"
    assert _git(repo, env, "rev-parse", f"{tag}^{{commit}}") == _git(
        repo, env, "rev-parse", "HEAD"
    )
    assert _git(repo, env, "rev-parse", "HEAD^") == previous_commit
    changed_files = _git(repo, env, "diff", "--name-only", "HEAD^", "HEAD").splitlines()
    assert set(changed_files) == {"pyproject.toml", "uv.lock", "CHANGELOG.md"}
    for filename in changed_files:
        assert (
            _git(repo, env, "show", f"{tag}:{filename}")
            == (repo / filename).read_text().strip()
        )
    changelog = (repo / "CHANGELOG.md").read_text()
    increment_notes = (repo / ".CHANGELOG-CURRENT.md").read_text()
    assert _OLD_NOTES in changelog
    assert _NEW_NOTES in changelog
    assert expected_version in increment_notes
    assert _NEW_NOTES in increment_notes
    assert _OLD_NOTES not in increment_notes
    for git_output in (
        "files changed",
        "file changed",
        "[master",
        "git add",
        "Running hook",
        "bump: version",
    ):
        assert git_output not in increment_notes
    assert _git(repo, env, "status", "--porcelain") == ""


@pytest.mark.parametrize("message", [None, "docs: update release instructions"])
def test_no_releasable_commits_is_a_successful_noop(
    release_repo: tuple[Path, dict[str, str]], message: str | None
) -> None:
    repo, env = release_repo
    if message is not None:
        _git(repo, env, "commit", "--allow-empty", "-m", message)
    previous_commit = _git(repo, env, "rev-parse", "HEAD")
    previous_tags = _git(repo, env, "tag", "--list")
    version = tomlkit.parse((repo / "pyproject.toml").read_text())["project"]["version"]

    result = _bump(repo, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _outputs(env) == {"bumped": "false", "version": version}
    assert _git(repo, env, "rev-parse", "HEAD") == previous_commit
    assert _git(repo, env, "tag", "--list") == previous_tags
    assert _git(repo, env, "status", "--porcelain") == ""


def test_bump_does_not_suppress_hook_failure(
    release_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = release_repo
    config_path = repo / "pyproject.toml"
    config = tomlkit.parse(config_path.read_text())
    config["tool"]["commitizen"]["pre_bump_hooks"] = ["exit 42"]
    config_path.write_text(tomlkit.dumps(config), encoding="utf-8")
    _git(repo, env, "add", "pyproject.toml")
    _git(repo, env, "commit", "-m", f"fix: {_NEW_NOTES}")
    previous_commit = _git(repo, env, "rev-parse", "HEAD")
    previous_tags = _git(repo, env, "tag", "--list")

    result = _bump(repo, env)

    assert result.returncode != 0
    assert "bumped" not in _outputs(env)
    assert _git(repo, env, "rev-parse", "HEAD") == previous_commit
    assert _git(repo, env, "tag", "--list") == previous_tags


def test_draft_workflow_checks_out_history_and_gates_side_effects() -> None:
    workflow = _workflow("release-draft.yml")
    job = workflow["jobs"]["release"]
    assert "github.repository == 'tilde-lab/yascheduler'" in job["if"]
    assert workflow["on"]["push"]["branches"] == ["master"]
    steps = job["steps"]
    checkout = next(
        step for step in steps if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["ref"] == "master"
    bump_index = next(i for i, step in enumerate(steps) if step.get("id") == "cz")
    assert any("uv sync --locked" in step.get("run", "") for step in steps[:bump_index])
    assert not any("commitizen-action" in step.get("uses", "") for step in steps)
    build = next(step for step in steps if "uv build" in step.get("run", ""))
    push = next(step for step in steps if "git push" in step.get("run", ""))
    draft = next(
        step
        for step in steps
        if step.get("uses", "").startswith("softprops/action-gh-release@")
    )
    for step in (build, push, draft):
        assert "steps.cz.outputs.bumped == 'true'" in step["if"]
    assert draft["with"]["draft"] == "true"
    assert draft["with"]["tag_name"] == "v${{ steps.cz.outputs.version }}"
    assert draft["with"]["body_path"] == ".CHANGELOG-CURRENT.md"


@pytest.mark.parametrize(
    ("ref", "release_state", "expected_code"),
    [
        ("refs/tags/v1.8.0", "false", 0),
        ("refs/tags/v1.8.0", "true", 1),
        ("refs/tags/v1.8.0", "missing", 42),
        ("refs/tags/v1.8.0", "unknown", 1),
        ("refs/tags/not-a-version", "false", 1),
        ("refs/heads/master", "false", 1),
    ],
)
def test_publication_requires_a_published_release(
    tmp_path: Path, ref: str, release_state: str, expected_code: int
) -> None:
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    gh = bin_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        '[ "$*" = "release view $GITHUB_REF_NAME --json isDraft --jq .isDraft" ] || exit 99\n'
        'if [ "$TEST_RELEASE_STATE" = missing ]; then exit 42; fi\n'
        'printf "%s\\n" "$TEST_RELEASE_STATE"\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(bin_path) + os.pathsep + os.environ["PATH"],
        GITHUB_REF=ref,
        GITHUB_REF_NAME=ref.rsplit("/", 1)[-1],
        TEST_RELEASE_STATE=release_state,
    )
    steps = _workflow("release.yml")["jobs"]["pypi-publish"]["steps"]
    shell = next(
        step["run"] for step in steps if step["name"] == "Verify published release"
    )

    result = _shell(tmp_path, env, shell)

    assert result.returncode == expected_code, result.stdout + result.stderr


@pytest.mark.parametrize("matching", [True, False])
def test_publication_requires_tag_matching_package_version(
    release_repo: tuple[Path, dict[str, str]], matching: bool
) -> None:
    repo, env = release_repo
    version = tomlkit.parse((repo / "pyproject.toml").read_text())["project"]["version"]
    env["GITHUB_REF_NAME"] = f"v{version}" if matching else "v0.0.0"
    steps = _workflow("release.yml")["jobs"]["pypi-publish"]["steps"]
    shell = next(
        step["run"] for step in steps if step["name"] == "Verify package version"
    )

    result = _shell(repo, env, shell)

    assert result.returncode == (0 if matching else 1), result.stdout + result.stderr


def test_published_release_keeps_trusted_publisher_contract() -> None:
    workflow = _workflow("release.yml")
    assert workflow["on"]["release"]["types"] == ["published"]
    job = workflow["jobs"]["pypi-publish"]
    assert job["environment"] == "pypi"
    assert job["permissions"]["id-token"] == "write"
    assert job["permissions"]["contents"] == "read"
    draft_job = _workflow("release-draft.yml")["jobs"]["release"]
    assert job["concurrency"]["group"] != draft_job["concurrency"]["group"]
    assert any(
        step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
        for step in job["steps"]
    )
