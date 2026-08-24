"""
Response envelope renderer (spec §7.1).

Wraps successful responses as { "data": ..., "meta": ... }. Payloads that are
already enveloped (contain a top-level "data" or "error" key) pass through
unchanged — so paginated responses and error responses are not double-wrapped.
"""

from rest_framework.renderers import JSONRenderer


class EnvelopeJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, dict) and ('data' in data or 'error' in data):
            payload = data
        else:
            payload = {'data': data}

        return super().render(payload, accepted_media_type, renderer_context)
