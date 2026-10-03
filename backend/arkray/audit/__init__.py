"""Append-only security/compliance audit trail.

Depends only on `core`. Actors and targets are stored as plain identifiers (no foreign
keys) so audit history is independent of the lifecycle of the records it describes.
"""
