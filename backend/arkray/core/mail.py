"""Outgoing email over verified TLS (docs/security.md#backend-tls).

Django's SMTP backend already verifies the server's certificate and host name with the
system CA store; this subclass makes that explicit and configurable: a private CA
(EMAIL_TLS_CA_FILE, e.g. an in-cluster relay's), TLS 1.2 at least, and a client certificate
when one is configured (Django's own context for that case loads no CA at all). STARTTLS
that the server doesn't offer is an error (smtplib), never a silent plain-text send.
"""

from __future__ import annotations

import ssl

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend
from django.utils.functional import cached_property


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=settings.EMAIL_TLS_CA_FILE or None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class VerifiedSMTPBackend(EmailBackend):
    @cached_property
    def ssl_context(self) -> ssl.SSLContext:
        context = tls_context()
        if self.ssl_certfile:
            context.load_cert_chain(self.ssl_certfile, self.ssl_keyfile)
        return context
