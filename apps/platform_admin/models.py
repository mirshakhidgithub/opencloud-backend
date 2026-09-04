"""
Platform operators — the people who run OpenCloud, not the people who buy it.

Deliberately NOT `accounts.User`. That model is a mirror of a Zadara identity:
rows appear there by themselves on first cabinet login, and `upsert_user` keeps
overwriting them from whatever the cloud says. An identity that can see all 21
accounts must not live in a table the cloud can write to, and must not be one
`is_platform_admin=True` away from a customer row.

So this is a second, self-contained identity: local password, mandatory TOTP,
no Zadara link at all. It is not AUTH_USER_MODEL — `PlatformSessionAuthentication`
resolves it from its own session key, which means a valid cabinet session
replayed at admin.opencloud.uz authenticates as nobody.
"""

from django.contrib.auth.hashers import check_password, is_password_usable, make_password
from django.db import models
from django.utils import timezone

# Roles inside the admin panel itself. Everyone can read; these gate the writes.
OWNER = 'OWNER'  # everything, including managing other operators
OPS = 'OPS'  # quotas, suspend/resume, account lifecycle
SUPPORT = 'SUPPORT'  # read + impersonate, no destructive actions
FINANCE = 'FINANCE'  # billing and reports only

ROLE_CHOICES = [(OWNER, 'Owner'), (OPS, 'Operations'), (SUPPORT, 'Support'), (FINANCE, 'Finance')]

# Roles allowed to change platform state (disable users, suspend accounts).
WRITE_ROLES = frozenset({OWNER, OPS})

# After this many consecutive failures the account stops answering for a while.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


class PlatformAdmin(models.Model):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255)
    password_hash = models.CharField(max_length=255)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=SUPPORT)

    is_active = models.BooleanField(default=True)

    # Base32 secret, Fernet-encrypted at rest (see `totp.py`). Empty until the
    # first sign-in walks the operator through enrolment.
    totp_secret = models.CharField(max_length=255, blank=True)
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)

    failed_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    last_login_at = models.DateTimeField(null=True, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'platform_admins'
        ordering = ['email']

    def __str__(self):
        return f'{self.email} ({self.role})'

    # -- DRF/Django duck-typing -------------------------------------------- #
    # Not an AUTH_USER_MODEL instance, but `request.user` is expected to answer
    # these. Anything that reads them gets an honest answer instead of an
    # AttributeError that a permission class might swallow into "allowed".

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    # -- Password ----------------------------------------------------------- #

    def set_password(self, raw: str) -> None:
        self.password_hash = make_password(raw)

    def check_password(self, raw: str) -> bool:
        if not is_password_usable(self.password_hash):
            return False
        return check_password(raw, self.password_hash)

    # -- Lockout ------------------------------------------------------------ #

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > timezone.now())

    def note_failure(self) -> None:
        self.failed_attempts += 1
        if self.failed_attempts >= MAX_FAILED_ATTEMPTS:
            self.locked_until = timezone.now() + timezone.timedelta(minutes=LOCKOUT_MINUTES)
            self.failed_attempts = 0
        self.save(update_fields=['failed_attempts', 'locked_until'])

    def note_success(self, ip: str | None) -> None:
        self.failed_attempts = 0
        self.locked_until = None
        self.last_login_at = timezone.now()
        self.last_login_ip = ip
        self.save(update_fields=['failed_attempts', 'locked_until', 'last_login_at', 'last_login_ip'])

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES


class AdminAction(models.Model):
    """
    What operators did, kept apart from `audit.AuditLog`.

    Two reasons not to share that table: its `user` column points at
    AUTH_USER_MODEL, and — more to the point — the log of who watched the
    watchmen should not be mixed into the log they are watching.
    """

    SUCCESS = 'SUCCESS'
    FAILURE = 'FAILURE'
    OUTCOME_CHOICES = [(SUCCESS, 'Success'), (FAILURE, 'Failure')]

    actor = models.ForeignKey(PlatformAdmin, null=True, on_delete=models.SET_NULL, related_name='actions')
    actor_email = models.EmailField()  # kept verbatim: an operator row may be deleted

    action = models.CharField(max_length=64)  # e.g. 'user.disable', 'account.suspend'
    target_account = models.CharField(max_length=255, blank=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=128, blank=True)
    target_name = models.CharField(max_length=255, blank=True)

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SUCCESS)
    error_code = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'platform_admin_actions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['action', '-created_at']),
            models.Index(fields=['target_account', '-created_at']),
        ]

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.actor_email} {self.action} {self.outcome}'
