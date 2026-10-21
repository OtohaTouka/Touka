from src.core.bootstrap import ensure_system_bootstrap, get_bootstrap_status


def test_config_self_healing(tmp_path):
    """驗證配置自癒邏輯：缺失時自動從 .example 復原"""
    # 模擬環境結構
    root = tmp_path / "touka_root"
    proxy_config = root / "ToukaProxy" / "config"
    proxy_config.mkdir(parents=True)
    
    # 建立範本
    example_env = root / ".env.example"
    example_env.write_text("TEST_VAR=true")
    
    example_json = proxy_config / "test_config.json.example"
    example_json.write_text('{"test": true}')
    
    # 執行引導 (此時 .env 與 .json 均缺失)
    ensure_system_bootstrap(root)
    
    # 驗證復原
    assert (root / ".env").exists()
    assert (root / ".env").read_text() == "TEST_VAR=true"
    
    assert (proxy_config / "test_config.json").exists()
    assert (proxy_config / "test_config.json").read_text() == '{"test": true}'

def test_bootstrap_status(tmp_path):
    """驗證引導狀態報告"""
    root = tmp_path / "touka_root"
    proxy_config = root / "ToukaProxy" / "config"
    proxy_config.mkdir(parents=True)
    
    (proxy_config / "missing.json.example").write_text("{}")
    
    status = get_bootstrap_status(root)
    assert "missing.json" in status["missing_configs"]
