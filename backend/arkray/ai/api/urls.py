"""Ask Arkray routes, mounted under /api/v1/workspaces/<workspace>/. Listed in
tests/authz_matrix.py."""

from django.urls import path

from . import views

workspace_urlpatterns = [
    path("ask", views.AskView.as_view(), name="ask"),
    path("ask/questions/<uuid:question_id>", views.QuestionView.as_view(), name="ask-question"),
    path("ask/conversations", views.ConversationListView.as_view(), name="ask-conversations"),
    path(
        "ask/conversations/<uuid:conversation_id>",
        views.ConversationView.as_view(),
        name="ask-conversation",
    ),
]
