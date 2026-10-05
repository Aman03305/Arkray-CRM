"""Pipeline routes. `workspace_urlpatterns` are mounted under /api/v1/workspaces/<workspace>/,
`config_urlpatterns` under /api/v1/config/. Every route is listed in tests/authz_matrix.py.

Lead conversion lives here (not in leads) because it creates an opportunity: the leads
module never imports the pipeline module (docs/architecture.md#dependency-rules)."""

from django.urls import path

from . import views

OPPORTUNITY = "opportunities/<uuid:opportunity_id>"
PIPELINE = "pipelines/<uuid:pipeline_id>"

workspace_urlpatterns = [
    path("pipelines", views.WorkspacePipelinesView.as_view(), name="workspace-pipelines"),
    path(PIPELINE, views.WorkspacePipelineView.as_view(), name="workspace-pipeline"),
    path(f"{PIPELINE}/stages", views.PipelineStagesView.as_view(), name="pipeline-stages"),
    path(f"{PIPELINE}/fields", views.PipelineFieldsView.as_view(), name="pipeline-fields"),
    path(f"{PIPELINE}/archive", views.PipelineArchiveView.as_view(), name="pipeline-archive"),
    path(f"{PIPELINE}/restore", views.PipelineRestoreView.as_view(), name="pipeline-restore"),
    path("pipeline-board", views.BoardView.as_view(), name="pipeline-board"),
    path("pipeline-summary", views.PipelineSummaryView.as_view(), name="pipeline-summary"),
    path("opportunities", views.OpportunityListView.as_view(), name="opportunities"),
    path(OPPORTUNITY, views.OpportunityDetailView.as_view(), name="opportunity"),
    path(f"{OPPORTUNITY}/move", views.OpportunityMoveView.as_view(), name="opportunity-move"),
    path(f"{OPPORTUNITY}/assign", views.OpportunityAssignView.as_view(), name="opportunity-assign"),
    path(
        f"{OPPORTUNITY}/archive", views.OpportunityArchiveView.as_view(), name="opportunity-archive"
    ),
    path(
        f"{OPPORTUNITY}/restore", views.OpportunityRestoreView.as_view(), name="opportunity-restore"
    ),
    path(
        f"{OPPORTUNITY}/history", views.OpportunityHistoryView.as_view(), name="opportunity-history"
    ),
    path(
        f"{OPPORTUNITY}/negotiated-prices",
        views.OpportunityNegotiatedPricesView.as_view(),
        name="opportunity-negotiated-prices",
    ),
    path("leads/<uuid:lead_id>/convert", views.LeadConvertView.as_view(), name="lead-convert"),
]

config_urlpatterns = [
    path("pipelines", views.PipelineConfigView.as_view(), name="pipelines"),
]
