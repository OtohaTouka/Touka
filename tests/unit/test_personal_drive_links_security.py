"""代理人個人空間的檔案動詞不跟隨連結（ADR-0293）。

第三方 MCP 或代理人的指令可以把個人空間的根本身、中間一段或最後一段換成連結，指到假的設定資料夾
（``hooks.json``、``hooks/``、``users.json``）或別的代理人的個人空間。read、write、append、
edit、delete、list、find、grep 與個人空間目錄重建都不能讀到、寫到或列出連結另一端。

經檔案動詞（``src.tools.file_verbs``）與真的 ``ResourceManager`` 跑；data 根換到
``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from memory_server.utils.no_follow import LINK_REFUSED_MESSAGE
from src.models.identity import AgentIdentity
from src.tools import file_verbs

from tests.support.links import LINK_FOLLOWED, make_dir_link

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

ALICE = AgentIdentity.for_role(agent_id="Alice", role="Owner")
M1 = AgentIdentity.for_role(agent_id="M1", role="Base")
CANARY = "CANARY = 1"
FAKE_FILES = {
    "hooks.json": '{"hooks": "' + CANARY + '"}',
    "hooks/guard.py": CANARY + "\n",
    "users.json": '{"users": "' + CANARY + '"}',
}


def _fill(folder: Path) -> Path:
    for rel, text in FAKE_FILES.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")
    return folder


def _snapshot(folder: Path) -> dict[str, bytes | None]:
    """資料夾裡全部的項目（資料夾記 None）；連結另一端被寫、被刪、多了東西都看得出來。"""
    return {
        p.relative_to(folder).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in folder.rglob("*")
    }


@pytest.fixture
def world(zone_data_root: Path, tmp_path: Path) -> dict[str, Path]:
    """Alice 與 M1 的個人空間、Bob 的個人空間，以及 data 外面的假設定資料夾。"""
    alice = zone_data_root / "personal_drive" / "Alice"
    (alice / "sub").mkdir(parents=True)
    (alice / "a.txt").write_text("alice\n", encoding="utf-8")
    custom = zone_data_root / "custom" / "M1"
    (custom / "drive").mkdir(parents=True)
    (custom / "config.json").write_text("{}", encoding="utf-8")
    bob = _fill(zone_data_root / "personal_drive" / "Bob")
    fake_config = _fill(tmp_path.resolve() / "fake_config")
    # 換掉 custom/M1 本身時，連結另一端要有 drive/ 才算「讀寫得到」
    fake_custom = tmp_path.resolve() / "fake_custom"
    _fill(fake_custom / "drive")
    return {
        "alice": alice,
        "custom": custom,
        "bob": bob,
        "fake_config": fake_config,
        "fake_custom": fake_custom,
    }


def _verbs(identity: AgentIdentity, prefix: str) -> dict[str, Callable[[], Awaitable[Any]]]:
    """每個檔案動詞各一次，路徑都在 ``prefix`` 底下（``prefix`` 是 ``""`` 時就是根）。"""
    kw: dict[str, Any] = {"scope": "personal", "identity": identity}
    return {
        "read": lambda: file_verbs.verb_read(path=prefix + "hooks.json", **kw),
        "write": lambda: file_verbs.verb_write(path=prefix + "hooks.json", content="PWNED", **kw),
        "write_new": lambda: file_verbs.verb_write(path=prefix + "new.json", content="PWNED", **kw),
        "write_nested": lambda: file_verbs.verb_write(
            path=prefix + "hooks/guard.py", content="PWNED", **kw
        ),
        "append": lambda: file_verbs.verb_write(
            path=prefix + "users.json", content="PWNED", mode="append", **kw
        ),
        "edit": lambda: file_verbs.verb_edit(
            path=prefix + "hooks.json", old_str="hooks", new_str="PWNED", **kw
        ),
        "delete": lambda: file_verbs.verb_delete(path=prefix + "users.json", **kw),
        "list": lambda: file_verbs.verb_list(path=prefix or None, depth=3, **kw),
    }


def _whole_drive_verbs(identity: AgentIdentity) -> dict[str, Callable[[], Awaitable[Any]]]:
    """走整個個人空間的動作：找檔、搜尋與讀目錄（會重建）。"""
    kw: dict[str, Any] = {"scope": "personal", "identity": identity}
    return {
        "find": lambda: file_verbs.verb_find(pattern="*.json", **kw),
        "grep": lambda: file_verbs.verb_grep(query="CANARY", **kw),
        "catalog": lambda: file_verbs.verb_read(path=".touka/catalog.json", **kw),
    }


LISTING_VERBS = frozenset({"list", "find", "grep", "catalog"})


def _assert_not_followed(
    result: Any, target: Path, before: dict[str, bytes | None], *, listing: bool
) -> None:
    """連結另一端沒被改、內容沒被讀出；列表類的結果也沒有列出另一端的名稱。"""
    text = str(result)
    assert _snapshot(target) == before, LINK_FOLLOWED
    assert CANARY not in text, LINK_FOLLOWED
    if listing:
        names = ("hooks.json", "guard.py", "users.json")
        assert not any(name in text for name in names), LINK_FOLLOWED
    assert str(target) not in text


# （誰的個人空間, 換成連結的那一段, 連結另一端）
ROOTS = {
    "personal_to_config": (ALICE, "alice", None, "fake_config"),
    "personal_to_bob": (ALICE, "alice", None, "bob"),
    "custom_drive_to_config": (M1, "custom", "drive", "fake_config"),
    "custom_folder_to_config": (M1, "custom", None, "fake_custom"),
}
ROOT_VERBS = [*_verbs(ALICE, ""), *_whole_drive_verbs(ALICE)]


def _swap_root(world: dict[str, Path], key: str, below: str | None, target: str) -> Path:
    folder = world[key] / below if below else world[key]
    folder.rename(folder.with_name(folder.name + "_old"))
    make_dir_link(folder, world[target])
    return world[target]


@pytest.mark.parametrize("verb", ROOT_VERBS)
@pytest.mark.parametrize("case", ROOTS.values(), ids=ROOTS.keys())
async def test_a_drive_root_swapped_to_a_link_is_refused(world, case, verb):
    identity, key, below, target_key = case
    target = _swap_root(world, key, below, target_key)
    before = _snapshot(target)

    calls = {**_verbs(identity, ""), **_whole_drive_verbs(identity)}
    result = await calls[verb]()

    _assert_not_followed(result, target, before, listing=verb in LISTING_VERBS)
    assert not (target / ".touka").exists(), LINK_FOLLOWED  # 目錄只建在個人空間自己裡面
    assert LINK_REFUSED_MESSAGE in str(result)


@pytest.mark.parametrize("verb", list(_verbs(ALICE, "sub/")))
@pytest.mark.parametrize("target_key", ["fake_config", "bob"])
async def test_a_link_in_the_middle_of_the_path_is_refused(world, target_key, verb):
    target = world[target_key]
    (world["alice"] / "sub").rmdir()
    make_dir_link(world["alice"] / "sub", target)
    before = _snapshot(target)

    result = await _verbs(ALICE, "sub/")[verb]()

    _assert_not_followed(result, target, before, listing=verb == "list")
    assert LINK_REFUSED_MESSAGE in str(result)


LAST_SEGMENT = {
    "read": lambda kw: file_verbs.verb_read(path="peek", **kw),
    "write": lambda kw: file_verbs.verb_write(path="peek", content="PWNED", **kw),
    "append": lambda kw: file_verbs.verb_write(path="peek", content="PWNED", mode="append", **kw),
    "list": lambda kw: file_verbs.verb_list(path="peek", **kw),
}


@pytest.mark.parametrize("verb", LAST_SEGMENT)
@pytest.mark.parametrize("target_key", ["fake_config", "bob"])
async def test_a_link_as_the_last_segment_is_refused(world, target_key, verb):
    target = world[target_key]
    make_dir_link(world["alice"] / "peek", target)
    before = _snapshot(target)

    result = await LAST_SEGMENT[verb]({"scope": "personal", "identity": ALICE})

    _assert_not_followed(result, target, before, listing=verb == "list")
    assert "Error" in str(result)


@pytest.mark.parametrize("target_key", ["fake_config", "bob"])
async def test_deleting_a_link_removes_only_the_link(world, target_key):
    target = world[target_key]
    make_dir_link(world["alice"] / "peek", target)
    before = _snapshot(target)

    result = await file_verbs.verb_delete(scope="personal", path="peek", identity=ALICE)

    assert _snapshot(target) == before, LINK_FOLLOWED
    assert str(result).startswith("Success")
    assert not (world["alice"] / "peek").exists()
    assert not (world["alice"] / "peek").is_symlink()


@pytest.mark.parametrize("target_key", ["fake_config", "bob"])
async def test_list_find_grep_and_catalog_do_not_enter_an_inner_link(world, target_key):
    target = world[target_key]
    make_dir_link(world["alice"] / "peek", target)
    before = _snapshot(target)
    kw: dict[str, Any] = {"scope": "personal", "identity": ALICE}

    listing = await file_verbs.verb_list(depth=3, **kw)
    found = await file_verbs.verb_find(pattern="*.json", **kw)
    grep = await file_verbs.verb_grep(query="CANARY", **kw)
    catalog = await file_verbs.verb_read(path=".touka/catalog.json", **kw)

    for result in (listing, found, grep):
        _assert_not_followed(result, target, before, listing=True)
    assert "peek (link, not followed)" in listing
    files = json.loads(catalog.split("\n", 1)[1])["files"]
    assert [entry["path"] for entry in files] == ["a.txt"], LINK_FOLLOWED
    assert not (target / ".touka").exists(), LINK_FOLLOWED


async def test_drive_verbs_still_work_without_links(world):
    kw: dict[str, Any] = {"scope": "personal", "identity": ALICE}
    assert str(await file_verbs.verb_write(path="sub/n.txt", content="one\n", **kw)).startswith(
        "Success"
    )
    await file_verbs.verb_write(path="sub/n.txt", content="two\n", mode="append", **kw)
    assert "one\ntwo\n" in await file_verbs.verb_read(path="sub/n.txt", **kw)
    edited = await file_verbs.verb_edit(path="sub/n.txt", old_str="two", new_str="three", **kw)
    assert "Edited sub/n.txt" in str(edited)
    assert "sub/n.txt" in await file_verbs.verb_find(pattern="*.txt", **kw)
    assert "sub/n.txt:2: three" in await file_verbs.verb_grep(query="three", **kw)
    assert "n.txt" in await file_verbs.verb_list(path="sub", **kw)
    absolute = str(world["alice"] / "sub" / "n.txt")
    assert "one\nthree\n" in await file_verbs.verb_read(path=absolute, **kw)
    assert str(await file_verbs.verb_delete(path="sub/n.txt", **kw)).startswith("Success")
    assert not (world["alice"] / "sub" / "n.txt").exists()


@pytest.mark.parametrize("path", ["../Bob/hooks.json", "sub/../../Bob/hooks.json"])
async def test_escaping_to_another_drive_keeps_the_old_message(world, path):
    before = _snapshot(world["bob"])
    kw: dict[str, Any] = {"scope": "personal", "identity": ALICE}

    read = await file_verbs.verb_read(path=path, **kw)
    write = await file_verbs.verb_write(path=path, content="PWNED", **kw)

    assert _snapshot(world["bob"]) == before
    assert CANARY not in read
    for result in (read, write):
        assert "Path escape detected." in result


async def test_an_absolute_path_into_another_drive_is_an_escape(world):
    before = _snapshot(world["bob"])
    path = str(world["bob"] / "hooks.json")

    result = await file_verbs.verb_write(
        scope="personal", path=path, content="PWNED", identity=ALICE
    )

    assert _snapshot(world["bob"]) == before
    assert "Path escape detected." in result
