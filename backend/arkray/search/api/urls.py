"""Search routes, mounted under /api/v1/workspaces/<workspace>/. Listed in
tests/authz_matrix.py."""

from django.urls import path

from . import views

workspace_urlpatterns = [
    path("search", views.SearchView.as_view(), name="search"),
]
