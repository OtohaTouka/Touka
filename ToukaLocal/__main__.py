"""小幫手的入口：``pythonw.exe -I -S <這個資料夾>``（ADR-0311）。"""

import sys

if sys.platform != "win32":
    sys.stderr.write("Touka 本機小幫手只支援 Windows\n")
    raise SystemExit(1)

# 沒有 -I 時，環境變數和目前的資料夾都可能換掉要載入的程式
if not sys.flags.isolated:
    raise SystemExit("請用安裝時建立的啟動檔開啟小幫手")

from touka_local.app import main

raise SystemExit(main())
