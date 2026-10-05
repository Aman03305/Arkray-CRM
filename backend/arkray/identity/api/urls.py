"""Identity routes, mounted under /api/v1/ by config.urls. Every route here is listed in
tests/authz_matrix.py with the access rule it implements."""

from django.urls import path

from . import views

auth_urlpatterns = [
    path("csrf", views.CsrfCookieView.as_view(), name="auth-csrf"),
    path("login", views.LoginView.as_view(), name="auth-login"),
    path("logout", views.LogoutView.as_view(), name="auth-logout"),
    path("me", views.MeView.as_view(), name="auth-me"),
    path("password/change", views.PasswordChangeView.as_view(), name="auth-password-change"),
    path("password-reset", views.PasswordResetRequestView.as_view(), name="auth-password-reset"),
    path(
        "password-reset/confirm",
        views.PasswordResetConfirmView.as_view(),
        name="auth-password-reset-confirm",
    ),
    path("invitations/verify", views.InvitationVerifyView.as_view(), name="auth-invitation-verify"),
    path("invitations/accept", views.InvitationAcceptView.as_view(), name="auth-invitation-accept"),
]

admin_urlpatterns = [
    path("users", views.AdminUserListView.as_view(), name="admin-users"),
    path("users/<uuid:user_id>", views.AdminUserDetailView.as_view(), name="admin-user"),
    path(
        "users/<uuid:user_id>/change-email",
        views.AdminUserEmailView.as_view(),
        name="admin-user-change-email",
    ),
    path(
        "users/<uuid:user_id>/deactivate",
        views.AdminUserDeactivateView.as_view(),
        name="admin-user-deactivate",
    ),
    path(
        "users/<uuid:user_id>/activate",
        views.AdminUserActivateView.as_view(),
        name="admin-user-activate",
    ),
    path(
        "users/<uuid:user_id>/resend-invitation",
        views.AdminUserResendInvitationView.as_view(),
        name="admin-user-resend-invitation",
    ),
    path(
        "users/<uuid:user_id>/set-password",
        views.AdminUserSetPasswordView.as_view(),
        name="admin-user-set-password",
    ),
    path("security-events", views.SecurityEventListView.as_view(), name="admin-security-events"),
    path("support-sessions", views.SupportSessionStartView.as_view(), name="support-sessions"),
    path(
        "support-sessions/current",
        views.SupportSessionCurrentView.as_view(),
        name="support-session-current",
    ),
]

workspace_urlpatterns = [
    path("<str:workspace>", views.WorkspaceView.as_view(), name="workspace"),
]

# Mounted at /api/v1/assignees.
directory_urlpatterns = [
    path("", views.AssigneeListView.as_view(), name="assignees"),
]
