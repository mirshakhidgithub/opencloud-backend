"""
Audit trail (spec §6.3, plan §7).

Every state-changing action a user performs through the cabinet is recorded
here — who did what, to which resource, in which project, and whether the cloud
accepted it. Reads are not recorded: the volume would drown the signal.
"""

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    SUCCESS = 'SUCCESS'
    FAILURE = 'FAILURE'
    OUTCOME_CHOICES = [(SUCCESS, 'Success'), (FAILURE, 'Failure')]

    # Kept even when the local user row is deleted: an audit trail with holes
    # in it is not an audit trail.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='audit_logs')
    username = models.CharField(max_length=255)
    account = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=128, blank=True)
    project_name = models.CharField(max_length=255, blank=True)

    action = models.CharField(max_length=64)  # e.g. 'vm.start'
    resource_type = models.CharField(max_length=64, blank=True)  # e.g. 'vm'
    resource_id = models.CharField(max_length=128, blank=True)
    resource_name = models.CharField(max_length=255, blank=True)

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=SUCCESS)
    error_code = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['action', '-created_at']),
            models.Index(fields=['resource_id', '-created_at']),
        ]

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.username} {self.action} {self.outcome}'
