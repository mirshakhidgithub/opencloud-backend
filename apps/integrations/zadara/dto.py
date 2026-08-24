"""Data transfer objects for the Zadara integration."""

from dataclasses import dataclass, field


@dataclass
class AuthResult:
    token: str
    scope: str  # 'project' | 'domain'
    user_id: str
    user_name: str
    email: str | None
    account_id: str | None
    account_name: str
    project_id: str | None
    project_name: str | None
    roles: list[str] = field(default_factory=list)
    expires_at: str | None = None


@dataclass
class ProjectSummary:
    id: str
    name: str
    description: str | None = None
    is_vpc: bool = False
