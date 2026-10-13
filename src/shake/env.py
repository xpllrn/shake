"""Environment and proxy helpers, with rotating proxy credentials support (Meta Muse)."""

import os
import urllib.parse


def get_proxy_credentials_from_env(proxy_url: str | None = None) -> str | None:
    """Resolve proxy URL, dynamically injecting credentials from environment variables.

    Specifically handles sandboxes such as Meta Muse where egress proxy credentials
    rotate every shell session and stale saved credentials would trigger HTTP 407 errors.
    Checks HTTPS_PROXY, HTTP_PROXY, and ALL_PROXY in order.
    """
    for env_var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        val = os.environ.get(env_var)
        if not val:
            continue
        parsed_env = urllib.parse.urlsplit(val)
        if parsed_env.username and parsed_env.password:
            if proxy_url:
                parsed_base = urllib.parse.urlsplit(proxy_url)
                # Combine credentials from env with host and port from base proxy URL
                user_pass = (
                    f"{urllib.parse.quote(parsed_env.username)}:"
                    f"{urllib.parse.quote(parsed_env.password)}"
                )
                netloc = f"{user_pass}@{parsed_base.hostname}"
                if parsed_base.port:
                    netloc += f":{parsed_base.port}"
                return urllib.parse.urlunsplit(
                    (parsed_base.scheme or "http", netloc, parsed_base.path, "", "")
                )
            return val
    return proxy_url
