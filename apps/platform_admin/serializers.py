"""Read-only serialization of the operator action log."""

from rest_framework import serializers

from .models import AdminAction


class AdminActionSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    actorEmail = serializers.CharField(source='actor_email', read_only=True)
    targetAccount = serializers.CharField(source='target_account', read_only=True)
    targetType = serializers.CharField(source='target_type', read_only=True)
    targetId = serializers.CharField(source='target_id', read_only=True)
    targetName = serializers.CharField(source='target_name', read_only=True)
    errorCode = serializers.CharField(source='error_code', read_only=True)
    ipAddress = serializers.CharField(source='ip_address', read_only=True)

    class Meta:
        model = AdminAction
        fields = [
            'id',
            'createdAt',
            'actorEmail',
            'action',
            'targetAccount',
            'targetType',
            'targetId',
            'targetName',
            'outcome',
            'errorCode',
            'detail',
            'ipAddress',
        ]
