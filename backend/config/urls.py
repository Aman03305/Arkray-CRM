"""Root URL configuration.

`/health/*` — liveness/readiness probes (unauthenticated, no infrastructure details).
`/api/v1/*` — the versioned JSON API. Every API route must be registered in the
authorization matrix (tests/authz_matrix.py); tests/architecture enforces this.
"""

from django.urls import URLPattern, URLResolver, include, path

from arkray.activities.api import urls as activities_urls
from arkray.ai.api import urls as ai_urls
from arkray.dashboard.api import urls as dashboard_urls
from arkray.identity.api import urls as identity_urls
from arkray.leads.api import urls as leads_urls
from arkray.pipeline.api import urls as pipeline_urls
from arkray.search.api import urls as search_urls
from config.metrics import metrics_view

# Module routers are added here phase by phase.
api_v1_patterns: list[URLPattern | URLResolver] = [
    path("auth/", include(identity_urls.auth_urlpatterns)),
    path("admin/", include(identity_urls.admin_urlpatterns)),
    path("assignees", include(identity_urls.directory_urlpatterns)),
    path("workspaces/", include(identity_urls.workspace_urlpatterns)),
    path("workspaces/<str:workspace>/", include(leads_urls.workspace_urlpatterns)),
    path("workspaces/<str:workspace>/", include(pipeline_urls.workspace_urlpatterns)),
    path("workspaces/<str:workspace>/", include(activities_urls.workspace_urlpatterns)),
    path("workspaces/<str:workspace>/", include(dashboard_urls.workspace_urlpatterns)),
    path("workspaces/<str:workspace>/", include(search_urls.workspace_urlpatterns)),
    path("workspaces/<str:workspace>/", include(ai_urls.workspace_urlpatterns)),
    path("config/", include(leads_urls.config_urlpatterns)),
    path("config/", include(pipeline_urls.config_urlpatterns)),
]

urlpatterns = [
    # Operational gauges for a scraper with METRICS_TOKEN (404 otherwise); before the
    # probes' include so `health/metrics` resolves here.
    path("health/metrics", metrics_view, name="health-metrics"),
    path("health/", include("arkray.core.health_urls")),
    path("api/v1/", include((api_v1_patterns, "api"), namespace="v1")),
]

handler400 = "arkray.core.views.bad_request"
handler403 = "arkray.core.views.permission_denied"
handler404 = "arkray.core.views.not_found"
handler500 = "arkray.core.views.server_error"
