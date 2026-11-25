"""專用空間的結構層（ADR-0332）：只看路徑，回 outside、ok 或 refused.

- 錨點只從部署設定來（``configured_projects_root``），和工作區設定脫鉤；
- 不在錨點底下（不含錨點本身）是 outside，和現在完全一樣；
- 錨點本身、第二層以下、不安全的名稱、路上有連結（連進來或連出去、含 ``..``）都是 refused；
- 這個部署沒有專用空間時，路徑用了 ``touka-projects`` 這個名稱也是 refused（不悄悄改用別的方式）。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from src.services.isolation import profiles
from src.services.isolation import project_space as ps

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def anchor(tmp_path: Path) -> Path:
    path = (tmp_path / "workspace" / "touka-projects").resolve()
    path.mkdir(parents=True)
    return path


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("this system cannot create symbolic links")


def test_no_project_is_outside(anchor):
    assert ps.structural_scope(None, anchor) == ps.OUTSIDE
    assert ps.structural_scope(None, None).kind == "outside"


def test_a_first_level_folder_is_a_dedicated_project(anchor):
    (anchor / "repo").mkdir()
    scope = ps.structural_scope(anchor / "repo", anchor)
    assert (scope.kind, scope.name, scope.path) == ("ok", "repo", anchor / "repo")
    assert scope.reason is None
    assert scope.dedicated
    assert ps.structural_scope(str(anchor / "repo"), anchor).name == "repo"


def test_a_folder_that_does_not_exist_yet_is_still_judged_by_its_path(anchor):
    assert ps.structural_scope(anchor / "later", anchor).kind == "ok"


def test_projects_outside_the_space_keep_todays_rules(anchor, tmp_path):
    other = (tmp_path / "workspace" / "myrepo").resolve()
    other.mkdir()
    sibling = (tmp_path / "workspace" / "touka-projects-old" / "repo").resolve()
    sibling.mkdir(parents=True)
    for path in (other, sibling, tmp_path.resolve()):
        scope = ps.structural_scope(path, anchor)
        assert scope == ps.OUTSIDE and not scope.dedicated


@pytest.mark.parametrize(
    ("relative", "reason"),
    [
        ("", ps.PROJECT_SPACE_ROOT),
        ("repo/src", ps.PROJECT_NOT_TOP_LEVEL),
        ("repo/a/b", ps.PROJECT_NOT_TOP_LEVEL),
        (" lead", ps.PROJECT_NAME_UNSAFE),
    ],
)
def test_the_anchor_deeper_levels_and_unsafe_names_are_refused(anchor, relative, reason):
    target = anchor / relative if relative else anchor
    target.mkdir(parents=True, exist_ok=True)
    scope = ps.structural_scope(target, anchor)
    assert (scope.kind, scope.reason, scope.name, scope.path) == ("refused", reason, None, None)
    assert scope.dedicated


def test_dot_dot_inside_the_space_is_refused(anchor):
    (anchor / "a").mkdir()
    (anchor / "b").mkdir()
    scope = ps.structural_scope(f"{anchor}{os.sep}a{os.sep}..{os.sep}b", anchor)
    assert (scope.kind, scope.reason) == ("refused", ps.PROJECT_LINKED)


def test_a_link_inside_the_space_pointing_out_is_refused(anchor, tmp_path):
    outside = (tmp_path / "secrets").resolve()
    outside.mkdir()
    _symlink(anchor / "repo", outside)
    scope = ps.structural_scope(anchor / "repo", anchor)
    assert (scope.kind, scope.reason) == ("refused", ps.PROJECT_LINKED)


def test_a_link_from_elsewhere_into_the_space_is_refused(anchor, tmp_path):
    (anchor / "repo").mkdir()
    link = tmp_path / "shortcut"
    _symlink(link, anchor / "repo")
    scope = ps.structural_scope(link, anchor)
    assert (scope.kind, scope.reason) == ("refused", ps.PROJECT_LINKED)


@pytest.mark.parametrize("name", ["touka-projects", "Touka-Projects"])
def test_without_a_space_the_name_is_refused(tmp_path, name):
    target = (tmp_path / "workspace" / name / "repo").resolve()
    target.mkdir(parents=True)
    scope = ps.structural_scope(target, None)
    assert (scope.kind, scope.reason) == ("refused", ps.PROJECT_SPACE_UNAVAILABLE)
    assert ps.structural_scope((tmp_path / "workspace").resolve(), None) == ps.OUTSIDE


def test_the_anchor_comes_only_from_the_deployment_setting(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles.settings, "TOUKA_PROJECTS_DIR", "")
    assert profiles.configured_projects_root() is None
    place = tmp_path / "touka-projects"
    monkeypatch.setattr(profiles.settings, "TOUKA_PROJECTS_DIR", str(place))
    assert profiles.configured_projects_root() == place.resolve()


def test_literal_path_is_the_profiles_literal():
    assert profiles.literal_path("a/../b") == profiles._literal("a/../b")
