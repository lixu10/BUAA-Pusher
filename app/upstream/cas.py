"""Narrow HSTS-compatible normalization for the school's CAS transport."""
import re


def secure_cas_transport_url(url: str) -> str:
    # Replace only the outer origin, never decode/re-encode the CAS service.
    # Native HTTP clients do not inherit the browser's HSTS state, and an HTTP
    # hop cannot carry the Secure SSO cookies needed to exchange a UC ticket.
    return re.sub(r"^http://sso\.buaa\.edu\.cn(?::80)?(?=[/?#]|$)",
                  "https://sso.buaa.edu.cn", url, count=1, flags=re.IGNORECASE)
