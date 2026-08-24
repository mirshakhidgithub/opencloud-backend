"""Read-only serialization of the audit trail for the admin endpoint."""

from rest_framework import serializers

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    resourceType = serializers.CharField(source='resource_type', read_only=True)
    resourceId = serializers.CharField(source='resource_id', read_only=True)
    resourceName = serializers.CharField(source='resource_name', read_only=True)
    projectName = serializers.CharField(source='project_name', read_only=True)
    errorCode = serializers.CharField(source='error_code', read_only=True)
    ipAddress = serializers.CharField(source='ip_address', read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            'id',
            'createdAt',
            'username',
            'account',
            'projectName',
            'action',
            'resourceType',
            'resourceId',
            'resourceName',
            'outcome',
            'errorCode',
            'detail',
            'ipAddress',
        ]
