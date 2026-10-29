"""HTTP layer: one read-only session per Jenkins instance."""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests
from fastmcp.exceptions import ToolError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__
from .settings import JsonDict


class ReadOnlySession(requests.Session):
    """A requests.Session that physically cannot change anything on Jenkins."""

    ALLOWED = {"GET", "HEAD"}

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        if str(method).upper() not in self.ALLOWED:
            raise ToolError(f"Blocked {method} {url}: this MCP server is read-only.")
        return super().request(method, url, *args, **kwargs)


@dataclass
class Instance:
    name: str
    url: str
    user: str
    token: str
    user_env: str
    token_env: str
    verify: bool | str = True
    timeout: float = 60.0
    description: str = ""
    exclude_folders: tuple[str, ...] = ()
    proxy: str = ""  # "" = environment (HTTPS_PROXY/NO_PROXY), "none" = direct, else the proxy URL
    session: ReadOnlySession = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.url = self.url.rstrip("/")
        s = ReadOnlySession()
        if self.user or self.token:
            s.auth = (self.user, self.token)
        s.verify = self.verify
        s.headers["User-Agent"] = f"jenkins-mcp/{__version__} (read-only)"
        if self.proxy.lower() == "none":
            s.trust_env = False  # ignore HTTPS_PROXY & co. (verify is set explicitly above)
        elif self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        retry = Retry(
            total=2, connect=2, read=1, backoff_factor=0.5,
            status_forcelist=(502, 503, 504), allowed_methods=frozenset({"GET", "HEAD"}),
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        self.session = s

    # -- url handling -----------------------------------------------------------
    def owns(self, url: str) -> bool:
        a, b = urlparse(url), urlparse(self.url)
        return (
            a.scheme.lower() == b.scheme.lower()
            and a.netloc.lower() == b.netloc.lower()
            and (a.path + "/").startswith(b.path.rstrip("/") + "/")
        )

    def abs_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            if not self.owns(path_or_url):
                raise ToolError(
                    f"Refusing to call {path_or_url}: it is outside instance '{self.name}' "
                    f"({self.url}). Credentials are only sent to the configured Jenkins."
                )
            return path_or_url
        return self.url + ("" if path_or_url.startswith("/") else "/") + path_or_url

    # -- requests ---------------------------------------------------------------
    def get(self, path: str, *, params: JsonDict | None = None, stream: bool = False,
            allow_404: bool = False) -> requests.Response | None:
        url = self.abs_url(path)
        try:
            r = self.session.get(url, params=params, timeout=(10, self.timeout), stream=stream)
        except requests.exceptions.SSLError as e:
            raise ToolError(
                f"[{self.name}] TLS verification failed for {self.url}. Point 'ca_bundle' of this "
                f"instance to the internal CA certificate (PEM). Detail: {str(e)[:200]}"
            ) from None
        except requests.exceptions.Timeout:
            raise ToolError(f"[{self.name}] Timeout after {self.timeout}s calling {url}.") from None
        except requests.exceptions.ConnectionError as e:
            raise ToolError(
                f"[{self.name}] Cannot connect to {self.url} (VPN/proxy/DNS?). Detail: {str(e)[:200]}"
            ) from None
        if r.status_code == 404 and allow_404:
            r.close()
            return None
        if r.status_code >= 400:
            r.close()
            raise ToolError(self.http_error(r.status_code, url))
        return r

    def probe(self, path: str) -> int:
        """HTTP status of a GET without downloading the body (0 = no connection). Never raises."""
        try:
            r = self.session.get(self.abs_url(path), timeout=(10, self.timeout), stream=True)
        except (requests.exceptions.RequestException, ToolError):
            return 0
        r.close()
        return r.status_code

    def size(self, path: str) -> int | None:
        """Content-Length from a HEAD request (None if unknown or not reachable). Never raises."""
        try:
            r = self.session.head(self.abs_url(path), timeout=(10, self.timeout), allow_redirects=True)
        except (requests.exceptions.RequestException, ToolError):
            return None
        r.close()
        length = r.headers.get("Content-Length", "")
        return int(length) if r.status_code < 400 and length.isdigit() else None

    def json(self, path: str, tree: str | None = None, allow_404: bool = False) -> Any:
        r = self.get(path, params={"tree": tree} if tree else None, allow_404=allow_404)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError:
            raise ToolError(
                f"[{self.name}] Expected JSON from {path} but got '{r.headers.get('content-type')}'. "
                "Is the instance URL the Jenkins root URL?"
            ) from None

    def http_error(self, code: int, url: str) -> str:
        if code == 401:
            return (f"[{self.name}] Authentication failed (401). Check {self.user_env} / "
                    f"{self.token_env} in config/.env - it must be an API token, not the password.")
        if code == 403:
            return (f"[{self.name}] Permission denied (403) for {url}. The token's user lacks read "
                    "permission (Overall/Read, Job/Read) on this item.")
        if code == 404:
            return (f"[{self.name}] Not found (404): {url}. Check the job path with list_jobs. Folders are "
                    "separated by '/'; multibranch branch names containing '/' are written with %2F "
                    "(e.g. 'proj/release/release%2F2.4').")
        return f"[{self.name}] HTTP {code} from {url}."
