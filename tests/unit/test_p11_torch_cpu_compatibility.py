"""P11 CPU lock coverage and small offline embeddings, without model downloads."""

from __future__ import annotations

import math
import re
import tomllib
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, call, patch
from urllib.parse import unquote, urlsplit

import numpy as np
import pytest
import torch
from memory_server import vector
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from sentence_transformers import SentenceTransformer, models

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
CPU_HOSTS = {"download.pytorch.org", "download-r2.pytorch.org"}
TORCH_VERSION = "2.13.0"
GPU_NAMES = ("cuda", "nvidia", "triton", "rocm", "hipblas", "rocblas", "hipsolver", "miopen")
LANES = (
    ("linux_s390x", "linux", "Linux", "s390x"),
    ("manylinux_2_28_aarch64", "linux", "Linux", "aarch64"),
    ("manylinux_2_28_x86_64", "linux", "Linux", "x86_64"),
    ("macosx_14_0_arm64", "darwin", "Darwin", "arm64"),
    ("win_amd64", "win32", "Windows", "AMD64"),
)


def _named_nodes(value: Any) -> Iterator[dict[str, Any]]:
    """Include rows and edges, even optional dependencies with inactive markers."""
    if isinstance(value, dict):
        if "name" in value:
            yield value
        for child in value.values():
            yield from _named_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _named_nodes(child)


def _assert_no_gpu_graph(lock: dict[str, Any]) -> None:
    for node in _named_nodes(lock):
        name = canonicalize_name(node["name"])
        assert not any(word in name for word in GPU_NAMES), f"GPU dependency in lock: {name}"


def _lane_environment(lane: tuple[str, str, str, str]) -> dict[str, str]:
    _, platform, system, machine = lane
    return {
        "implementation_name": "cpython",
        "implementation_version": "3.12.12",
        "os_name": "nt" if platform == "win32" else "posix",
        "platform_machine": machine,
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": system,
        "platform_version": "",
        "python_full_version": "3.12.12",
        "python_version": "3.12",
        "sys_platform": platform,
        "extra": "",
    }


def _assert_cpu_lock(lock: dict[str, Any]) -> None:
    _assert_no_gpu_graph(lock)
    rows = [row for row in lock["package"] if canonicalize_name(row["name"]) == "torch"]
    assert len(rows) == 2, "missing or duplicate Torch platform branch"
    assert {row["version"] for row in rows} == {TORCH_VERSION, TORCH_VERSION + "+cpu"}, (
        "unexpected Torch platform versions"
    )
    wheel_platforms: dict[str, set[str]] = {}
    for row in rows:
        assert row["source"] == {"registry": CPU_INDEX}, "non-CPU Torch source"
        assert not row.get("sdist"), "Torch must use the selected CPU wheels"
        platforms = set()
        for wheel in row.get("wheels", []):
            url = urlsplit(wheel["url"])
            assert (
                url.scheme == "https"
                and url.hostname in CPU_HOSTS
                and url.port in (None, 443)
                and url.username is None
                and url.password is None
                and url.path.startswith("/whl/cpu/")
                and not url.query
                and not url.fragment
            ), "non-CPU Torch wheel URL"
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", wheel.get("hash", "")), (
                "missing or invalid wheel SHA256"
            )
            name, version, _, tags = parse_wheel_filename(unquote(PurePosixPath(url.path).name))
            assert name == "torch" and version == Version(row["version"]), (
                "wheel version does not match Torch branch"
            )
            assert all(tag.interpreter == "cp312" and tag.abi == "cp312" for tag in tags), (
                "unexpected Torch interpreter/ABI"
            )
            assert all(
                tag.platform.startswith("macosx_") == (row["version"] == TORCH_VERSION)
                for tag in tags
            ), "wheel platform belongs to the wrong Torch branch"
            platforms.update(tag.platform for tag in tags)
        expected = {
            lane[0] for lane in LANES if (lane[1] == "darwin") == (row["version"] == TORCH_VERSION)
        }
        assert expected <= platforms, "missing existing Torch wheel platform"
        wheel_platforms[row["version"]] = platforms

    for lane in LANES:
        environment = _lane_environment(lane)
        expected_version = TORCH_VERSION if lane[1] == "darwin" else TORCH_VERSION + "+cpu"
        active_partitions = [
            marker
            for marker in lock.get("resolution-markers", [])
            if Marker(marker).evaluate(environment)
        ]
        assert len(active_partitions) == 1, "missing or overlapping lock resolution marker"
        active_rows = [
            row
            for row in rows
            if any(
                Marker(marker).evaluate(environment) for marker in row.get("resolution-markers", [])
            )
        ]
        assert len(active_rows) == 1 and active_rows[0]["version"] == expected_version, (
            "Torch platform marker selects the wrong branch"
        )
        assert lane[0] in wheel_platforms[expected_version]
        for owner in lock["package"]:
            edges = [
                edge
                for edge in _named_nodes(owner.get("dependencies", []))
                if canonicalize_name(edge["name"]) == "torch"
            ]
            if not edges:
                continue
            active_edges = [
                edge
                for edge in edges
                if "marker" not in edge or Marker(edge["marker"]).evaluate(environment)
            ]
            assert len(active_edges) == 1, f"{owner['name']}: missing/overlapping Torch edge"
            assert active_edges[0].get("version") == expected_version, (
                f"{owner['name']}: Torch edge selects the wrong branch"
            )
            assert active_edges[0].get("source") == {"registry": CPU_INDEX}, (
                f"{owner['name']}: non-CPU Torch edge source"
            )


def test_torch_lock_keeps_all_cpu_platform_branches() -> None:
    """Check every row/edge and jointly evaluate platform markers and wheel tags."""
    _assert_cpu_lock(tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8")))


def test_root_torch_source_is_explicit_cpu_index() -> None:
    uv = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]
    assert uv["sources"]["torch"] == {"index": "pytorch-cpu"}
    indices = [index for index in uv["index"] if index["name"] == "pytorch-cpu"]
    assert len(indices) == 1 and indices[0]["url"] == CPU_INDEX and indices[0]["explicit"] is True


@pytest.mark.parametrize("member", ("ToukaProxy", "ToukaMemory"))
def test_embedding_members_pin_target_torch_version(member: str) -> None:
    manifest = tomllib.loads((REPO_ROOT / member / "pyproject.toml").read_text(encoding="utf-8"))
    declarations = [
        requirement
        for value in manifest["project"]["dependencies"]
        if canonicalize_name((requirement := Requirement(value)).name) == "torch"
    ]
    assert declarations == [Requirement("torch==" + TORCH_VERSION)]


def _synthetic_cpu_lock() -> dict[str, Any]:
    """A small valid graph lets negative cases reach the intended failing boundary."""
    rows = []
    for version, marker in (
        (TORCH_VERSION, "sys_platform == 'darwin'"),
        (TORCH_VERSION + "+cpu", "sys_platform != 'darwin'"),
    ):
        rows.append(
            {
                "name": "torch",
                "version": version,
                "source": {"registry": CPU_INDEX},
                "resolution-markers": [marker],
                "wheels": [
                    {
                        "url": f"{CPU_INDEX}/torch-{version}-cp312-cp312-{lane[0]}.whl",
                        "hash": "sha256:" + "a" * 64,
                    }
                    for lane in LANES
                    if (lane[1] == "darwin") == (version == TORCH_VERSION)
                ],
            }
        )
    rows.append(
        {
            "name": "embedding-consumer",
            "version": "1.0.0",
            "dependencies": [
                {
                    "name": "torch",
                    "version": row["version"],
                    "source": deepcopy(row["source"]),
                    "marker": row["resolution-markers"][0],
                }
                for row in rows
            ],
        }
    )
    return {
        "resolution-markers": ["sys_platform == 'darwin'", "sys_platform != 'darwin'"],
        "package": rows,
    }


@pytest.mark.parametrize(
    "names",
    (
        ("nvidia-cublas-cu13",),
        ("cuda-bindings",),
        ("triton",),
        ("pytorch-triton-rocm", "hipblas", "rocblas", "hipsolver", "miopen"),
    ),
    ids=("nvidia", "cuda", "triton", "rocm"),
)
@pytest.mark.parametrize("location", ("row", "edge"))
def test_gpu_graph_is_rejected_even_for_inactive_edges(
    names: tuple[str, ...], location: str
) -> None:
    for name in names:
        lock = _synthetic_cpu_lock()
        gpu_node = {"name": name, "version": "1.0.0", "marker": "python_version < '3.0'"}
        if location == "row":
            lock["package"].append(gpu_node)
        else:
            lock["package"][-1]["dependencies"].append(gpu_node)
        with pytest.raises(AssertionError, match="GPU dependency"):
            _assert_cpu_lock(lock)


@pytest.mark.parametrize("branch", (0, 1), ids=("darwin", "non-darwin"))
def test_missing_cpu_branch_is_rejected(branch: int) -> None:
    lock = _synthetic_cpu_lock()
    del lock["package"][branch]
    with pytest.raises(AssertionError, match="missing or duplicate Torch platform branch"):
        _assert_cpu_lock(lock)


def test_cpu_markers_must_match_wheel_platforms() -> None:
    lock = _synthetic_cpu_lock()
    lock["package"][0]["resolution-markers"] = ["sys_platform != 'darwin'"]
    with pytest.raises(AssertionError, match="platform marker"):
        _assert_cpu_lock(lock)


def test_cpu_edge_markers_must_select_the_matching_branch() -> None:
    lock = _synthetic_cpu_lock()
    edges = lock["package"][-1]["dependencies"]
    edges[0]["marker"], edges[1]["marker"] = edges[1]["marker"], edges[0]["marker"]
    with pytest.raises(AssertionError, match="edge selects the wrong branch"):
        _assert_cpu_lock(lock)


def test_valid_synthetic_cpu_graph_is_accepted() -> None:
    _assert_cpu_lock(_synthetic_cpu_lock())


def test_cpu_graph_can_retain_additional_official_cpu_wheels() -> None:
    """An extra artifact does not qualify another platform or remove baseline coverage."""
    lock = _synthetic_cpu_lock()
    cpu_row = lock["package"][1]
    extra = deepcopy(cpu_row["wheels"][-1])
    extra["url"] = extra["url"].replace("win_amd64", "win_arm64")
    cpu_row["wheels"].append(extra)
    _assert_cpu_lock(lock)


def test_cpu_graph_still_requires_every_existing_wheel_platform() -> None:
    lock = _synthetic_cpu_lock()
    cpu_row = lock["package"][1]
    cpu_row["wheels"] = [wheel for wheel in cpu_row["wheels"] if "win_amd64" not in wheel["url"]]
    with pytest.raises(AssertionError, match="missing"):
        _assert_cpu_lock(lock)


@pytest.fixture
def local_cpu_model() -> SentenceTransformer:
    """Use real CPU operations, deterministic weights, and no pretrained checkpoint."""
    with torch.random.fork_rng(devices=[]):
        bow = models.BoW(vocab=["alpha", "beta", "gamma"])
        dense = models.Dense(
            in_features=3,
            out_features=2,
            bias=False,
            activation_function=torch.nn.Identity(),
            init_weight=torch.tensor([[1.0, 0.0, 1.0], [0.0, 2.0, 1.0]]),
        )
        return SentenceTransformer(
            modules=[bow, dense, models.Normalize()],
            device="cpu",
            local_files_only=True,
        )


def test_real_cpu_encode_preserves_values_batch_order_and_output_types(
    local_cpu_model: SentenceTransformer,
) -> None:
    sentences = ["beta beta", "alpha", "alpha gamma", "gamma"]
    expected = torch.tensor(
        [[0.0, 1.0], [1.0, 0.0], [2 / math.sqrt(5), 1 / math.sqrt(5)], [1 / math.sqrt(2)] * 2]
    )
    actual = local_cpu_model.encode(sentences, batch_size=2, convert_to_tensor=True)
    default = local_cpu_model.encode(sentences, batch_size=3)
    assert isinstance(actual, torch.Tensor) and isinstance(default, np.ndarray)
    assert actual.shape == default.shape == (4, 2)
    assert local_cpu_model.get_sentence_embedding_dimension() == 2
    assert actual.device.type == local_cpu_model.device.type == "cpu"
    assert torch.version.cuda is None and torch.version.hip is None
    assert torch.isfinite(actual).all()
    torch.testing.assert_close(actual, expected)
    np.testing.assert_allclose(default, expected.numpy(), rtol=1e-6, atol=1e-7)


def test_real_cpu_encode_empty_and_unknown_text_stays_finite(
    local_cpu_model: SentenceTransformer,
) -> None:
    actual = local_cpu_model.encode(["", "outside vocabulary"], convert_to_tensor=True)
    assert actual.shape == (2, 2) and actual.device.type == "cpu"
    assert torch.isfinite(actual).all()
    torch.testing.assert_close(actual, torch.zeros((2, 2)))


def test_vector_store_uses_real_cpu_encode_and_reuses_cached_list(
    local_cpu_model: SentenceTransformer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    qdrant = MagicMock()
    model_factory = MagicMock(return_value=local_cpu_model)
    monkeypatch.setattr(vector, "QdrantClient", qdrant)
    monkeypatch.setattr(vector, "SentenceTransformer", model_factory)
    monkeypatch.setattr(vector.VectorStore, "_client_cache", {})
    store = vector.VectorStore(base_dir=tmp_path, model_name="p11-local-bow")
    model_factory.assert_not_called()
    with patch.object(local_cpu_model, "encode", wraps=local_cpu_model.encode) as encode:
        first = store.get_embedding("alpha gamma")
        repeated = store.get_embedding("alpha gamma")
        other = store.get_embedding("beta beta")
    assert isinstance(first, list) and all(isinstance(value, float) for value in first)
    assert first == pytest.approx([2 / math.sqrt(5), 1 / math.sqrt(5)])
    assert repeated is first
    assert other == pytest.approx([0.0, 1.0])
    assert encode.call_args_list == [call("alpha gamma"), call("beta beta")]
    model_factory.assert_called_once_with("p11-local-bow", trust_remote_code=False)
    assert (tmp_path / "_qdrant_db").is_dir()  # 真的 resource proxy 建的（ADR-0293 T3）
    qdrant.assert_called_once_with(path=str((tmp_path / "_qdrant_db").absolute()))
