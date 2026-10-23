"""ToukaHeartbeat/permission_init.py contract; automatic cases use synthetic metadata only."""

import ast
import copy
import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.utils.touka_dirs import SCAN_SKIP_NAMES

ROOT = Path(__file__).parent.parent.parent


def test_q42_permission_helper_is_separate_from_product_bootstrap():
    helper = ROOT / "ToukaHeartbeat" / "permission_init.py"
    assert helper.is_file(), "A standalone permission-only helper must exist before deployment."
    text = helper.read_text(encoding="utf-8")
    assert "from awaken" not in text and "import awaken" not in text


@pytest.fixture
def permission():
    path = ROOT / "ToukaHeartbeat" / "permission_init.py"
    assert path.is_file(), "Permission algorithm is not implemented yet."
    spec = importlib.util.spec_from_file_location("q42_permission_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SyntheticMetadata:
    """In-memory inode model: no filesystem content or ownership calls."""

    def __init__(self):
        self.platform = "linux"
        self.credentials = (0, 1001, 1001, 11)
        self.rows = {}
        self.handles = {}
        self.next_fd = 10
        self.mutations = []
        self.closes = []
        self.clock = 0.0
        self.hook = None
        self.fault = None
        self.extra_mounts = ""
        self.mount_calls = 0
        self.mount_change = False
        self.opened = []
        self.xattr_keys = set()
        for role in ("data", "hf_cache", "proxy_config", "heartbeat_config"):
            self.add((role,), stat.S_IFDIR | 0o000, nlink=2)
        self.add(("data", "ordinary"), stat.S_IFREG | 0o000)
        self.add(("data", "executable"), stat.S_IFREG | 0o051)

    def add(self, key, mode, nlink=1):
        self.rows[key] = SimpleNamespace(
            st_dev=1,
            st_ino=len(self.rows) + 1,
            st_mode=mode,
            st_nlink=nlink,
            st_uid=0,
            st_gid=0,
            st_size=17,
            st_mtime_ns=5,
            st_ctime_ns=7,
        )

    def account(self):
        return self.credentials

    def monotonic(self):
        return self.clock

    def mount_points(self):
        return {key[0]: "/synthetic/" + key[0] for key in self.rows if len(key) == 1}

    def mountinfo(self, limit):
        self.mount_calls += 1
        if self.mount_change and self.mount_calls > 1:
            self.extra_mounts = "90 1 1:0 /new /synthetic/data/nested rw - ext4 synthetic rw\n"
        text = (
            "".join(
                f"{index + 10} 1 1:0 /backing/{role} /synthetic/{role} rw - ext4 synthetic rw\n"
                for index, role in enumerate(self.mount_points())
            )
            + self.extra_mounts
        )
        return text.encode()

    def device(self, value):
        return (1, 0)

    def mount_id(self, fd):
        role = self.handles[fd][0]
        return 10 + ("data", "hf_cache", "proxy_config", "heartbeat_config").index(role)

    def _open(self, key):
        if self.hook:
            self.hook(self, key)
        if key not in self.rows:
            raise FileNotFoundError
        self.opened.append(key)
        self.next_fd += 1
        self.handles[self.next_fd] = key
        return self.next_fd

    def open_root(self, role):
        return self._open((role,))

    def open_child(self, parent, name, directory):
        return self._open(self.handles[parent] + (name,))

    def fstat(self, fd):
        return copy.copy(self.rows[self.handles[fd]])

    def lstat(self, parent, name):
        return copy.copy(self.rows[self.handles[parent] + (name,)])

    def names(self, fd):
        parent = self.handles[fd]
        return iter([key[-1] for key in self.rows if key[:-1] == parent])

    def xattrs(self, fd):
        if self.fault == "acl":
            return ["system.posix_acl_access"]
        return ["user.synthetic"] if self.handles[fd] in self.xattr_keys else []

    def fchown(self, fd, uid, gid):
        key = self.handles[fd]
        self.mutations.append(("chown", key))
        row = self.rows[key]
        row.st_uid, row.st_gid = uid, gid
        row.st_ctime_ns += 1
        if self.fault == "chown-after-mutation":
            raise OSError("synthetic-secret-must-not-escape")

    def fchmod(self, fd, mode):
        if self.fault == "chmod":
            raise OSError("synthetic-secret-must-not-escape")
        key = self.handles[fd]
        self.mutations.append(("chmod", key))
        self.rows[key].st_mode = stat.S_IFMT(self.rows[key].st_mode) | mode
        self.rows[key].st_ctime_ns += 1

    def close(self, fd):
        self.closes.append(fd)
        del self.handles[fd]
        if self.fault == "close":
            raise OSError("synthetic-secret-must-not-escape")


def test_q42_algorithm_preserves_content_metadata_and_execute_intent(permission):
    backend = SyntheticMetadata()
    before = copy.deepcopy(backend.rows)
    result = permission.run_permission_init(backend)
    assert result["status"] == "PASS" and result["nodes"] == 6
    assert not backend.handles
    for key, row in backend.rows.items():
        assert (row.st_uid, row.st_gid) == (1001, 1001)
        assert (row.st_size, row.st_mtime_ns) == (before[key].st_size, before[key].st_mtime_ns)
        assert stat.S_IMODE(row.st_mode) == (
            0o700 if len(key) == 1 else 0o751 if key[-1] == "executable" else 0o600
        )
    backend.mutations.clear()
    assert permission.run_permission_init(backend)["status"] == "PASS"
    assert backend.mutations == []


@pytest.mark.parametrize(
    "credentials",
    [
        (1001, 1001, 1001, 11),
        (0, 999, 1001, 11),
        (0, 1001, 999, 11),
        (0, 1001, 1001, 10),
        (0, 1001, 1001, 9),
        (0, 1001, 1001, 3),
        (0, 1001, 1001, 15),
    ],
)
def test_q42_algorithm_rejects_identity_and_capability_drift_before_mutation(
    permission, credentials
):
    backend = SyntheticMetadata()
    backend.credentials = credentials
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and result["stage"] == "preflight"
    assert not backend.mutations and not backend.handles


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "acl",
        "root-symlink",
        "root-setuid",
        "root-setgid",
        "root-xattr",
        "workspace-root-symlink",
        "workspace-root-xattr",
        "workspace-root-setgid",
        "root-alias",
        "nested-mount",
        "backing-alias",
        "unsupported-os",
    ],
)
def test_q42_preflight_rejects_unsafe_metadata_with_zero_changes(permission, defect):
    backend = SyntheticMetadata()
    root = backend.rows[("data",)]
    if defect == "missing":
        del backend.rows[("heartbeat_config",)]
    elif defect == "acl":
        backend.fault = "acl"
    elif defect == "root-symlink":
        root.st_mode = stat.S_IFLNK | 0o777
    elif defect == "root-setuid":
        root.st_mode |= stat.S_ISUID
    elif defect == "root-setgid":
        root.st_mode |= stat.S_ISGID
    elif defect == "root-xattr":
        backend.xattr_keys.add(("hf_cache",))
    elif defect == "workspace-root-symlink":
        backend.add(("data", "personal_drive"), stat.S_IFLNK | 0o777)
    elif defect == "workspace-root-xattr":
        backend.add(("data", "mcp_servers"), stat.S_IFDIR | 0o700, nlink=2)
        backend.xattr_keys.add(("data", "mcp_servers"))
    elif defect == "workspace-root-setgid":
        backend.add(("data", "personal_drive"), stat.S_IFDIR | stat.S_ISGID | 0o700, nlink=2)
    elif defect == "root-alias":
        backend.rows[("proxy_config",)].st_ino = backend.rows[("data",)].st_ino
    elif defect == "nested-mount":
        backend.extra_mounts = "90 10 1:0 /other /synthetic/data/nested rw - ext4 synthetic rw\n"
    elif defect == "backing-alias":
        backend.extra_mounts = "90 1 1:0 /backing/data /elsewhere rw - ext4 synthetic rw\n"
    else:
        backend.platform = "win32"
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and result["stage"] == "preflight"
    assert not backend.mutations and not backend.handles


SKIP_KINDS = ("symlink", "fifo", "socket", "device", "hardlink", "setid_file", "xattr", "tool_dir")


@pytest.mark.parametrize(
    "mode,nlink,kind",
    [
        (stat.S_IFLNK | 0o777, 1, "symlink"),
        (stat.S_IFIFO | 0o600, 1, "fifo"),
        (stat.S_IFSOCK | 0o600, 1, "socket"),
        (stat.S_IFCHR | 0o600, 1, "device"),
        (stat.S_IFBLK | 0o600, 1, "device"),
        (stat.S_IFREG | 0o600, 2, "hardlink"),
        (stat.S_IFREG | stat.S_ISUID | 0o700, 1, "setid_file"),
        (stat.S_IFREG | stat.S_ISGID | 0o700, 1, "setid_file"),
    ],
    ids=["symlink", "fifo", "socket", "char", "block", "hardlink", "setuid", "setgid"],
)
def test_special_entries_are_skipped_without_being_opened(permission, mode, nlink, kind):
    backend = SyntheticMetadata()
    backend.add(("hf_cache", "snapshot-link"), mode, nlink=nlink)
    before = copy.deepcopy(backend.rows[("hf_cache", "snapshot-link")])
    result = permission.run_permission_init(backend)
    assert result["status"] == "PASS"
    assert ("hf_cache", "snapshot-link") not in backend.opened
    assert all(key != ("hf_cache", "snapshot-link") for _, key in backend.mutations)
    assert backend.rows[("hf_cache", "snapshot-link")] == before
    assert result["skipped"] == {name: int(name == kind) for name in SKIP_KINDS}
    assert backend.rows[("data", "ordinary")].st_uid == 1001
    assert not backend.handles


def test_xattr_entries_are_read_for_metadata_but_never_changed_or_descended(permission):
    backend = SyntheticMetadata()
    backend.add(("data", "labelled"), stat.S_IFREG | 0o600)
    backend.add(("data", "labelled_dir"), stat.S_IFDIR | 0o700, nlink=2)
    backend.add(("data", "labelled_dir", "inner"), stat.S_IFREG | 0o600)
    backend.xattr_keys |= {("data", "labelled"), ("data", "labelled_dir")}
    result = permission.run_permission_init(backend)
    assert result["status"] == "PASS"
    assert result["skipped"]["xattr"] == 2
    touched = {key for _, key in backend.mutations}
    assert not touched & {("data", "labelled"), ("data", "labelled_dir")}
    assert ("data", "labelled_dir", "inner") not in backend.opened
    assert backend.rows[("data", "labelled")].st_uid == 0
    assert not backend.handles


def test_setgid_directory_below_a_root_is_processed(permission):
    backend = SyntheticMetadata()
    backend.add(("data", "shared"), stat.S_IFDIR | stat.S_ISGID | 0o770, nlink=2)
    result = permission.run_permission_init(backend)
    assert result["status"] == "PASS"
    row = backend.rows[("data", "shared")]
    assert (row.st_uid, row.st_gid) == (1001, 1001)
    assert stat.S_IMODE(row.st_mode) == stat.S_ISGID | 0o770


def test_tool_directories_are_skipped_only_inside_agent_workspaces(permission):
    backend = SyntheticMetadata()
    alice = ("data", "personal_drive", "Alice")
    modules = (*alice, "project", "node_modules")
    board = (*alice, "missions", "board.json")
    for key in (
        ("data", "personal_drive"),
        alice,
        (*alice, "project"),
        modules,
        (*alice, "missions"),
        ("data", "mcp_servers"),
        ("data", "mcp_servers", ".venv"),
        ("data", "soul"),
        ("data", "soul", "node_modules"),
    ):
        backend.add(key, stat.S_IFDIR | 0o700, nlink=2)
    backend.add((*modules, "x.js"), stat.S_IFREG)
    backend.add(board, stat.S_IFREG)
    result = permission.run_permission_init(backend)
    assert result["status"] == "PASS"
    assert result["skipped"]["tool_dir"] == 2
    skipped = {modules, (*modules, "x.js"), ("data", "mcp_servers", ".venv")}
    assert not skipped & set(backend.opened)
    assert not skipped & {key for _, key in backend.mutations}
    assert backend.rows[board].st_uid == 1001
    assert backend.rows[("data", "soul", "node_modules")].st_uid == 1001


def test_skipped_entries_count_toward_the_node_limit(permission):
    backend = SyntheticMetadata()
    for index in range(3):
        backend.add(("hf_cache", f"link-{index}"), stat.S_IFLNK | 0o777)
    result = permission.run_permission_init(backend, limits=permission.Limits(nodes=8))
    assert result["status"] == "FAIL" and result["reason"] == "LIMIT_EXCEEDED"
    assert not backend.mutations and not backend.handles


@pytest.mark.parametrize("change", ["to_regular_file", "to_fifo", "retimed"])
def test_skipped_entry_changed_after_inventory_fails_closed(permission, change):
    backend = SyntheticMetadata()
    backend.add(("hf_cache", "snapshot-link"), stat.S_IFLNK | 0o777)
    original = backend.mountinfo

    def replace_link(limit):
        raw = original(limit)
        if backend.mount_calls == 2:
            row = backend.rows[("hf_cache", "snapshot-link")]
            if change == "to_regular_file":
                row.st_mode = stat.S_IFREG | 0o600
            elif change == "to_fifo":
                row.st_mode = stat.S_IFIFO | 0o600
            else:
                row.st_mtime_ns += 1
        return raw

    backend.mountinfo = replace_link
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL"
    assert all(key != ("hf_cache", "snapshot-link") for _, key in backend.mutations)
    assert not backend.handles


def test_result_reports_every_skip_kind_without_names(permission, monkeypatch, capsys):
    backend = SyntheticMetadata()
    backend.add(("data", "private-link-name"), stat.S_IFLNK | 0o777)
    monkeypatch.setattr(permission, "NativeMetadata", lambda: backend)
    argv = ["--profile", "local-permissions-v1", "--expected-uid", "1001", "--expected-gid", "1001"]
    assert permission.main(argv) == 0
    output = capsys.readouterr().out
    assert '"symlink": 1' in output and "private-link-name" not in output
    for kind in SKIP_KINDS:
        assert f'"{kind}"' in output


def test_tool_directory_names_match_the_project_scan_skip_list(permission):
    assert permission.TOOL_DIR_NAMES == SCAN_SKIP_NAMES


@pytest.mark.parametrize("fault", ["chmod", "chown-after-mutation", "close"])
def test_q42_partial_and_cleanup_failures_never_report_success(permission, fault):
    backend = SyntheticMetadata()
    backend.fault = fault
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL"
    assert "synthetic-secret" not in str(result)
    if fault != "close":
        assert result["reason"] == "PARTIAL_PERMISSION_FAILURE"
        assert result["mutation_uncertain"] is True
    assert not backend.handles


def test_q42_mount_drift_blocks_apply(permission):
    backend = SyntheticMetadata()
    backend.mount_change = True
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and not backend.mutations


@pytest.mark.parametrize(
    "field,value",
    [("nodes", 5), ("depth", 0), ("manifest_bytes", 1), ("mount_bytes", 1), ("seconds", 0)],
)
def test_q42_every_shared_work_limit_fails_closed(permission, field, value):
    backend = SyntheticMetadata()
    limits = permission.Limits(**{field: value})
    result = permission.run_permission_init(backend, limits=limits)
    assert result["status"] == "FAIL" and not backend.mutations and not backend.handles


@pytest.mark.parametrize(
    "mode,expected", [(0o640, 0o640), (0o111, 0o711), (0o400, 0o600), (0o751, 0o751)]
)
def test_q42_regular_mode_preserves_existing_nonowner_bits(permission, mode, expected):
    backend = SyntheticMetadata()
    backend.rows[("data", "ordinary")].st_mode = stat.S_IFREG | mode
    result = permission.run_permission_init(backend)
    assert result["status"] == "PASS"
    assert stat.S_IMODE(backend.rows[("data", "ordinary")].st_mode) == expected


def test_q42_directory_sticky_bit_is_preserved(permission):
    backend = SyntheticMetadata()
    backend.rows[("data",)].st_mode |= stat.S_ISVTX
    assert permission.run_permission_init(backend)["status"] == "PASS"
    assert stat.S_IMODE(backend.rows[("data",)].st_mode) == 0o1700


@pytest.mark.parametrize("error", [KeyError("private-account"), OSError("private-metadata")])
def test_q42_account_failure_has_no_target_access_or_private_output(permission, error):
    backend = SyntheticMetadata()

    def fail_account():
        raise error

    backend.account = fail_account
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and result["stage"] == "preflight"
    assert backend.next_fd == 10 and not backend.mutations
    assert "private-" not in str(result)


@pytest.mark.parametrize("field", ["st_ino", "st_nlink", "st_size", "st_mtime_ns", "st_mode"])
def test_q42_leaf_replacement_or_metadata_drift_prevents_leaf_mutation(permission, field):
    backend = SyntheticMetadata()
    count = 0

    def replace_before_second_open(model, key):
        nonlocal count
        if key != ("data", "ordinary"):
            return
        count += 1
        if count == 2:
            value = getattr(model.rows[key], field)
            setattr(model.rows[key], field, value + 1)

    backend.hook = replace_before_second_open
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL"
    assert all(key != ("data", "ordinary") for operation, key in backend.mutations)
    assert not backend.handles


def test_q42_directory_replacement_blocks_access_through_new_ancestor(permission):
    backend = SyntheticMetadata()
    backend.add(("data", "parent"), stat.S_IFDIR | 0o700, nlink=2)
    backend.add(("data", "parent", "child"), stat.S_IFREG | 0o600)
    count = 0

    def replace_parent(model, key):
        nonlocal count
        if key == ("data", "parent"):
            count += 1
            if count == 2:
                model.rows[key].st_ino += 100

    backend.hook = replace_parent
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL"
    assert all(key != ("data", "parent", "child") for operation, key in backend.mutations)
    assert not backend.handles


def test_q42_successful_syscall_without_postcondition_is_failure(permission):
    backend = SyntheticMetadata()
    backend.fchown = lambda fd, uid, gid: None
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and result["mutation_uncertain"] is True
    assert not backend.handles


def test_q42_final_metadata_drift_cannot_be_success(permission):
    backend = SyntheticMetadata()
    original = backend.mountinfo

    def drift_during_final(limit):
        raw = original(limit)
        if backend.mount_calls == 3:
            backend.rows[("data", "ordinary")].st_size += 1
        return raw

    backend.mountinfo = drift_during_final
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and result["mutation_uncertain"] is True
    assert not backend.handles


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"broken\n",
        b"1 2 broken / /synthetic/data rw - ext4 none rw\n",
        b"\xff",
        b"1 2 1:0 /bad\\999 /synthetic/data rw - ext4 none rw\n",
    ],
)
def test_q42_malformed_mount_metadata_is_not_accepted(permission, raw):
    backend = SyntheticMetadata()
    backend.mountinfo = lambda limit: raw
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and result["stage"] == "preflight"
    assert not backend.mutations and not backend.handles


@pytest.mark.parametrize("name", ["..", ".", "outside/child", "escape\\child", ""])
def test_q42_untrusted_relative_entry_is_rejected(permission, name):
    backend = SyntheticMetadata()
    backend.add(("data", name), stat.S_IFREG | 0o600)
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and not backend.mutations
    assert not backend.handles


def test_q42_fd_budget_is_enforced_without_leaking_handles(permission):
    backend = SyntheticMetadata()
    result = permission.run_permission_init(backend, limits=permission.Limits(fds=4))
    assert result["status"] == "FAIL" and not backend.mutations and not backend.handles


def test_q42_import_does_not_initialize_adapter_or_read_target_metadata(permission, monkeypatch):
    path = ROOT / "ToukaHeartbeat" / "permission_init.py"
    spec = importlib.util.spec_from_file_location("q42_import_no_metadata", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def forbidden(*args, **kwargs):
        pytest.fail("Import performed metadata, content, or mutation operations.")

    for name in ("open", "scandir", "lstat", "fchmod", "fchown"):
        monkeypatch.setattr(os, name, forbidden, raising=False)
    for name in ("resolve", "stat", "open", "read_bytes", "read_text"):
        monkeypatch.setattr(Path, name, forbidden)
    spec.loader.exec_module(module)
    assert module.PROFILE == "local-permissions-v1"


def test_q42_source_has_only_fd_permission_mutation_apis(permission):
    tree = ast.parse((ROOT / "ToukaHeartbeat" / "permission_init.py").read_bytes())
    forbidden = {
        "write",
        "write_bytes",
        "write_text",
        "pwrite",
        "truncate",
        "ftruncate",
        "unlink",
        "rename",
        "replace",
        "mkdir",
        "makedirs",
        "copy",
        "copyfile",
        "chown",
        "chmod",
        "mmap",
        "pread",
    }
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and not (
            node.func.attr == "write"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "stdout"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
        )
    }
    assert not calls & forbidden
    assert {"fchown", "fchmod"} <= calls
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr == "remove"
        for node in ast.walk(tree)
    )


def test_q42_unknown_internal_failure_reason_is_redacted(permission):
    backend = SyntheticMetadata()

    def fail_account():
        raise permission.PermissionFailure("private-arbitrary-reason")

    backend.account = fail_account
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and "private-arbitrary" not in str(result)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--profile", "private-unknown"],
        ["--profile", "local-permissions-v1", "--expected-uid", "0", "--expected-gid", "1001"],
        ["--target", "private-target"],
    ],
)
def test_q42_invalid_cli_never_initializes_native_adapter(permission, monkeypatch, capsys, argv):
    calls = []
    monkeypatch.setattr(permission, "NativeMetadata", lambda: calls.append(True))
    assert permission.main(argv) != 0
    output = capsys.readouterr().out
    assert "private-" not in output and not calls


def test_q42_canonical_cli_runs_same_algorithm_with_closed_output(permission, monkeypatch, capsys):
    backend = SyntheticMetadata()
    monkeypatch.setattr(permission, "NativeMetadata", lambda: backend)
    assert (
        permission.main(
            [
                "--profile",
                "local-permissions-v1",
                "--expected-uid",
                "1001",
                "--expected-gid",
                "1001",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"status": "PASS"' in output and "ordinary" not in output
    assert not backend.handles


def test_q42_held_mount_id_must_match_path_selected_mount_with_same_device(permission):
    backend = SyntheticMetadata()
    backend.mount_id = lambda fd: 999
    result = permission.run_permission_init(backend)
    assert result["status"] == "FAIL" and result["stage"] == "preflight"
    assert not backend.mutations and not backend.handles


@pytest.mark.parametrize("kind", [stat.S_IFIFO, stat.S_IFLNK, stat.S_IFCHR, stat.S_IFDIR])
def test_q42_native_regular_open_rejects_special_type_before_readable_open(
    permission, monkeypatch, kind
):
    opened = []
    closed = []
    for name, value in {
        "O_PATH": 0x200000,
        "O_NOFOLLOW": 0x20000,
        "O_CLOEXEC": 0x80000,
        "O_NOATIME": 0x40000,
        "O_NONBLOCK": 0x800,
    }.items():
        monkeypatch.setattr(os, name, value, raising=False)

    def fake_open(path, flags, **kwargs):
        opened.append((path, flags, kwargs))
        return 18

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda fd: SimpleNamespace(st_mode=kind | 0o600, st_nlink=1, st_dev=1, st_ino=8),
    )
    backend = object.__new__(permission.NativeMetadata)
    with pytest.raises(permission.PermissionFailure):
        backend.open_child(10, "candidate", False)
    assert len(opened) == 1 and opened[0][1] & os.O_PATH
    assert closed == [18]


def test_q42_native_regular_upgrade_uses_held_inode_and_checks_identity(permission, monkeypatch):
    opened = []
    closed = []
    for name, value in {
        "O_PATH": 0x200000,
        "O_NOFOLLOW": 0x20000,
        "O_CLOEXEC": 0x80000,
        "O_NOATIME": 0x40000,
        "O_NONBLOCK": 0x800,
    }.items():
        monkeypatch.setattr(os, name, value, raising=False)

    def fake_open(path, flags, **kwargs):
        opened.append((str(path), flags, kwargs))
        return 18 if len(opened) == 1 else 19

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_dev=1, st_ino=8),
    )
    backend = object.__new__(permission.NativeMetadata)
    assert backend.open_child(10, "candidate", False) == 19
    assert len(opened) == 2 and opened[0][1] & os.O_PATH
    assert Path(opened[1][0]).parts[-3:] == (str(os.getpid()), "fd", "18")
    assert opened[1][2] == {} and not opened[1][1] & os.O_PATH
    assert closed == [18]


@pytest.mark.parametrize("raw", [b"", b"mnt_id:\tbad\n", b"mnt_id:\t1\nmnt_id:\t2\n"])
def test_q42_native_fd_mount_metadata_is_closed(permission, monkeypatch, raw):
    backend = object.__new__(permission.NativeMetadata)
    monkeypatch.setattr(backend, "_kernel_bytes", lambda *args: raw)
    with pytest.raises(permission.PermissionFailure):
        backend.mount_id(18)


def test_q42_native_fd_mount_metadata_reads_only_held_descriptor_record(permission, monkeypatch):
    backend = object.__new__(permission.NativeMetadata)
    observed = []

    def kernel(name, limit, fd=None):
        observed.append((name, limit, fd))
        return b"pos:\t0\nflags:\t0100000\nmnt_id:\t123\nino:\t456\n"

    monkeypatch.setattr(backend, "_kernel_bytes", kernel)
    assert backend.mount_id(18) == 123
    assert observed == [("fdinfo", 64 * 1024, 18)]


@pytest.mark.parametrize("fds,passed", [(5, False), (6, True)])
def test_q42_transient_fd_budget_includes_probe_and_readable_descriptor(permission, fds, passed):
    backend = SyntheticMetadata()
    result = permission.run_permission_init(backend, limits=permission.Limits(fds=fds))
    assert (result["status"] == "PASS") is passed
    assert passed or not backend.mutations
    assert not backend.handles


@pytest.mark.parametrize(
    "chunks,expected", [([b"123", b"456", b""], b"123456"), ([b"123456", b"78901"], None)]
)
def test_q42_kernel_metadata_read_is_bounded_until_eof(permission, monkeypatch, chunks, expected):
    backend = object.__new__(permission.NativeMetadata)
    pending = iter(chunks)
    closed = []
    monkeypatch.setattr(os, "O_CLOEXEC", 0x80000, raising=False)
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: 18)
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setattr(os, "read", lambda fd, count: next(pending))
    if expected is None:
        with pytest.raises(permission.PermissionFailure):
            backend._kernel_bytes("mountinfo", 10)
    else:
        assert backend._kernel_bytes("mountinfo", 10) == expected
    assert closed == [18]
