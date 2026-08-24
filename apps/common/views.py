"""Health check (spec §9.5)."""

from django.db import connection
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        db_ok = True
        try:
            connection.ensure_connection()
        except Exception:
            db_ok = False

        return Response({'data': {'status': 'ok', 'service': 'opencloud-backend', 'database': db_ok}})
