import sys
from pathlib import Path

import pytest

# 讓 `import app.xxx` 在不 pip install -e . 的情況下也能運作。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """**測試污染修正**：許多寫入函式（`append_audit()`／`Database()` 等）
    對相對路徑（`audit.jsonl`／`jobqueue.db` 等）有預設值，pytest 的 cwd
    是專案根目錄——沒有這條防線的話，任何沒有明確帶 `audit_path`/`db_path`
    的單元測試（例如直接呼叫 `request_*_approval()`/`approve()` 而沒傳
    `audit_path` 的測試）會不小心把紀錄寫進正式環境的 `audit.jsonl`（已經
    真的發生過，2026-07-09 09:16-09:17 的污染紀錄就是這樣來的）。

    這裡對**每個測試**都 `monkeypatch.chdir(tmp_path)`，讓預設相對路徑落在
    暫存目錄，不會碰到專案根目錄的真實檔案。`app/main.py` 的
    `STATIC_DIR`／`app_state.config.*_path` 等本來就是用
    `Path(__file__)` 絕對路徑或由 fixture 明確指定，不受影響。

    **不清理、不改寫**正式的 `audit.jsonl`——稽核檔案是 append-only 的
    歷史，已經污染的紀錄留著，這條 fixture 只負責讓污染不再發生。
    """
    monkeypatch.chdir(tmp_path)
    # Goal 1 auth transport switches must never inherit host/deployment
    # settings. Individual tests may override these deterministic defaults.
    monkeypatch.setenv("LEGACY_SHARED_TOKEN_ENABLED", "true")
    monkeypatch.setenv("SERVICE_TOKEN_AUTH_ENABLED", "false")
    monkeypatch.setenv("AUTHORIZATION_MODE", "off")
    monkeypatch.setenv("IDENTITY_ADMIN_ENABLED", "false")
    monkeypatch.setenv("ENGINEERING_TASK_BACKEND_V1", "false")
    monkeypatch.setenv("SESSION_COOKIE_NAME", "dispatch_session")
    monkeypatch.setenv("DISPATCH_SERVICE_TOKEN", "")
    # Slice 7 OIDC must never inherit a developer's real provider, client, or
    # bootstrap subjects.  Empty credentials plus OIDC_ENABLED=false guarantee
    # that ordinary tests cannot perform discovery/token/JWKS network calls.
    monkeypatch.setenv("OIDC_ENABLED", "false")
    monkeypatch.setenv("OIDC_ISSUER", "")
    monkeypatch.setenv("OIDC_CLIENT_ID", "")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "")
    monkeypatch.setenv("OIDC_SCOPES", "openid profile email")
    monkeypatch.setenv("OIDC_PLATFORM_ADMIN_SUBJECTS", "")
    monkeypatch.setenv("OIDC_LOGIN_FLOW_TTL_SEC", "600")
    monkeypatch.setenv("OIDC_SESSION_TTL_SEC", "28800")
    monkeypatch.setenv("OIDC_FLOW_COOKIE_NAME", "dispatch_oidc_flow")
    monkeypatch.setenv("OIDC_PROVIDER_TIMEOUT_SEC", "10")
    monkeypatch.setenv("OIDC_CLOCK_SKEW_LEEWAY_SEC", "60")


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


@pytest.fixture
def audit_path(tmp_path) -> str:
    return str(tmp_path / "audit.jsonl")


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """啟動一個乾淨的 FastAPI TestClient：DB/audit 檔都在 tmp_path，不碰
    真實 SSH（沒有 servers.yaml 時 config.servers 為空列表，monitor/scheduler
    背景迴圈跑起來也不會嘗試連線任何機器）。

    回傳 (client, main_module)，測試可透過 main_module.app_state 直接操作
    DB，或注入假的 ssh_run/ssh_write_file 來測 stop 流程與 log 端點。
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    #: 階段 8 第二批：`SERVERS_YAML_PATH` 指向一個 tmp_path 底下**不存在**
    #: 的檔案（`load_servers_yaml()` 對不存在的檔案回傳空列表，維持既有
    #: 「servers.yaml 不存在時 config.servers 為空列表」的測試假設）。
    #: **這條非常重要**：專案根目錄有一份真實的 servers.yaml（部署中的機器
    #: 設定），server_add/server_update/server_disable/server_delete 核准後
    #: 會用 `app_state.config.servers_yaml_path` 做 atomic write——沒有這行
    #: 的話，任何測試核准這幾種 approval 都會不小心覆寫專案根目錄的真實
    #: servers.yaml。個別測試需要讓 `POST /server-config/*` 有實際檔案可讀
    #: 寫時，請在測試內用 `app.server_config.write_servers_yaml_atomically()`
    #: 或直接寫檔到這個路徑，並同步設定 `main_module.app_state.server_configs`。
    monkeypatch.setenv("SERVERS_YAML_PATH", str(tmp_path / "servers.yaml"))
    #: 同上理由：`validate_server_config()` 預設只允許 `~/.ssh`（真實使用者
    #: 家目錄），測試用的假私鑰檔案放在 tmp_path 底下，指向這裡才能通過
    #: 驗證（見 tests/test_server_config_api.py）。
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    #: 階段 7：這台機器的 .env / 實際環境變數可能設定了 VLLM_BASE_URL（例如
    #: 本機另外跑一個 vLLM 實例做別的實驗），但既有測試假設「沒有設定
    #: VLLM_BASE_URL」天然走舊路徑（WS /ws 用 app.chat.handle_chat_text()）
    #: ——這裡明確清成空字串（不能只 delenv：`load_dotenv()` 只在環境變數
    #: 「不存在」時才會用 .env 檔內容補上，delenv 之後 .env 裡的設定反而會
    #: 生效，必須用空字串蓋掉），避免測試結果受執行環境影響（PLAN.md H 節：
    #: 既有 289 條測試不依賴、也不因為本地是否真的有 vLLM 而改變行為）。
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module
