/**
 * 金鑰遮罩佔位：後端 `ToukaProxy/src/utils/secrets.py` 的 `MASK` 的鏡像（那邊是正本）。
 * 代表「已設定，但這裡不帶真值」；後端收到它就保留原本的金鑰，不當成新金鑰。
 * 兩邊必須逐字相同，`tests/lint/test_connection_key_mask_security.py` 釘住。
 */
export const SECRET_MASK = "••••••••";
