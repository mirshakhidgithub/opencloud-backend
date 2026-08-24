from django.urls import path

from .views import ProjectSwitchView, ProjectsView

urlpatterns = [
    path('projects', ProjectsView.as_view(), name='user-projects'),
    path('projects/switch', ProjectSwitchView.as_view(), name='user-projects-switch'),
]
