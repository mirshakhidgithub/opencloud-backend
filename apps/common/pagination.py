"""Pagination that emits the { data, meta } envelope (spec §7.1)."""

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class EnvelopePagination(PageNumberPagination):
    page_size_query_param = 'per_page'
    max_page_size = 200

    def get_paginated_response(self, data):
        return Response(
            {
                'data': data,
                'meta': {
                    'total': self.page.paginator.count,
                    'page': self.page.number,
                    'per_page': self.get_page_size(self.request),
                    'pages': self.page.paginator.num_pages,
                },
            }
        )
