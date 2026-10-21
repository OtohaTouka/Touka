"""插件管理器 CLI 腳本，提供對插件的列出、新增與刪除功能。"""

import argparse
import os
import shutil
from pathlib import Path


def list_plugins(plugin_dir):
    """列出插件目錄下所有的插件檔案、類型及其內建的工具。"""
    print(f"\n{'File':<20} | {'Type':<15} | {'Tools Detected'}")
    print("-" * 60)
    if not Path(plugin_dir).exists():
        print("Empty.")
        return

    for f in os.listdir(plugin_dir):
        if not f.endswith(".py") or f == "__init__.py":
            continue

        # 簡單偵測類型
        with Path(Path(plugin_dir).open() / f, encoding="utf-8") as file:
            content = file.read()
            p_type = (
                "Native"
                if "SCHEMA =" in content
                else "Open WebUI"
                if "class Tools" in content
                else "Unknown"
            )

            # 取得工具名稱
            tools = []
            if p_type == "Native":
                match = next(
                    (
                        l
                        for l in content.split("\n")
                        if "'name':" in l or '"name":' in l
                    ),
                    "N/A",
                )
                tools.append(
                    match.split(":")[-1].strip().strip(",").strip("'").strip('"')
                )
            elif p_type == "Open WebUI":
                # 這裡不實際執行，只做簡單匹配
                import re

                tools = re.findall(r"def (\w+)\(self", content)

            print(f"{f:<20} | {p_type:<15} | {', '.join(tools)}")


def add_plugin(src_path, plugin_dir):
    """進行語法檢查後，安全地將插件複製到 plugins 目錄。"""
    if not Path(src_path).exists():
        print(f"Error: File {src_path} not found.")
        return

    # 基本語法檢查
    try:
        with Path(src_path).open( encoding="utf-8") as f:
            compile(f.read(), src_path, "exec")
    except Exception as e:
        print(f"Error: Python syntax error in plugin: {e}")
        return

    filename = os.path.basename(src_path)
    dest_path = Path(plugin_dir) / filename
    shutil.copy2(src_path, dest_path)
    print(f"Success: Added {filename} to plugins.")


def remove_plugin(filename, plugin_dir):
    """從 plugins 目錄中安全地刪除指定的插件。"""
    if not filename.endswith(".py"):
        filename += ".py"

    target = Path(plugin_dir) / filename
    if Path(target).exists():
        os.remove(target)
        print(f"Success: Removed {filename}.")
    else:
        print(f"Error: Plugin {filename} not found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP Proxy Plugin Manager")
    parser.add_argument(
        "command", choices=["list", "add", "remove"], help="Command to run"
    )
    parser.add_argument(
        "target", nargs="?", help="File path for 'add' or Filename for 'remove'"
    )

    args = parser.parse_args()

    # 決定 plugin 目錄 (相對於此腳本位置)
    current_dir = Path(Path(__file__).resolve()).parent
    plugin_dir = Path(current_dir) / "src" / "plugins"
    Path(plugin_dir).mkdir(parents=True, exist_ok=True)

    if args.command == "list":
        list_plugins(plugin_dir)
    elif args.command == "add":
        if not args.target:
            print("Error: Missing file path for 'add'.")
        else:
            add_plugin(args.target, plugin_dir)
    elif args.command == "remove":
        if not args.target:
            print("Error: Missing filename for 'remove'.")
        else:
            remove_plugin(args.target, plugin_dir)
