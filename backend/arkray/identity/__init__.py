"""Identity: users, roles, capabilities, authentication and workspace resolution.

Depends on `core` and `audit`. Domain modules ask this module *what* an actor may do
(capabilities) and *whose* records a request may touch (resolve_workspace -> AccessScope).
"""
