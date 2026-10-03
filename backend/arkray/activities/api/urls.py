"""Activity routes, mounted under /api/v1/workspaces/<workspace>/. Every route is listed in
tests/authz_matrix.py.

The lead and opportunity timelines live here (not in leads or pipeline) because the
timeline belongs to this module: leads and pipeline never import activities
(docs/architecture.md#dependency-rules)."""

from django.urls import path

from . import views

ACTIVITY = "activities/<uuid:activity_id>"

workspace_urlpatterns = [
    path("activities", views.ActivityListView.as_view(), name="activities"),
    path("activity-summary", views.ActivitySummaryView.as_view(), name="activity-summary"),
    path(ACTIVITY, views.ActivityDetailView.as_view(), name="activity"),
    path(f"{ACTIVITY}/complete", views.ActivityCompleteView.as_view(), name="activity-complete"),
    path(f"{ACTIVITY}/cancel", views.ActivityCancelView.as_view(), name="activity-cancel"),
    path(f"{ACTIVITY}/reopen", views.ActivityReopenView.as_view(), name="activity-reopen"),
    path(f"{ACTIVITY}/archive", views.ActivityArchiveView.as_view(), name="activity-archive"),
    path(f"{ACTIVITY}/restore", views.ActivityRestoreView.as_view(), name="activity-restore"),
    path("leads/<uuid:lead_id>/timeline", views.LeadTimelineView.as_view(), name="lead-timeline"),
    path(
        "opportunities/<uuid:opportunity_id>/timeline",
        views.OpportunityTimelineView.as_view(),
        name="opportunity-timeline",
    ),
]
