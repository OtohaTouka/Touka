"""記憶長期穩定性評估（不花錢的部分）。

- ``scenarios``／``judges``：純資料與純函式，匯入時不讀檔、不連網、不載入產品程式。
- ``seeding``：合成靈魂、合成事實與對話；只寫進呼叫端給的沙盒根之下。
- ``runner``：多輪 driver；送出點可注入（pytest 用假串流，人工 probe 用真 pipeline）。
- ``stats``：通過率範圍、兩次結果的比較工具與評估程式雜湊；純標準函式庫，不載入產品程式。

真模型實測只走 ``tests/smoke/manual/memory_eval_probe.py``，每批另經人類同意。
"""
