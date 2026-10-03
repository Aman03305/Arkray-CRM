"""Lead routes. `workspace_urlpatterns` are mounted under /api/v1/workspaces/<workspace>/,
`config_urlpatterns` under /api/v1/config/. Every route is listed in tests/authz_matrix.py."""

from django.urls import path

from . import views

workspace_urlpatterns = [
    path("leads", views.LeadListView.as_view(), name="leads"),
    path("leads/duplicates", views.LeadDuplicatesView.as_view(), name="lead-duplicates"),
    path("leads/<uuid:lead_id>", views.LeadDetailView.as_view(), name="lead"),
    path("leads/<uuid:lead_id>/status", views.LeadStatusView.as_view(), name="lead-status"),
    path("leads/<uuid:lead_id>/assign", views.LeadAssignView.as_view(), name="lead-assign"),
    path("leads/<uuid:lead_id>/archive", views.LeadArchiveView.as_view(), name="lead-archive"),
    path("leads/<uuid:lead_id>/restore", views.LeadRestoreView.as_view(), name="lead-restore"),
]

config_urlpatterns = [
    path("lead-options", views.LeadOptionsView.as_view(), name="lead-options"),
]
