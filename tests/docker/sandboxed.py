"""先開 smoke 沙盒，再執行指定的檢查模組（ADR-0214、禁令 63：沙盒外不載入專案程式）.

用法：``python -B tests/docker/sandboxed.py <模組> [模組的參數…]``，例如
``tests.docker.executor_check``。這支只匯入標準函式庫與 ``sandbox_env``；檢查模組在沙盒開好之後才由
``runpy`` 載入，所以它們的 ``src`` 匯入照常寫在模組頂部。
"""

from __future__ import annotations

import runpy
import sys

from tests.smoke.lib.sandbox_env import enable_smoke_sandbox

CHECK_MODULES = frozenset({"tests.docker.executor_check", "tests.docker.landlock_proxy_check"})


def main() -> None:
    """開沙盒（換掉資料、設定、.env 的位置並封鎖外網），再以 ``__main__`` 執行檢查模組."""
    module, *args = sys.argv[1:]
    if module not in CHECK_MODULES:
        sys.stderr.write(f"unknown check module: {module}\n")
        raise SystemExit(2)
    enable_smoke_sandbox(module.rsplit(".", 1)[-1])
    sys.argv = [module, *args]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
