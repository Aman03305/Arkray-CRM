"""OpenAPI (drf-spectacular) extensions. Imported from CoreConfig.ready()."""

from drf_spectacular.authentication import SessionScheme


class ArkraySessionScheme(SessionScheme):  # type: ignore[no-untyped-call]
    """Describe arkray.core.authentication.SessionAuthentication like DRF's own."""

    target_class = "arkray.core.authentication.SessionAuthentication"
    name = "sessionAuth"
