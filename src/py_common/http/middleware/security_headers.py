"""OWASP baseline security headers middleware (pure ASGI)."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Suitable for an API that serves JSON only: no scripts, styles, frames or
# images of its own, and not embeddable anywhere. It will break Swagger UI and
# ReDoc, which load their assets from a CDN — hence opt-in rather than default.
API_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"


class SecurityHeadersMiddleware:
    """Attach standard secure response headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        hsts: bool = True,
        hsts_max_age: int = 31536000,
        frame_options: str = "DENY",
        content_type_options: str = "nosniff",
        referrer_policy: str = "strict-origin-when-cross-origin",
        permissions_policy: str = "geolocation=(), microphone=(), camera=()",
        content_security_policy: str | None = None,
    ) -> None:
        """
        ``content_security_policy`` is off unless set, and has no default value
        on purpose. Every other header here is safe for any service, but a CSP
        describes one application's resources — the tight policy an API wants
        (:data:`API_CONTENT_SECURITY_POLICY`) blanks out ``/docs``, and a policy
        loose enough for ``/docs`` protects nothing. Shipping either as a
        default would mean silently breaking some services and silently failing
        to protect others.

        Set it per service, and if you serve interactive docs in production,
        either exclude their path or accept a policy that permits their CDN.
        """
        self.app = app
        self.hsts = hsts
        self.hsts_max_age = hsts_max_age
        self.frame_options = frame_options
        self.content_type_options = content_type_options
        self.referrer_policy = referrer_policy
        self.permissions_policy = permissions_policy
        self.content_security_policy = content_security_policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        include_hsts = self.hsts and scope.get("scheme") == "https"

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = self.content_type_options
                headers["X-Frame-Options"] = self.frame_options
                headers["Referrer-Policy"] = self.referrer_policy
                headers["Permissions-Policy"] = self.permissions_policy
                if self.content_security_policy:
                    headers["Content-Security-Policy"] = self.content_security_policy
                if include_hsts:
                    headers["Strict-Transport-Security"] = (
                        f"max-age={self.hsts_max_age}; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)
