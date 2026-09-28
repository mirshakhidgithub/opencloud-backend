"""
Local user model (spec §6.3, §11.4).

Users are not created manually — on first successful Zadara login a local record
is created and linked to `zadara_user_id`/account. Passwords are never stored
here (authentication is delegated to Zadara).
"""

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

from .roles import ROLE_CHOICES, USER


class UserManager(BaseUserManager):
    def create_user(self, zadara_user_id, **extra):
        if not zadara_user_id:
            raise ValueError('zadara_user_id is required')
        user = self.model(zadara_user_id=zadara_user_id, **extra)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, zadara_user_id, password=None, **extra):
        extra.setdefault('is_staff', True)
        extra.setdefault('is_superuser', True)
        extra.setdefault('app_role', 'ADMIN')
        user = self.model(zadara_user_id=zadara_user_id, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user


class User(AbstractBaseUser, PermissionsMixin):
    STATUS_ACTIVE = 'active'
    STATUS_BLOCKED = 'blocked'
    STATUS_CHOICES = [(STATUS_ACTIVE, 'Active'), (STATUS_BLOCKED, 'Blocked')]

    zadara_user_id = models.CharField(max_length=128, unique=True)
    username = models.CharField(max_length=255)
    email = models.EmailField(blank=True, null=True)

    account = models.CharField(max_length=255, blank=True)
    account_id = models.CharField(max_length=128, blank=True)

    app_role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=USER)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    date_joined = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = 'zadara_user_id'
    REQUIRED_FIELDS = []

    class Meta:
        db_table = 'users'

    def __str__(self):
        return f'{self.username}@{self.account}'

    @property
    def is_blocked(self) -> bool:
        return self.status == self.STATUS_BLOCKED


class AccountSettings(models.Model):
    """What the platform decided about one client account — not what the cloud reports.

    Keyed by the cloud's domain id, not the name: a renamed account keeps its
    settings, and a new account that takes an old name does not inherit them.
    `account` is only the spelling at the last write, for whoever reads the table.

    A row appears the first time an operator changes something. No row means
    every default.
    """

    domain_id = models.CharField(max_length=64, unique=True)
    account = models.CharField(max_length=255, blank=True, default='')

    # Snapshots a protection group takes on a schedule — backups run for the
    # client — are counted like any other while this is on. Off hides them from
    # the client's cabinet and leaves them off the bill; the snapshots the client
    # takes by hand stay visible and billed either way. See `apps.accounts.backups`.
    count_backup_snapshots = models.BooleanField(default=True)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'account_settings'

    def __str__(self):
        return f'settings of {self.account or self.domain_id}'
