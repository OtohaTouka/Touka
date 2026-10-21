"""User models for ToukaProxy."""

from pydantic import BaseModel, EmailStr, Field  # noqa: TC002


class UserBase(BaseModel):
    """Base user model containing common fields."""
    email: EmailStr
    role: str = Field(default="normal")
    display_name: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    status: str = Field(default="active") # active, suspended

    @property
    def is_active(self) -> bool:
        """DA23: True only when the account is not suspended — the single source of
        truth for the suspension check enforced at every auth gate. (Plain property,
        not a field, so it is never serialised into API responses.)"""
        return self.status == "active"

class UserCreate(UserBase):
    """Model for creating a new user."""
    password: str

class User(UserBase):
    """Full user model including sensitive and internal fields."""
    id: str
    hashed_password: str
    is_temp_password: bool = False
    # 修復 1（07-11 事故餘波）：最近一次密碼變更的 epoch 秒。此前簽發（iat 較小或
    # 無 iat）的 access token 於兩道 Bearer 閘一律失效——改密碼不再留下 1h 殘活窗。
    password_changed_at: int | None = None
    # P3（認證縫）：最近一次登出/撤銷的 epoch 秒＝access 撤銷水位（與 password_changed_at
    # 取 max）。此前簽發的 access token 立即失效——登出即撤，不再殘活至 1h 自然過期。
    tokens_revoked_at: int | None = None
    refresh_token_exp: int | None = None
    refresh_token_jti: str | None = None      # P13 fix: jti for replay detection
    previous_refresh_jti: str | None = None   # Grace period: JTI before last rotation
    refresh_jti_rotated_at: int | None = None # Grace period: epoch when current JTI was issued
    last_active_at: int | None = None
    created_at: int | None = None
    owned_agents: list[str] = Field(default_factory=list)

class UserOut(UserBase):
    """User model for public API responses, excluding sensitive data."""
    id: str
    refresh_token_exp: int | None = None
    last_active_at: int | None = None
    created_at: int | None = None
    owned_agents: list[str] = Field(default_factory=list)
