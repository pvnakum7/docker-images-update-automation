import json

import pytest
import requests

from src.dockerfile_parser import parse_image_ref
from src.registry_manager import RegistryClient, RegistryError, registry_label


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Routes by URL substring; each route is a list of responses/exceptions consumed in order."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.log = []

    def request(self, method, url, headers=None, timeout=None, auth=None, allow_redirects=True):
        self.log.append((method, url, dict(headers or {})))
        for key, queue in self.routes.items():
            if key in url:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return item
        return Resp(404)


def client(routes, **kw):
    sleeps = []
    c = RegistryClient(session=FakeSession(routes), retries=kw.pop("retries", 2), backoff=1, sleep=sleeps.append, env={}, **kw)
    c._sleeps = sleeps
    return c


@pytest.mark.parametrize("host,label", [
    ("docker.io", "Docker Hub"), ("ghcr.io", "GHCR"), ("public.ecr.aws", "ECR Public"),
    ("us-docker.pkg.dev", "Google Artifact Registry"), ("gcr.io", "Google Artifact Registry"),
    ("myreg.azurecr.io", "Azure ACR"), ("container-registry.oracle.com", "Oracle Container Registry"),
    ("quay.io", "Quay.io"), ("registry.access.redhat.com", "Red Hat Registry"),
    ("registry.k8s.io", "Kubernetes Registry"), ("mcr.microsoft.com", "Microsoft MCR"),
    ("harbor.example.com", "OCI Registry (harbor.example.com)")])
def test_registry_labels(host, label):
    assert registry_label(host) == label


def test_docker_hub_tags_paginated_with_name_filter():
    base = "https://hub.docker.com/v2/repositories/library/node/tags"
    page1 = Resp(200, {"results": [{"name": "22.1.0"}, {"name": "22.0.0"}], "next": base + "?page=2"})
    page2 = Resp(200, {"results": [{"name": "22.2.0"}], "next": None})
    c = client({"page=2": [page2], base: [page1]})
    out = c.list_tags(parse_image_ref("node:22.0.0"), name_filter="22.")
    assert out.tags == ["22.1.0", "22.0.0", "22.2.0"] and out.complete and out.source == "dockerhub-api"
    assert "name=22." in c.session.log[0][1]


def test_docker_hub_not_found():
    c = client({"hub.docker.com": [Resp(404)]})
    with pytest.raises(RegistryError) as e:
        c.list_tags(parse_image_ref("nosuchuser/nosuchimage:1"))
    assert e.value.kind == "not_found"


def test_oci_bearer_token_flow_and_link_pagination():
    chal = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:org/api:pull"'
    c = client({
        "ghcr.io/token": [Resp(200, {"token": "tok123"})],
        "tags/list?n=1000&last=": [Resp(200, {"tags": ["v2.2"]})],
        "tags/list?n=1000": [Resp(401, headers={"WWW-Authenticate": chal}),
                             Resp(200, {"tags": ["v2.0", "v2.1"]}, {"Link": '</v2/org/api/tags/list?n=1000&last=v2.1>; rel="next"'})],
    })
    out = c.list_tags(parse_image_ref("ghcr.io/org/api:v2.0"))
    assert out.tags == ["v2.0", "v2.1", "v2.2"] and out.complete
    assert any(h.get("Authorization") == "Bearer tok123" for _m, _u, h in c.session.log)


def test_basic_auth_challenge_is_reported_as_auth_error():
    c = client({"/v2/": [Resp(401, headers={"WWW-Authenticate": 'Basic realm="Oracle"'})]})
    with pytest.raises(RegistryError) as e:
        c.list_tags(parse_image_ref("container-registry.oracle.com/database/free:23.4"))
    assert e.value.kind == "auth"


def test_403_and_404_mapping():
    c = client({"/v2/": [Resp(403)]})
    with pytest.raises(RegistryError) as e:
        c.list_tags(parse_image_ref("quay.io/a/b:1"))
    assert e.value.kind == "auth"
    c = client({"/v2/": [Resp(404)]})
    with pytest.raises(RegistryError) as e:
        c.list_tags(parse_image_ref("quay.io/a/b:1"))
    assert e.value.kind == "not_found"


def test_rate_limit_retries_with_retry_after_then_succeeds():
    c = client({"/v2/": [Resp(429, headers={"Retry-After": "7"}), Resp(200, {"tags": ["1.0"]})]})
    assert c.list_tags(parse_image_ref("registry.k8s.io/pause:1.0")).tags == ["1.0"]
    assert c._sleeps == [7.0]


def test_rate_limit_exhausted():
    c = client({"/v2/": [Resp(429)]}, retries=1)
    with pytest.raises(RegistryError) as e:
        c.list_tags(parse_image_ref("registry.k8s.io/pause:1.0"))
    assert e.value.kind == "rate_limit"


def test_network_errors_use_exponential_backoff_then_fail():
    c = client({"/v2/": [requests.exceptions.ConnectionError("boom")]}, retries=3)
    with pytest.raises(RegistryError) as e:
        c.list_tags(parse_image_ref("quay.io/a/b:1"))
    assert e.value.kind == "network"
    assert c._sleeps == [1, 2, 4]


def test_timeout_then_success():
    c = client({"/v2/": [requests.exceptions.Timeout("t"), Resp(200, {"tags": ["1"]})]})
    assert c.list_tags(parse_image_ref("quay.io/a/b:1")).tags == ["1"]


def test_server_error_retried():
    c = client({"/v2/": [Resp(503), Resp(200, {"tags": ["9"]})]})
    assert c.list_tags(parse_image_ref("quay.io/a/b:1")).tags == ["9"]


def test_invalid_json_is_unsupported():
    c = client({"/v2/": [Resp(200, None)]})
    with pytest.raises(RegistryError) as e:
        c.list_tags(parse_image_ref("quay.io/a/b:1"))
    assert e.value.kind == "unsupported"


def test_tag_exists_and_digest():
    ref = parse_image_ref("ghcr.io/org/api:v2.0")
    c = client({"manifests/v2.1": [Resp(200, None, {"Docker-Content-Digest": "sha256:" + "c" * 64})],
                "manifests/v9": [Resp(404)]})
    assert c.tag_exists(ref, "v2.1") is True
    assert c.tag_exists(ref, "v9") is False
    assert c.get_digest(ref, "v2.1") == "sha256:" + "c" * 64


def test_tag_cache():
    c = client({"/v2/": [Resp(200, {"tags": ["1"]})]})
    ref = parse_image_ref("quay.io/a/b:1")
    c.list_tags(ref)
    c.list_tags(ref)
    assert len(c.session.log) == 1


def test_hub_api_failure_falls_back_to_oci():
    c = client({"hub.docker.com": [Resp(503)], "registry-1.docker.io": [Resp(200, {"tags": ["22.0.0"]})]}, retries=0)
    out = c.list_tags(parse_image_ref("node:22.0.0"))
    assert out.tags == ["22.0.0"] and out.source == "oci"
