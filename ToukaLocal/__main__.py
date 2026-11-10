"""小幫手的入口：``pythonw.exe -I -S <這個資料夾>``（ADR-0311）。"""

import sys

if sys.platform != "win32":
    sys.stderr.write("Touka 本機小幫手只支援 Windows\n")
    raise SystemExit(1)

from touka_local.app import main

raise SystemExit(main())
