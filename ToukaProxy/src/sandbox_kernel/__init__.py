"""Touka 隔離啟動器核心（iso-c2）：只用標準函式庫，映像內另存一份在唯讀的信任根目錄.

proxy 與執行器以 ``importlib`` 依路徑載入本目錄的模組；啟動器以 ``python -I -S launcher.py``
執行。唯一的套件匯入是 proxy 端的 ``spec``（spec 上限、公開設定檔名、``kernel_digest``、
自測輸出解析的 SSOT，只用標準函式庫）。這個檔案刻意保持空白。
"""
