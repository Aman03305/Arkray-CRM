"""Privacy routes, mounted under /api/v1/admin/privacy/ (tests/authz_matrix.py)."""

from django.urls import path

from . import views

admin_urlpatterns = [
    path("exports", views.DataExportListView.as_view(), name="privacy-exports"),
    path("exports/<uuid:export_id>", views.DataExportDetailView.as_view(), name="privacy-export"),
    path(
        "exports/<uuid:export_id>/download",
        views.DataExportDownloadView.as_view(),
        name="privacy-export-download",
    ),
    path(
        "users/<uuid:user_id>/pseudonymise",
        views.PseudonymiseUserView.as_view(),
        name="privacy-pseudonymise-user",
    ),
]

# Mounted under /api/v1/admin/users/ (ADR-0036): the user-administration routes that erase.
account_urlpatterns = [
    path(
        "<uuid:user_id>/deletion-preview",
        views.DeletionPreviewView.as_view(),
        name="admin-user-deletion-preview",
    ),
    path("<uuid:user_id>/delete", views.DeleteAccountView.as_view(), name="admin-user-delete"),
]
