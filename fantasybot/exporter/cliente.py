"""Read-only LALIGA Fantasy client used by the exporter."""

from fantasybot.api import FantasyClient, FantasyError


class ReadOnlyFantasyClient(FantasyClient):
    """FantasyClient transport guard that permits GET and nothing else.

    FantasyClient normally refreshes an expiring token, which performs an OAuth
    POST and rewrites tokens.json. The exporter refuses to refresh and disables
    the usual retry-on-401 path.
    """

    def refresh(self):  # pragma: no cover - defensive override
        raise FantasyError("Token refresh is disabled in read-only export mode.")

    def _request(self, method: str, path: str, body=None):
        if method.upper() != "GET" or body is not None:
            raise FantasyError("A non-GET request was blocked in read-only export mode.")
        if self._is_expiring():
            raise FantasyError(
                "The session needs renewal; refusing to change authentication "
                "in read-only export mode."
            )
        return self._do("GET", path, None, retry_on_401=False)
