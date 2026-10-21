"""隔離啟動器核心的純函式測試（任何平台都能跑；不呼叫真的 Landlock）.

涵蓋 spec 的版本與驗證（SSOT）、ABI 3～7 的權利遮罩、每條路徑規則的授權判定
（上層公開設定檔遇 ELOOP／符號連結／硬連結略過、系統路徑 ENOTDIR 略過），以及
套用順序：先路徑層、最後才套共用網路規則集。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
from pathlib import PurePosixPath

import pytest
from src.sandbox_kernel import launcher
from src.sandbox_kernel import spec as kernel_spec
from src.services.isolation import profiles
from src.utils import touka_dirs

ll = launcher.landlock
sp = launcher.spec

PUBLIC = "pytest.ini"


def _spec(**overrides: object) -> dict:
    base: dict = {
        "version": sp.SPEC_VERSION,
        "profile": "agent_command",
        "rules": [{"path": "/srv/agent", "access": "rwx"}],
        "block_ports": [8000, 8083],
        "rlimits": {},
        "pin": True,
    }
    base.update(overrides)
    return base


def _parse(document: object) -> sp.LaunchSpec:
    return sp.parse_spec(json.dumps(document).encode("utf-8"))


def _code(document: object) -> str:
    with pytest.raises(sp.SpecError) as caught:
        _parse(document)
    return caught.value.code


# ── spec：版本、大小、欄位 ──────────────────────────────────────────────────────


def test_spec_round_trip_and_defaults():
    parsed = _parse(_spec(rules=[{"path": "/srv/agent", "access": "rwx"}]))
    assert parsed.version == sp.SPEC_VERSION
    assert parsed.rules == (sp.RuleSpec("/srv/agent", "rwx", False, False),)
    assert parsed.block_ports == (8000, 8083)
    assert parsed.pin is True  # pin 模式是預設（U1a 探測：9p 上只有 pin 穩定）
    document = _spec()
    del document["pin"]
    assert _parse(document).pin is True


@pytest.mark.parametrize("version", [None, 0, 2, "1", True, 1.0])
def test_spec_rejects_missing_or_unknown_version(version):
    document = _spec()
    if version is None:
        del document["version"]
    else:
        document["version"] = version
    assert _code(document) == "spec_version"


def test_spec_rejects_more_than_one_mebibyte():
    raw = b" " * sp.MAX_SPEC_BYTES + json.dumps(_spec()).encode()
    with pytest.raises(sp.SpecError) as caught:
        sp.parse_spec(raw)
    assert caught.value.code == "spec_too_large"
    assert sp.MAX_SPEC_BYTES == 1024 * 1024


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b'{"version": 1, "version": 1}', b"\xff\xfe"],
    ids=["empty", "garbage", "array", "duplicate-key", "not-utf8"],
)
def test_spec_rejects_malformed_documents(raw):
    with pytest.raises(sp.SpecError) as caught:
        sp.parse_spec(raw)
    assert caught.value.code == "spec_invalid"


@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": 1},
        {"profile": "Agent Command"},
        {"rules": "nope"},
        {"rules": [{"path": "/srv/a", "access": "rwx", "kind": "dir"}]},
        {"rules": [{"path": "/srv/a"}]},
        {"rules": [{"path": "/srv/a", "access": "rw"}]},
        {"rules": [{"path": "/srv/a", "access": "rwx", "optional": 1}]},
        {"rules": [{"path": "/srv/a", "access": "rwx", "skip_symlink": "yes"}]},
        {"pin": "true"},
        {"rlimits": {"nofile": 1}},
        {"rlimits": {"cpu": -1}},
        {"rlimits": {"cpu": True}},
        {"rlimits": []},
    ],
)
def test_spec_rejects_unknown_or_mistyped_fields(overrides):
    assert _code(_spec(**overrides)) == "spec_invalid"


def test_spec_rejects_too_many_rules():
    rules = [{"path": f"/srv/a{i}", "access": "ro"} for i in range(sp.MAX_RULES + 1)]
    assert _code(_spec(rules=rules)) == "spec_invalid"


@pytest.mark.parametrize(
    "path",
    ["relative/x", "/", "", "/srv//x", "/srv/./x", "/srv/../x", "/srv/x/", "/srv/\x00x", 7],
)
def test_spec_rejects_unsafe_rule_paths(path):
    assert _code(_spec(rules=[{"path": path, "access": "ro"}])) == "spec_path"


def test_spec_rejects_overlong_rule_path():
    path = "/" + "a" * sp.MAX_PATH_BYTES
    assert _code(_spec(rules=[{"path": path, "access": "ro"}])) == "spec_path"


@pytest.mark.parametrize(
    "ports",
    [[0], [65536], [8000, 8000], [True], ["8000"], list(range(1, sp.MAX_BLOCK_PORTS + 2)), 8000],
)
def test_spec_rejects_bad_block_ports(ports):
    assert _code(_spec(block_ports=ports)) == "spec_ports"


def test_spec_accepts_up_to_1024_block_ports_and_never_truncates():
    """上游埠加進 B 之後會超過 64：上限 1024（spec 與 HELLO 一起），超過就拒絕、不截斷."""
    assert sp.MAX_BLOCK_PORTS == 1024
    ports = list(range(1, sp.MAX_BLOCK_PORTS + 1))
    assert _parse(_spec(block_ports=ports)).block_ports == tuple(ports)
    assert _code(_spec(block_ports=[*ports, 65535])) == "spec_ports"


def test_spec_net_strict_defaults_to_true():
    """``net_strict``（TCP 規則必須擋得住）沒帶時當 true；只收真正的 bool."""
    assert _parse(_spec()).net_strict is True
    assert _parse(_spec(net_strict=True)).net_strict is True
    assert _parse(_spec(net_strict=False)).net_strict is False
    for bad in ("false", 0, None):
        assert _code(_spec(net_strict=bad)) == "spec_invalid"


def test_spec_accepts_the_fields_u1c_emits():
    """U1c 的 profile 欄位（加上版本）原樣可用；rlimits 只收三種。"""
    document = _spec(
        rules=[
            {"path": "/usr", "access": "rox", "optional": True, "skip_symlink": False},
            {"path": "/proj/pytest.ini", "access": "ro", "optional": True, "skip_symlink": True},
            {"path": "/dev/null", "access": "dev_rw", "optional": True, "skip_symlink": False},
        ],
        rlimits={"cpu": 30, "fsize": 1 << 20, "as": 1 << 30},
        pin=False,
    )
    parsed = _parse(document)
    assert [r.access for r in parsed.rules] == ["rox", "ro", "dev_rw"]
    assert parsed.rules[1].skip_symlink is True
    assert dict(parsed.rlimits) == {"cpu": 30, "fsize": 1 << 20, "as": 1 << 30}
    assert parsed.pin is False


def test_public_config_names_are_the_nine_and_never_env():
    expected = {
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "ruff.toml",
        ".ruff.toml",
        ".python-version",
        "uv.lock",
        "package.json",
    }
    assert expected == sp.PUBLIC_CONFIG_NAMES
    assert not any(name.startswith(".env") for name in sp.PUBLIC_CONFIG_NAMES)


# ── ABI 3～7 遮罩 ───────────────────────────────────────────────────────────────

_ABI1 = (1 << 13) - 1  # EXECUTE..MAKE_SYM
_TCP = 0b11  # BIND_TCP | CONNECT_TCP：網路規則集同時處理 bind 與 connect


@pytest.mark.parametrize(
    ("abi", "fs", "net", "scoped"),
    [
        (3, _ABI1 | ll.REFER | ll.TRUNCATE, 0, 0),
        (4, _ABI1 | ll.REFER | ll.TRUNCATE, _TCP, 0),
        (5, _ABI1 | ll.REFER | ll.TRUNCATE | ll.IOCTL_DEV, _TCP, 0),
        (6, _ABI1 | ll.REFER | ll.TRUNCATE | ll.IOCTL_DEV, _TCP, 0b11),
        (7, _ABI1 | ll.REFER | ll.TRUNCATE | ll.IOCTL_DEV, _TCP, 0b11),
    ],
)
def test_handled_masks_follow_abi_table(abi, fs, net, scoped):
    assert ll.handled_fs(abi) == fs
    assert ll.handled_net(abi) == net
    assert (ll.BIND_TCP, ll.CONNECT_TCP) == (1, 2)
    assert ll.handled_scopes(abi) == scoped
    assert ll.SCOPE_SIGNAL | ll.SCOPE_ABSTRACT_UNIX_SOCKET == 0b11


def test_unverified_abi_is_treated_as_seven():
    assert ll.VERIFIED_ABI_MAX == 7
    assert ll.effective_abi(9) == 7
    assert ll.handled_fs(9) == ll.handled_fs(7)
    assert ll.effective_abi(4) == 4


def test_ruleset_attr_size_covers_only_fields_in_use():
    assert ll.ruleset_attr_size(0, 0) == 8
    assert ll.ruleset_attr_size(ll.CONNECT_TCP, 0) == 16
    assert ll.ruleset_attr_size(0, ll.handled_scopes(6)) == 24


def test_rwx_has_no_device_node_rights():
    rwx = ll.ACCESS_RIGHTS["rwx"]
    assert rwx & ll.EXECUTE and rwx & ll.TRUNCATE and rwx & ll.REFER
    assert not rwx & (ll.MAKE_CHAR | ll.MAKE_BLOCK | ll.IOCTL_DEV)


# ── 每條規則的授權判定 ─────────────────────────────────────────────────────────


def _st(kind: int, nlink: int = 1, perm: int = 0o644) -> os.stat_result:
    return os.stat_result((kind | perm, 1, 1, nlink, 0, 0, 0, 0, 0, 0))


DIR, REG, CHR, LNK, FIFO = stat.S_IFDIR, stat.S_IFREG, stat.S_IFCHR, stat.S_IFLNK, stat.S_IFIFO


def _rule(path: str, access: str, *, optional: bool = False, skip: bool = False):
    return sp.RuleSpec(path, access, optional, skip)


@pytest.mark.parametrize("abi", [3, 4, 5, 6, 7])
def test_directory_rules_get_masked_access(abi):
    granted = launcher.rule_rights(_rule("/srv/a", "rwx"), _st(DIR, 2), abi)
    assert granted == ll.ACCESS_RIGHTS["rwx"] & ll.handled_fs(abi)
    assert launcher.rule_rights(_rule("/usr", "rox"), _st(DIR), abi) == (
        ll.EXECUTE | ll.READ_FILE | ll.READ_DIR
    )


def test_public_config_file_rule_is_read_only():
    assert launcher.rule_rights(_rule(f"/w/{PUBLIC}", "ro", optional=True), _st(REG), 7) == (
        ll.READ_FILE
    )


@pytest.mark.parametrize(
    ("rule", "info"),
    [
        (_rule(f"/w/{PUBLIC}", "ro"), _st(REG, nlink=2)),  # 硬連結
        (_rule(f"/w/{PUBLIC}", "ro"), _st(LNK)),  # 符號連結本身
        (_rule(f"/w/{PUBLIC}", "ro"), _st(DIR)),  # 同名資料夾
        (_rule(f"/w/{PUBLIC}", "ro"), _st(FIFO)),
        (_rule(f"/w/{PUBLIC}", "rwx"), _st(REG)),  # 單檔只給唯讀
        (_rule(f"/w/{PUBLIC}", "rox"), _st(REG)),
        (_rule("/w/.env", "ro"), _st(REG)),  # 九個名稱以外的一般檔
        (_rule("/w/PYTEST.INI", "ro"), _st(REG)),
        (_rule("/srv/a", "dev_rw"), _st(DIR)),  # 裝置權利不給資料夾
        (_rule("/srv/null", "dev_rw"), _st(CHR)),  # 裝置只在 /dev 底下
        (_rule("/dev/null", "rwx"), _st(CHR)),
    ],
)
def test_ungrantable_targets_are_refused(rule, info):
    assert launcher.rule_rights(rule, info, 7) is None


@pytest.mark.parametrize(("abi", "ioctl"), [(4, 0), (5, ll.IOCTL_DEV), (7, ll.IOCTL_DEV)])
def test_device_rules_mask_ioctl_by_abi(abi, ioctl):
    assert launcher.rule_rights(_rule("/dev/null", "dev_rw"), _st(CHR), abi) == (
        ll.READ_FILE | ll.WRITE_FILE | ioctl
    )
    assert launcher.rule_rights(_rule("/dev/urandom", "ro"), _st(CHR), abi) == ll.READ_FILE


# ── 套用：用假的 Landlock 與假的開檔記錄每一步 ───────────────────────────────────


class FakeLandlock:
    """記錄呼叫順序；fd 從 100 開始配。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._next = 100
        self.port_rules: list[int] = []
        self.port_rights: set[int] = set()

    def _fd(self) -> int:
        self._next += 1
        return self._next

    def create_ruleset(self, fs: int, net: int, scoped: int) -> int:
        fd = self._fd()
        self.calls.append(("create", fd, fs, net, scoped))
        return fd

    def add_path_rule(self, ruleset_fd: int, allowed: int, parent_fd: int) -> None:
        self.calls.append(("path", ruleset_fd, allowed, parent_fd))

    def add_port_rule(self, ruleset_fd: int, allowed: int, port: int) -> None:
        self.port_rules.append(port)
        self.port_rights.add(allowed)

    def restrict_self(self, ruleset_fd: int) -> None:
        self.calls.append(("restrict", ruleset_fd))

    def close(self, fd: int) -> None:
        self.calls.append(("close", fd))


class FakeOpener:
    """依路徑回傳 stat 或拋出 errno；記錄 keep／close。"""

    def __init__(self, table: dict[str, object]) -> None:
        self.table = table
        self.kept: list[int] = []
        self.closed: list[int] = []
        self.fds: dict[str, int] = {}
        self._next = 10

    def open_rule_path(self, path: str) -> tuple[int, os.stat_result]:
        entry = self.table[path]
        if isinstance(entry, int):
            raise OSError(entry, os.strerror(entry))
        self._next += 1
        self.fds[path] = self._next
        return self._next, entry

    def keep(self, fd: int) -> int:
        self.kept.append(fd)
        return fd

    def close(self, fd: int) -> None:
        self.closed.append(fd)


def _apply(rules, *, abi=7, net_fd=None, ports=(8000,), pin=True, table=None):
    spec = sp.LaunchSpec(
        version=sp.SPEC_VERSION,
        profile="t",
        rules=tuple(rules),
        block_ports=tuple(ports),
        rlimits=(),
        pin=pin,
    )
    api = FakeLandlock()
    opener = FakeOpener(table or {})
    pins = launcher.apply_sandbox(spec, abi, net_fd, api, opener)
    return api, opener, pins


AGENT = _rule("/srv/agent", "rwx")
TABLE = {"/srv/agent": _st(DIR)}


def test_path_layer_first_then_shared_net_ruleset_last():
    api, _, _ = _apply([AGENT], net_fd=55, table=TABLE)
    kinds = [c[0] for c in api.calls]
    assert kinds == ["create", "path", "restrict", "close", "restrict", "close"]
    fs_fd = api.calls[0][1]
    assert api.calls[0][2:] == (ll.handled_fs(7), 0, ll.handled_scopes(7))
    assert api.calls[2] == ("restrict", fs_fd)
    assert api.calls[4:] == [("restrict", 55), ("close", 55)]


def test_launcher_builds_net_layer_last_when_no_shared_fd():
    api, _, _ = _apply([AGENT], ports=(8000, 8083), table=TABLE)
    creates = [c for c in api.calls if c[0] == "create"]
    assert [c[2:] for c in creates] == [
        (ll.handled_fs(7), 0, ll.handled_scopes(7)),
        (0, _TCP, 0),
    ]
    net_fd = creates[1][1]
    assert api.calls[-2:] == [("restrict", net_fd), ("close", net_fd)]
    assert len(api.port_rules) == 65536 - 2  # 0～65535 扣掉封鎖埠；埠 0 讓程式能 bind 暫時埠
    assert 8000 not in api.port_rules and 8083 not in api.port_rules
    assert {0, 1, 443, 65535} <= set(api.port_rules)
    assert api.port_rights == {_TCP}  # 每條允許的埠同時給 bind 與 connect


@pytest.mark.parametrize(("abi", "ports"), [(3, (8000,)), (7, ())])
def test_no_net_layer_without_ports_or_tcp_support(abi, ports):
    api, _, _ = _apply([AGENT], abi=abi, ports=ports, table=TABLE)
    assert [c[0] for c in api.calls] == ["create", "path", "restrict", "close"]
    assert api.port_rules == []


def test_pin_mode_keeps_rule_fds_and_non_pin_closes_them():
    _, opener, pins = _apply([AGENT], table=TABLE)
    assert pins == [opener.fds["/srv/agent"]] == opener.kept
    assert opener.closed == []
    _, opener, pins = _apply([AGENT], pin=False, table=TABLE)
    assert pins == [] and opener.kept == []
    assert opener.closed == [opener.fds["/srv/agent"]]


class MovingOpener(FakeOpener):
    """pin 時把 fd 換成另一個號碼（真的啟動器用 ``F_DUPFD`` 搬到 100 以上）。"""

    def keep(self, fd: int) -> int:
        super().keep(fd)
        return fd + 100


def test_pins_are_the_fds_that_keep_returns():
    opener = MovingOpener(TABLE)
    launch_spec = sp.LaunchSpec(
        version=sp.SPEC_VERSION, profile="t", rules=(AGENT,), block_ports=(), rlimits=(), pin=True
    )
    pins = launcher.apply_sandbox(launch_spec, 7, None, FakeLandlock(), opener)
    assert pins == [opener.fds["/srv/agent"] + 100]


@pytest.mark.parametrize(
    ("entry", "rule"),
    [
        (errno.ENOENT, _rule("/usr/x", "rox", optional=True)),
        (errno.ENOTDIR, _rule("/bin", "rox", optional=True)),  # /bin 是符號連結時
        (_st(LNK), _rule("/lib64", "rox", optional=True)),
        (errno.ELOOP, _rule(f"/w/{PUBLIC}", "ro", optional=True, skip=True)),
        (_st(LNK), _rule(f"/w/{PUBLIC}", "ro", skip=True)),  # 只有 skip_symlink 也略過
        (_st(REG, nlink=2), _rule(f"/w/{PUBLIC}", "ro", optional=True, skip=True)),
        (_st(DIR), _rule(f"/w/{PUBLIC}", "ro", optional=True)),
    ],
    ids=["enoent", "system-enotdir", "system-symlink", "eloop", "skip-symlink",
         "hardlink", "dir-named-like-config"],
)
def test_optional_and_symlink_rules_are_skipped_not_fatal(entry, rule):
    api, opener, pins = _apply([AGENT, rule], table={**TABLE, rule.path: entry})
    granted = [c for c in api.calls if c[0] == "path"]
    assert [c[3] for c in granted] == [opener.fds["/srv/agent"]]
    assert pins == [opener.fds["/srv/agent"]]
    if rule.path in opener.fds:  # 開到了但不授權：fd 一定要關
        assert opener.fds[rule.path] in opener.closed


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        (errno.ENOENT, "path_open"),
        (errno.ENOTDIR, "path_open"),
        (errno.EACCES, "path_open"),
        (_st(LNK), "path_type"),
        (_st(REG), "path_type"),
    ],
)
def test_required_rules_fail_closed(entry, code):
    with pytest.raises(launcher.LaunchError) as caught:
        _apply([_rule("/srv/agent", "rwx")], table={"/srv/agent": entry})
    assert caught.value.code == code


def test_symlink_skip_does_not_cover_other_errors_of_required_rules():
    rule = _rule(f"/w/{PUBLIC}", "ro", skip=True)
    with pytest.raises(launcher.LaunchError):
        _apply([rule], table={rule.path: errno.ENOENT})


def test_optional_rules_do_not_hide_unexpected_errors():
    rule = _rule("/usr", "rox", optional=True)
    with pytest.raises(launcher.LaunchError) as caught:
        _apply([rule], table={rule.path: errno.EACCES})
    assert caught.value.code == "path_open"


# ── 自測的網路設定檢查（只讀設定值；不建立 socket、不連線）──────────────────────────


@pytest.mark.parametrize(
    ("fastopen", "mptcp", "expected"),
    [
        ("0\n", None, None),  # 沒有 MPTCP 的核心：/proc/sys/net/mptcp 不存在
        ("0\n", "0\n", None),
        ("2\n", None, None),  # 只開 server 端（被連進來）：不影響往外連
        ("1\n", None, "net_tcp_fastopen"),  # 預設值：client 位元開著
        ("3\n", "0\n", "net_tcp_fastopen"),
        ("1027\n", None, "net_tcp_fastopen"),
        (None, None, "net_tcp_fastopen"),  # 讀不到就無法確認，當成開著
        ("", None, "net_tcp_fastopen"),
        ("x\n", "0\n", "net_tcp_fastopen"),
        ("0\n", "1\n", "net_mptcp"),
        ("0\n", "", "net_mptcp"),  # 檔案在卻讀不懂：當成開著
        ("0\n", "-1\n", "net_mptcp"),
        ("1\n", "1\n", "net_tcp_fastopen"),
    ],
)
def test_net_sysctls_landlock_cannot_cover_fail_closed(fastopen, mptcp, expected):
    assert launcher.net_sysctl_issue(fastopen, mptcp) == expected
    assert {"net_tcp_fastopen", "net_mptcp"} == sp.NET_SYSCTL_CODES
    assert all(sp.FAILURE_CODE_PATTERN.fullmatch(code) for code in sp.NET_SYSCTL_CODES)


def test_net_sysctls_are_read_from_proc_of_the_launchers_own_namespace(tmp_path):
    assert launcher.NET_SYSCTL_PATHS == (
        "/proc/sys/net/ipv4/tcp_fastopen",
        "/proc/sys/net/mptcp/enabled",
    )
    value = tmp_path / "value"
    value.write_bytes(b"1\n")
    assert launcher._read_sysctl(str(value)) == "1\n"
    assert launcher._read_sysctl(str(tmp_path / "missing")) is None
    assert launcher._read_sysctl(str(tmp_path)) == ""  # 讀取錯誤（不是不存在）：看不懂


class _FakeListener:
    """自測的 127.0.0.1 listener 替身（不建立真的 socket）。"""

    def __init__(self, port: int) -> None:
        self.port = port

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)

    def close(self) -> None:
        return None


def _selftest(monkeypatch, capsys, tmp_path, abi: int, values: dict[str, str]) -> tuple[int, dict]:
    reads: list[str] = []
    spawned: list[tuple[dict, dict]] = []
    ports = iter(range(40001, 40100))

    def _read(path: str) -> str | None:
        reads.append(path)
        return values.get(path)

    def _spawn(spec_bytes: bytes, argv: list[str]) -> tuple[int, bytes]:
        spawned.append((json.loads(spec_bytes), json.loads(argv[-1])))
        return 0, b'{"checks": {}, "failed": []}'

    monkeypatch.setattr(launcher, "_linux_landlock", lambda: (None, abi))
    monkeypatch.setattr(launcher, "_read_sysctl", _read)
    monkeypatch.setattr(launcher, "_listener", lambda *_args: _FakeListener(next(ports)))
    monkeypatch.setattr(launcher, "_stage2_checks", lambda _config: [])
    monkeypatch.setattr(launcher, "_spawn_launcher", _spawn)
    code = launcher.run_self_test(str(tmp_path / "st"), pin=True)
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return code, {**report, "reads": reads, "spawned": spawned}


@pytest.mark.parametrize(
    ("values", "net"),
    [
        ({"fastopen": "1\n"}, "net_tcp_fastopen"),
        ({"fastopen": "0\n", "mptcp": "1\n"}, "net_mptcp"),
        ({"fastopen": "0\n"}, None),
    ],
)
def test_self_test_reports_unsafe_net_sysctls_and_still_runs_every_check(
    monkeypatch, capsys, tmp_path, values, net
):
    """網路設定不安全只另外回報（``net``）：檔案、bind 與一般 TCP 的檢查照跑，自測本身通過."""
    paths = dict(zip(("fastopen", "mptcp"), launcher.NET_SYSCTL_PATHS, strict=True))
    code, report = _selftest(
        monkeypatch, capsys, tmp_path, 5, {paths[k]: v for k, v in values.items()}
    )
    assert (code, report["ok"], report["error"], report["abi"]) == (0, True, None, 5)
    assert report["net"] == net
    [(spec_document, config)] = report["spawned"]  # 第二段照樣跑了一次
    assert config["tcp"] == {"blocked": 40001, "allowed": 40002}
    assert spec_document["block_ports"] == [40001]
    assert {"rw", "ro", "deny", "pub"} <= set(config)
    assert not (tmp_path / "st").exists()


def test_self_test_stage2_is_not_net_strict(monkeypatch, capsys, tmp_path):
    """自測要在網路設定不安全時照樣跑完檔案、bind 與一般 TCP 的檢查（``net`` 另外回報），
    所以第二段的 spec 送 ``net_strict: false``；否則啟動器會以 net_* 拒絕、整個自測失敗."""
    paths = dict(zip(("fastopen", "mptcp"), launcher.NET_SYSCTL_PATHS, strict=True))
    _code, report = _selftest(monkeypatch, capsys, tmp_path, 5, {paths["fastopen"]: "1\n"})
    [(spec_document, _config)] = report["spawned"]
    assert spec_document["net_strict"] is False


def test_self_test_skips_the_net_check_when_tcp_is_not_restricted(monkeypatch, capsys, tmp_path):
    """ABI 3 沒有 TCP 規則（卡片照實寫「連得到內部服務」），網路設定不影響檔案隔離."""
    code, report = _selftest(monkeypatch, capsys, tmp_path, 3, {})
    assert (code, report["ok"], report["error"], report["reads"]) == (0, True, None, [])
    assert report["net"] is None and report["spawned"][0][1]["tcp"] is None


# ── 每次啟動都讀網路設定（net_strict）：不安全就不執行指令 ───────────────────────────────


def _fake_launch(monkeypatch, *, abi=7, sysctls=None, **spec_fields):
    """用假的 Landlock 跑 ``launch``；回（讀了哪些設定、到了哪些步驟、結果或 LaunchError）。"""
    reads: list[str] = []
    steps: list[str] = []
    values = dict(zip(launcher.NET_SYSCTL_PATHS, sysctls or ("0\n", None), strict=True))
    net_fd = spec_fields.pop("_net_fd", None)
    document = json.dumps(_spec(**spec_fields)).encode("utf-8")

    def _read(path):
        reads.append(path)
        return values[path]

    monkeypatch.setattr(launcher, "_read_spec", lambda _fd: document)
    monkeypatch.setattr(launcher, "_linux_landlock", lambda: (FakeLandlock(), abi))
    monkeypatch.setattr(launcher, "_set_process_attributes", lambda _api: steps.append("attrs"))
    monkeypatch.setattr(launcher, "_read_sysctl", _read)
    monkeypatch.setattr(launcher, "apply_sandbox", lambda *_a: steps.append("sandbox") or [])
    monkeypatch.setattr(launcher, "_apply_rlimits", lambda _limits: None)
    monkeypatch.setattr(launcher, "_close_inherited_fds", lambda _pins: None)
    monkeypatch.setattr(launcher, "_exec", lambda _argv, _env: steps.append("exec") or 0)
    command = launcher.Command("launch", spec_fd=3, net_fd=net_fd, argv=("true",))
    try:
        outcome: object = launcher.launch(command)
    except launcher.LaunchError as exc:
        outcome = exc
    return reads, steps, outcome


@pytest.mark.parametrize(
    ("sysctls", "code"),
    [
        (("1\n", None), "net_tcp_fastopen"),
        (("0\n", "1\n"), "net_mptcp"),
        ((None, None), "net_tcp_fastopen"),  # 讀不到就當成開著
    ],
)
def test_launch_refuses_unsafe_net_sysctls_before_any_restriction(monkeypatch, sysctls, code):
    reads, steps, outcome = _fake_launch(monkeypatch, sysctls=sysctls)
    assert isinstance(outcome, launcher.LaunchError) and outcome.code == code
    assert reads == list(launcher.NET_SYSCTL_PATHS)
    assert steps == ["attrs"]  # 沒套規則、沒 exec


def test_launch_reads_the_net_sysctls_on_every_launch(monkeypatch):
    reads, steps, outcome = _fake_launch(monkeypatch)
    assert outcome == 0 and steps == ["attrs", "sandbox", "exec"]
    assert reads == list(launcher.NET_SYSCTL_PATHS)
    again, _, _ = _fake_launch(monkeypatch)
    assert again == list(launcher.NET_SYSCTL_PATHS)  # 沒有快取


@pytest.mark.parametrize(
    "case",
    [
        {"net_strict": False},  # 人核准的降級（features 不含 tcp）
        {"abi": 3},  # 沒有 TCP 規則
        {"block_ports": []},  # 沒有封鎖埠、也沒有共用規則集
    ],
    ids=["consented-fallback", "abi3", "no-ports"],
)
def test_launch_skips_the_net_check_without_strict_tcp_rules(monkeypatch, case):
    reads, steps, outcome = _fake_launch(monkeypatch, sysctls=("1\n", "1\n"), **case)
    assert (reads, outcome) == ([], 0)
    assert steps == ["attrs", "sandbox", "exec"]


def test_shared_net_ruleset_counts_as_tcp_rules(monkeypatch):
    _, _, outcome = _fake_launch(monkeypatch, sysctls=("1\n", None), block_ports=[], _net_fd=9)
    assert isinstance(outcome, launcher.LaunchError) and outcome.code == "net_tcp_fastopen"


# ── 命令列 ─────────────────────────────────────────────────────────────────────


def test_cli_launch_form_reads_spec_from_fd():
    command = launcher.parse_cli(["--spec-fd", "4", "--net-ruleset-fd", "5", "--", "sh", "-c", "x"])
    assert (command.mode, command.spec_fd, command.net_fd) == ("launch", 4, 5)
    assert command.argv == ("sh", "-c", "x")
    assert launcher.parse_cli(["--spec-fd", "3", "--", "true"]).net_fd is None


def test_cli_self_test_and_probe_forms():
    command = launcher.parse_cli(["--self-test", "--pin", "--dir", "/srv/st"])
    assert (command.mode, command.pin, command.directory) == ("self-test", True, "/srv/st")
    assert launcher.parse_cli(["--self-test", "--dir", "/srv/st"]).pin is True
    assert launcher.parse_cli(["--self-test", "--no-pin", "--dir", "/srv/st"]).pin is False
    assert launcher.parse_cli(["--probe"]).mode == "probe"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--spec-fd", "4"],  # 沒有 -- 與指令
        ["--spec-fd", "4", "--"],
        ["--spec-fd", "x", "--", "true"],
        ["--spec-fd", "2", "--", "true"],  # stdio 不能當 spec fd
        ["--spec-fd", "4", "--net-ruleset-fd", "4", "--", "true"],
        ["--spec", "{}", "--", "true"],  # 舊的 argv 形式不再接受
        ["--self-test"],
        ["--self-test", "--dir", "relative"],
        ["--probe", "extra"],
        ["--unknown"],
    ],
)
def test_cli_rejects_malformed_arguments(argv):
    with pytest.raises(sp.SpecError) as caught:
        launcher.parse_cli(argv)
    assert caught.value.code == "usage"


# ── 獨立的失敗代碼 fd（--status-fd）：runner 只認這條通道，指令輸出的標記不算 ──────────


def test_cli_launch_form_accepts_a_status_fd():
    command = launcher.parse_cli(["--spec-fd", "4", "--status-fd", "6", "--", "true"])
    assert (command.spec_fd, command.status_fd, command.net_fd) == (4, 6, None)
    assert launcher.parse_cli(["--spec-fd", "4", "--", "true"]).status_fd is None
    for argv in (
        ["--spec-fd", "4", "--status-fd", "4", "--", "true"],
        ["--spec-fd", "4", "--net-ruleset-fd", "5", "--status-fd", "5", "--", "true"],
        ["--spec-fd", "4", "--status-fd", "1", "--", "true"],
        ["--self-test", "--status-fd", "6", "--dir", "/srv/st"],
    ):
        with pytest.raises(sp.SpecError):
            launcher.parse_cli(argv)


def test_status_fd_is_close_on_exec_as_soon_as_it_is_claimed():
    """指令（以及之後 fork 出去的任何東西）拿不到這條通道."""
    read_fd, write_fd = os.pipe()
    try:
        os.set_inheritable(write_fd, True)
        assert launcher._claim_status_fd(write_fd) == write_fd
        assert os.get_inheritable(write_fd) is False
        assert launcher._claim_status_fd(None) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_unusable_status_fd_is_a_usage_error():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    with pytest.raises(launcher.LaunchError) as caught:
        launcher._claim_status_fd(write_fd)
    assert caught.value.code == "usage"


def test_failure_code_goes_to_the_status_fd_without_the_detail(capfd):
    spec_r, spec_w = os.pipe()
    status_r, status_w = os.pipe()
    os.write(spec_w, b"not json")
    os.close(spec_w)
    os.set_blocking(status_r, False)  # 啟動器沒關寫端時直接失敗，不會卡住
    try:
        code = launcher.main(
            ["--spec-fd", str(spec_r), "--status-fd", str(status_w), "--", "true"]
        )
        assert code == sp.FAILURE_EXIT
        assert os.read(status_r, 4096) == b"spec_invalid\n"
        assert os.read(status_r, 4096) == b""  # 寫完就關：讀端拿到 EOF
    finally:
        os.close(status_r)
    assert f"{sp.FAILURE_MARKER}spec_invalid json" in capfd.readouterr().err  # 給人看的那行照留


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"path_open\n", "path_open"),
        (b"net_mptcp\n", "net_mptcp"),
        (b"path_open", "path_open"),
        (b"", None),  # 啟動器沒寫：不是隔離失敗
        (b"made_up_code\n", None),
        (b"path_open\nusage\n", None),
        (b"PATH_OPEN\n", None),
        (b"path_open \n", None),
        (b"\xff\n", None),
        (b"a" * 200, None),
    ],
)
def test_parse_status_only_accepts_one_known_code(raw, code):
    assert sp.parse_status(raw, sp.FAILURE_CODES) == code


def test_net_sysctl_refusals_are_launcher_failure_codes():
    assert sp.NET_SYSCTL_CODES <= sp.FAILURE_CODES
    assert sp.encode_status("net_tcp_fastopen") == b"net_tcp_fastopen\n"
    assert len(sp.encode_status("a" * 64)) <= sp.STATUS_MAX_BYTES


def test_failure_codes_match_the_runner_marker_format():
    assert sp.FAILURE_EXIT == 125
    assert sp.FAILURE_MARKER == "TOUKA_SANDBOX_ERROR:"
    for code in sp.FAILURE_CODES:
        assert sp.FAILURE_CODE_PATTERN.fullmatch(code), code
    assert {"spec_too_large", "abi_too_old", "path_open", "usage"} <= sp.FAILURE_CODES


# ── kernel 是 Touka 標記與預設封鎖埠的 SSOT（執行器伺服器只能依路徑載入 kernel）───────────


def test_touka_markers_and_default_ports_have_one_copy_in_the_kernel():
    """proxy 端（``src.sandbox_kernel.spec``）與啟動器依路徑載入的那份是同一個原始檔."""
    assert not hasattr(touka_dirs, "TOUKA_MARKERS")  # 名單只在 kernel，proxy 端不另存
    assert not hasattr(touka_dirs, "DATA_ROOT_NAMES")
    assert touka_dirs.fold_name is kernel_spec.fold_name
    assert touka_dirs.listing_kind is kernel_spec.listing_kind
    assert profiles.DEFAULT_BLOCK_PORTS is kernel_spec.DEFAULT_BLOCK_PORTS
    assert sp.TOUKA_MARKERS == kernel_spec.TOUKA_MARKERS
    assert frozenset({8000, 8082, 8083, 3001, 2375, 2376}) == sp.DEFAULT_BLOCK_PORTS
    assert len(sp.DEFAULT_BLOCK_PORTS) <= sp.MAX_BLOCK_PORTS
    assert frozenset({"soul", "personal_drive", "ui_sessions.db"}) == sp.DATA_ROOT_NAMES
    assert {"toukaproxy", "heartbeat-config", "docker-compose.ghcr.yml"} <= sp.TOUKA_MARKERS


@pytest.mark.parametrize(
    ("names", "kind"),
    [
        ({"soul", "toukaproxy"}, "sealed"),  # 資料根形狀優先
        ({"personal_drive"}, "sealed"),
        ({"toukamemory", "src"}, "touka"),
        ({"docker-compose.ghcr.yml"}, "touka"),
        ({"src", "readme.md"}, None),
        (set(), None),
    ],
)
def test_listing_kind_reads_only_the_folded_names(names, kind):
    assert sp.listing_kind(names) == kind


def test_fold_name_is_nfc_then_casefold():
    assert sp.fold_name("ToukaProxy") == "toukaproxy"
    assert sp.fold_name("Cafe\u0301") == sp.fold_name("CAF\u00c9") == "caf\u00e9"


# ── 執行器 START 的 env 允許清單（精確名稱；GIT_CONFIG_* 只收 profile 那一組）─────────────

ALLOWED_ENV = {
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_NUMERIC",
    "LC_TIME", "LC_MONETARY",
    "TZ", "TERM", "NO_COLOR",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    "GIT_CEILING_DIRECTORIES", "PYTEST_ADDOPTS",
    "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONIOENCODING",
    "PIP_NO_INPUT", "PIP_DISABLE_PIP_VERSION_CHECK", "PIP_NO_CACHE_DIR", "PIP_PROGRESS_BAR",
    "UV_PYTHON_DOWNLOADS",  # C2-3：執行器裡的 uvx 不自己下載 Python
}
GIT_TRIO = {
    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"
}
NEVER_ENV = [
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST", "PIP_CERT", "PIP_CLIENT_CERT",
    "PIP_CONFIG_FILE", "PIP_FIND_LINKS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "TOUKA_EXEC_ID", "LD_PRELOAD", "PYTHONHOME", "PYTHONSTARTUP", "OPENAI_API_KEY",
    "TOUKA_AUTH_SECRET", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_GLOBAL", "LC_FAKE",
]


def test_executor_env_allowlist_is_the_exact_list():
    assert set(sp.EXECUTOR_ENV_ALLOWED) == ALLOWED_ENV
    assert dict(sp.EXECUTOR_GIT_CONFIG) == GIT_TRIO
    assert not set(NEVER_ENV) & sp.EXECUTOR_ENV_ALLOWED
    assert sp.MAX_EXECUTOR_ENV_BYTES == 64 * 1024


def test_proxy_filter_keeps_only_exact_names_and_drops_inherited_git_config():
    inherited = dict.fromkeys(NEVER_ENV, "x")
    inherited.update(GIT_TRIO, PATH="/usr/bin", LANG="C.UTF-8", PIP_NO_INPUT="1")
    assert sp.filter_executor_env(inherited) == {
        "PATH": "/usr/bin", "LANG": "C.UTF-8", "PIP_NO_INPUT": "1"
    }


def test_profile_env_passes_the_executor_check():
    """profile 自己設的那組（``profiles.sandbox_env``）就是 kernel 收的那組，兩邊不會漂移."""
    env = profiles.sandbox_env(PurePosixPath("/h"), PurePosixPath("/t"), [], PurePosixPath("/p"))
    assert {k: v for k, v in env.items() if k.startswith("GIT_CONFIG_")} == GIT_TRIO
    composed = {**sp.filter_executor_env({"PATH": "/usr/bin", "GIT_CONFIG_COUNT": "9"}), **env}
    assert sp.executor_env_issue(composed) is None


@pytest.mark.parametrize(
    ("env", "issue"),
    [
        ({"PATH": "/usr/bin"}, None),
        ({"PATH": "/usr/bin", **GIT_TRIO}, None),
        ({}, None),
        ({"LD_PRELOAD": "/x.so"}, "env_name"),
        ({"TOUKA_EXEC_ID": "f" * 32}, "env_name"),
        ({"PIP_INDEX_URL": "https://u:p@example.invalid/simple"}, "env_name"),
        ({**GIT_TRIO, "GIT_CONFIG_VALUE_0": "/"}, "env_git_config"),
        ({**GIT_TRIO, "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "core.hooksPath",
          "GIT_CONFIG_VALUE_1": "/srv/hooks"}, "env_git_config"),
        ({"GIT_CONFIG_COUNT": "1"}, "env_git_config"),
        ({**GIT_TRIO, "GIT_CONFIG_PARAMETERS": "'core.pager=sh'"}, "env_git_config"),
        ({"PATH": 1}, "env_type"),
        ({"PATH": "/usr/bin\x00x"}, "env_nul"),
        ({"PYTEST_ADDOPTS": "x" * (64 * 1024)}, "env_too_large"),
        (["PATH"], "env_type"),
    ],
    ids=["plain", "git-trio", "empty", "ld-preload", "exec-id", "pip-url", "git-value",
         "git-extra", "git-partial", "git-parameters", "type", "nul", "size", "not-dict"],
)
def test_server_side_env_check(env, issue):
    assert sp.executor_env_issue(env) == issue


def test_kernel_digest_hashes_sorted_names_and_contents(tmp_path):
    (tmp_path / "b.py").write_bytes(b"B")
    (tmp_path / "a.py").write_bytes(b"A")
    (tmp_path / "notes.txt").write_bytes(b"ignored")
    expected = hashlib.sha256(b"a.py\0A\0b.py\0B\0").hexdigest()
    assert sp.kernel_digest(tmp_path) == expected
    assert sp.kernel_digest(tmp_path / "missing") is None


# ── 可信直譯器：sys._base_executable 解析後的實體路徑；容器裡整條擁有者鏈要 root、不可寫 ───────


def test_trusted_interpreter_is_the_resolved_base_executable_not_the_venv_one(
    tmp_path, monkeypatch
):
    """venv 的 python 可能在服務帳號改得到的位置（``/app`` 由 appuser 擁有）；只信任基底直譯器."""
    base, venv = tmp_path / "base-python", tmp_path / "venv-python"
    base.write_bytes(b"")
    venv.write_bytes(b"")
    monkeypatch.setattr(sys, "_base_executable", str(base), raising=False)
    monkeypatch.setattr(sys, "executable", str(venv))
    assert sp.trusted_interpreter(require_root_owned=False) == str(base.resolve())


@pytest.mark.parametrize("value", ["", None])
def test_trusted_interpreter_without_a_base_executable_is_untrusted(monkeypatch, value):
    monkeypatch.setattr(sys, "_base_executable", value, raising=False)
    with pytest.raises(sp.InterpreterUntrustedError) as caught:
        sp.trusted_interpreter(require_root_owned=False)
    assert caught.value.code == sp.INTERPRETER_UNTRUSTED == "interpreter_untrusted"


def test_trusted_interpreter_that_cannot_be_resolved_is_untrusted(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "missing"), raising=False)
    with pytest.raises(sp.InterpreterUntrustedError):
        sp.trusted_interpreter(require_root_owned=False)


PYTHON = PurePosixPath("/usr/local/bin/python3.12")
CHAIN = [PYTHON, *PYTHON.parents]  # 檔案本身、/usr/local/bin、/usr/local、/usr、/


def _chain_lstat(bad: PurePosixPath | None = None, *, uid=0, mode=0o755, kind=None):
    def _lstat(path):
        node = PurePosixPath(path)
        file_kind = stat.S_IFREG if node == PYTHON else stat.S_IFDIR
        if node == bad:
            return os.stat_result(((kind or file_kind) | mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))
        return os.stat_result((file_kind | 0o755, 1, 1, 1, 0, 0, 0, 0, 0, 0))

    return _lstat


def test_root_owned_chain_passes():
    assert sp.owner_chain_issue(PYTHON, _chain_lstat()) is None


@pytest.mark.parametrize("bad", CHAIN, ids=[str(p) for p in CHAIN])
@pytest.mark.parametrize(
    "fault",
    [{"uid": 1001}, {"mode": 0o775}, {"mode": 0o757}, {"kind": stat.S_IFLNK}],
    ids=["not-root", "group-writable", "other-writable", "symlink"],
)
def test_every_level_of_the_owner_chain_is_checked(bad, fault):
    """檔案與每一層上層（一直到 ``/``）都要 root 擁有、群組與其他人不可寫、不是連結."""
    assert sp.owner_chain_issue(PYTHON, _chain_lstat(bad, **fault))


def test_owner_chain_needs_a_regular_file_and_readable_levels():
    assert sp.owner_chain_issue(PYTHON, _chain_lstat(PYTHON, kind=stat.S_IFDIR))

    def _unreadable(_path):
        raise PermissionError(errno.EACCES, "denied")

    assert sp.owner_chain_issue(PYTHON, _unreadable)


def test_trusted_interpreter_in_a_container_checks_the_owner_chain(tmp_path, monkeypatch):
    base = tmp_path / "python"
    base.write_bytes(b"")
    monkeypatch.setattr(sys, "_base_executable", str(base), raising=False)
    seen = []

    def _issue(path, _lstat):
        seen.append(path)
        return "owner"

    monkeypatch.setattr(sp, "owner_chain_issue", _issue)
    with pytest.raises(sp.InterpreterUntrustedError):
        sp.trusted_interpreter(require_root_owned=True)
    assert seen == [base.resolve()]


def test_launcher_self_test_children_use_the_trusted_interpreter(monkeypatch, tmp_path):
    """自測的第二段與啟動器子行程都用可信直譯器，不用 ``sys.executable``."""
    monkeypatch.setattr(sp, "trusted_interpreter", lambda *, require_root_owned: "/trusted/py")
    spawned: list[list[str]] = []

    def _spawn(_spec_bytes, argv):
        spawned.append(argv)
        return 0, b'{"checks": {}, "failed": []}'

    monkeypatch.setattr(launcher, "_linux_landlock", lambda: (None, 3))
    monkeypatch.setattr(launcher, "_stage2_checks", lambda _config: [])
    monkeypatch.setattr(launcher, "_spawn_launcher", _spawn)
    launcher.run_self_test(str(tmp_path / "st"), pin=True)
    assert spawned and spawned[0][0] == "/trusted/py"
    assert launcher._launcher_command(4) == [
        "/trusted/py", "-I", "-S", str(launcher._LAUNCHER), "--spec-fd", "4", "--"
    ]
