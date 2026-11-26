"""Registry discovery: registry identification, auth, tag listing and manifest lookups.

Docker Hub  -> hub.docker.com tags API (newest first, supports name filter), falling back to OCI
Others      -> OCI Distribution Spec ``/v2/<name>/tags/list`` with Link-header pagination and the
               standard ``WWW-Authenticate: Bearer`` anonymous token flow (GHCR, Quay, ECR Public,
               GAR/GCR, MCR, registry.k8s.io, Red Hat ...)
Registries that demand credentials (Basic challenge / 401 with no token) raise RegistryError(kind="auth")
and the caller marks the image SKIPPED with the reason - nothing is guessed.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin, urlencode

import requests

from .logger import get_logger, register_secret
from .models import ImageRef

log = get_logger("registry")

_KNOWN = [
    (lambda h: h == "docker.io", "Docker Hub"),
    (lambda h: h == "ghcr.io", "GHCR"),
    (lambda h: h == "public.ecr.aws", "ECR Public"),
    (lambda h: h == "gcr.io" or h.endswith(".gcr.io") or h.endswith(".pkg.dev"), "Google Artifact Registry"),
    (lambda h: h.endswith(".azurecr.io"), "Azure ACR"),
    (lambda h: h == "mcr.microsoft.com", "Microsoft MCR"),
    (lambda h: h == "container-registry.oracle.com", "Oracle Container Registry"),
    (lambda h: h == "quay.io", "Quay.io"),
    (lambda h: h in {"registry.access.redhat.com", "registry.redhat.io"}, "Red Hat Registry"),
    (lambda h: h in {"registry.k8s.io", "k8s.gcr.io"}, "Kubernetes Registry"),
]

_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def registry_label(registry: str) -> str:
    h = registry.lower()
    for pred, label in _KNOWN:
        if pred(h):
            return label
    return f"OCI Registry ({registry})"


class RegistryError(Exception):
    """kind: auth | not_found | rate_limit | network | server | unsupported"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class TagListing:
    tags: list[str]
    complete: bool = True
    source: str = "oci"


class RegistryClient:
    def __init__(self, session: Optional[requests.Session] = None, timeout: float = 20, retries: int = 3,
                 backoff: float = 1.5, max_pages: int = 30, sleep: Callable[[float], None] = time.sleep,
                 env: Optional[dict] = None):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.backoff = backoff
        self.max_pages = max_pages
        self.sleep = sleep
        env = os.environ if env is None else env
        self._hub_user = env.get("DOCKERHUB_USERNAME")
        self._hub_token = env.get("DOCKERHUB_TOKEN")
        register_secret(self._hub_token)
        self._tokens: dict[tuple[str, str], str] = {}
        self._tag_cache: dict[tuple, TagListing] = {}

    # ------------------------------------------------------------------ HTTP
    def _http(self, method: str, url: str, headers: Optional[dict] = None, auth=None) -> requests.Response:
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(method, url, headers=headers or {}, timeout=self.timeout,
                                            auth=auth, allow_redirects=True)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last = exc
                log.warning("network error on %s %s (attempt %d/%d): %s", method, url, attempt + 1,
                            self.retries + 1, type(exc).__name__)
                if attempt < self.retries:
                    self.sleep(self.backoff * (2 ** attempt))
                continue
            except requests.exceptions.RequestException as exc:
                raise RegistryError("network", f"request failed: {type(exc).__name__}") from exc
            if resp.status_code == 429 or (resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0"):
                if attempt < self.retries:
                    wait = min(float(resp.headers.get("Retry-After", self.backoff * (2 ** attempt)) or 1), 60)
                    log.warning("rate limited by %s, sleeping %.1fs", url, wait)
                    self.sleep(wait)
                    continue
                raise RegistryError("rate_limit", "registry rate limit exceeded")
            if resp.status_code >= 500 and attempt < self.retries:
                log.warning("server error %s from %s (attempt %d)", resp.status_code, url, attempt + 1)
                self.sleep(self.backoff * (2 ** attempt))
                continue
            if resp.status_code >= 500:
                raise RegistryError("server", f"registry returned HTTP {resp.status_code}")
            return resp
        raise RegistryError("network", f"network error after {self.retries + 1} attempts: "
                                       f"{type(last).__name__ if last else 'unknown'}")

    # ------------------------------------------------------------------ auth
    @staticmethod
    def _parse_challenge(header: str) -> tuple[str, dict]:
        scheme, _, rest = header.partition(" ")
        return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', rest))

    def _token_for(self, host: str, challenge: dict, repo: str) -> str:
        realm = challenge.get("realm")
        if not realm:
            raise RegistryError("auth", "registry sent a Bearer challenge without a realm")
        scope = challenge.get("scope") or f"repository:{repo}:pull"
        key = (host, scope)
        if key in self._tokens:
            return self._tokens[key]
        params = {"scope": scope}
        if challenge.get("service"):
            params["service"] = challenge["service"]
        auth = None
        if host.endswith("docker.io") and self._hub_user and self._hub_token:
            auth = (self._hub_user, self._hub_token)
        resp = self._http("GET", f"{realm}?{urlencode(params)}", auth=auth)
        if resp.status_code in (401, 403):
            raise RegistryError("auth", "anonymous token request refused - image is private or needs credentials")
        if resp.status_code != 200:
            raise RegistryError("auth", f"token endpoint returned HTTP {resp.status_code}")
        data = resp.json()
        tok = data.get("token") or data.get("access_token")
        if not tok:
            raise RegistryError("auth", "token endpoint returned no token")
        register_secret(tok)
        self._tokens[key] = tok
        return tok

    def _authed(self, method: str, host: str, url: str, repo: str, headers: Optional[dict] = None):
        headers = dict(headers or {})
        cached = next((t for (h, _s), t in self._tokens.items() if h == host and repo in _s), None)
        if cached:
            headers["Authorization"] = f"Bearer {cached}"
        resp = self._http(method, url, headers)
        if resp.status_code == 401:
            scheme, chal = self._parse_challenge(resp.headers.get("WWW-Authenticate", ""))
            if scheme != "bearer":
                raise RegistryError("auth", "registry requires credentials (non-anonymous authentication)")
            tok = self._token_for(host, chal, repo)
            headers["Authorization"] = f"Bearer {tok}"
            resp = self._http(method, url, headers)
        if resp.status_code == 401:
            raise RegistryError("auth", "registry requires credentials")
        if resp.status_code == 403:
            raise RegistryError("auth", "permission denied by registry")
        if resp.status_code == 404:
            raise RegistryError("not_found", "repository or tag not found (or private)")
        return resp

    @staticmethod
    def _api_host(registry: str) -> str:
        return "registry-1.docker.io" if registry == "docker.io" else registry

    # ------------------------------------------------------------------ tags
    def list_tags(self, ref: ImageRef, name_filter: Optional[str] = None) -> TagListing:
        key = (ref.registry, ref.repository, name_filter)
        if key in self._tag_cache:
            return self._tag_cache[key]
        if ref.registry == "docker.io":
            try:
                listing = self._hub_tags(ref, name_filter)
            except RegistryError as exc:
                if exc.kind in {"not_found", "auth"}:
                    raise
                log.warning("Docker Hub API failed (%s); falling back to OCI listing", exc)
                listing = self._oci_tags(ref)
        else:
            listing = self._oci_tags(ref)
        self._tag_cache[key] = listing
        return listing

    def _hub_tags(self, ref: ImageRef, name_filter: Optional[str]) -> TagListing:
        ns, _, name = ref.repository.partition("/")
        url = f"https://hub.docker.com/v2/repositories/{ns}/{name}/tags?page_size=100&ordering=last_updated"
        if name_filter:
            url += f"&name={name_filter}"
        tags: list[str] = []
        pages = 0
        while url and pages < self.max_pages:
            resp = self._http("GET", url)
            if resp.status_code == 404:
                raise RegistryError("not_found", "repository not found on Docker Hub")
            if resp.status_code in (401, 403):
                raise RegistryError("auth", "Docker Hub denied access")
            if resp.status_code != 200:
                raise RegistryError("unsupported", f"Docker Hub API returned HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise RegistryError("unsupported", "Docker Hub API returned invalid JSON") from exc
            tags += [r["name"] for r in data.get("results", []) if "name" in r]
            url = data.get("next")
            pages += 1
        return TagListing(tags, complete=url is None, source="dockerhub-api")

    def _oci_tags(self, ref: ImageRef) -> TagListing:
        host = self._api_host(ref.registry)
        url = f"https://{host}/v2/{ref.repository}/tags/list?n=1000"
        tags: list[str] = []
        pages = 0
        while url and pages < self.max_pages:
            resp = self._authed("GET", host, url, ref.repository)
            if resp.status_code != 200:
                raise RegistryError("unsupported", f"tag listing returned HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise RegistryError("unsupported", "registry returned invalid JSON for tag list") from exc
            tags += list(data.get("tags") or [])
            link = resp.headers.get("Link", "")
            m = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', link)
            url = urljoin(url, m.group(1)) if m else None
            pages += 1
        return TagListing(tags, complete=url is None, source="oci")

    # -------------------------------------------------------------- manifests
    def _manifest(self, method: str, ref: ImageRef, tag: str) -> requests.Response:
        host = self._api_host(ref.registry)
        url = f"https://{host}/v2/{ref.repository}/manifests/{tag}"
        return self._authed(method, host, url, ref.repository, {"Accept": _MANIFEST_ACCEPT})

    def tag_exists(self, ref: ImageRef, tag: str) -> bool:
        try:
            resp = self._manifest("HEAD", ref, tag)
        except RegistryError as exc:
            if exc.kind == "not_found":
                return False
            raise
        return resp.status_code == 200

    def get_digest(self, ref: ImageRef, tag: str) -> str:
        resp = self._manifest("HEAD", ref, tag)
        digest = resp.headers.get("Docker-Content-Digest")
        if resp.status_code != 200 or not digest:
            raise RegistryError("unsupported", "registry did not return a content digest")
        return digest
