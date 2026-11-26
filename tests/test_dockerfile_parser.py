import hashlib

import pytest

from src.dockerfile_parser import (InvalidImageReference, parse_dockerfile, parse_image_ref, rewrite_from, substitute)

DIGEST = "sha256:" + "a" * 64


def froms(text):
    return parse_dockerfile(text).froms


def test_single_stage():
    f = froms("FROM node:22.22.0-slim\nRUN echo hi\n")
    assert len(f) == 1 and f[0].resolved_image == "node:22.22.0-slim" and f[0].alias is None


def test_multi_stage_with_alias_platform_and_stage_ref():
    t = ("FROM --platform=linux/amd64 node:22.22.0-slim AS builder\nRUN npm ci\n"
         "FROM nginx:1.27-alpine AS production\nCOPY --from=builder /app /usr/share/nginx/html\n"
         "FROM production\n")
    f = froms(t)
    assert [x.index for x in f] == [1, 2, 3]
    assert f[0].platform == "linux/amd64" and f[0].alias == "builder"
    assert f[1].alias == "production"
    assert f[2].is_stage_ref


def test_lowercase_keywords_comments_and_continuations():
    t = "# a comment FROM fake:1\nfrom   node:20 \\\n   as  base\n# FROM nope:1\nRUN echo FROM alpine:3\n"
    f = froms(t)
    assert len(f) == 1 and f[0].alias == "base" and f[0].start_line == 1 and f[0].end_line == 2


def test_comment_inside_continuation():
    t = "FROM \\\n# c\n  node:20-slim\nRUN x\n"
    f = froms(t)
    assert f[0].resolved_image == "node:20-slim"


def test_heredoc_content_is_not_parsed():
    t = "FROM alpine:3.20\nRUN <<EOF\nFROM evil:1\nEOF\nFROM busybox:1.36\n"
    assert [x.resolved_image for x in froms(t)] == ["alpine:3.20", "busybox:1.36"]


def test_arg_resolution_defaults_and_unresolved():
    t = "ARG V=22.22.0\nARG EMPTY\nARG IMG=node\nFROM ${IMG}:${V}-slim AS a\nFROM node:${V:-20}\nFROM ${EMPTY}\nFROM base:$UNKNOWN\nFROM --platform=$BUILDPLATFORM node:20\nFROM x:${TARGETARCH}\n"
    f = froms(t)
    assert f[0].resolved_image == "node:22.22.0-slim"
    assert f[1].resolved_image == "node:22.22.0"
    assert f[2].resolved_image is None and "no default" in f[2].unresolved_reason
    assert f[3].resolved_image is None
    assert f[4].resolved_image == "node:20"
    assert f[5].resolved_image is None and "build-time" in f[5].unresolved_reason


def test_substitute_operators():
    assert substitute("${A:-x}", {}).value == "x"
    assert substitute("${A:-x}", {"A": "y"}).value == "y"
    assert substitute("${A:+z}", {"A": "y"}).value == "z"
    assert substitute("${A:+z}", {}).value == ""


def test_digest_and_registry_variants():
    r = parse_image_ref(f"ghcr.io/org/api:v2.0@{DIGEST}")
    assert (r.registry, r.repository, r.tag, r.digest) == ("ghcr.io", "org/api", "v2.0", DIGEST)
    r = parse_image_ref("localhost:5000/team/app:1.0")
    assert r.registry == "localhost:5000" and r.repository == "team/app"
    r = parse_image_ref("registry.example.com:8443/a/b/c:1")
    assert r.registry == "registry.example.com:8443" and r.repository == "a/b/c"
    r = parse_image_ref("nginx")
    assert r.registry == "docker.io" and r.repository == "library/nginx" and r.tag is None
    r = parse_image_ref("bitnami/redis:7.2")
    assert r.repository == "bitnami/redis" and r.registry == "docker.io"
    r = parse_image_ref("docker.io/library/node:22")
    assert r.registry == "docker.io" and r.render(tag="22.1") == "docker.io/library/node:22.1"
    assert parse_image_ref("node:22-slim").render(tag="22.2-slim") == "node:22.2-slim"
    assert parse_image_ref("public.ecr.aws/docker/library/node:22").registry == "public.ecr.aws"
    assert parse_image_ref("quay.io/prometheus/prometheus:v2.1").registry == "quay.io"


@pytest.mark.parametrize("bad", ["", "Node:22", "node:", "node::1", "a b", "node:22@sha256:xyz", "-x/y:1", "node:${V}"])
def test_invalid_images(bad):
    with pytest.raises(InvalidImageReference):
        parse_image_ref(bad)


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def test_rewrite_is_surgical_and_preserves_everything():
    t = ("# syntax=docker/dockerfile:1\n# builds node:22.22.0-slim\nFROM --platform=linux/amd64 node:22.22.0-slim AS builder  \n"
         "RUN echo node:22.22.0-slim && npm ci\nLABEL base=\"node:22.22.0-slim\"\nFROM node:22.22.0-slim AS second\n")
    p = parse_dockerfile(t)
    new = "".join(rewrite_from(p, p.froms[0], "node:22.23.0-slim"))
    expect = t.replace("FROM --platform=linux/amd64 node:22.22.0-slim", "FROM --platform=linux/amd64 node:22.23.0-slim", 1)
    assert new == expect                              # only that token changed; trailing spaces kept
    assert new.count("node:22.22.0-slim") == 4        # comment, RUN, LABEL and the second FROM untouched


def test_rewrite_preserves_crlf_and_multiline_from():
    t = "FROM \\\r\n  nginx:1.27-alpine \\\r\n  AS prod\r\nRUN x\r\n"
    p = parse_dockerfile(t)
    new = "".join(rewrite_from(p, p.froms[0], "nginx:1.29.0-alpine"))
    assert new == t.replace("1.27", "1.29.0")
    assert "\n" not in new.replace("\r\n", "")


def test_arg_default_edit_only_when_safe():
    t = "ARG NODE=22.22.0\nFROM node:${NODE}-slim\nRUN echo ok\n"
    p = parse_dockerfile(t)
    f = p.froms[0]
    assert f.arg_name == "NODE" and not f.arg_edit_blocked
    new = "".join(rewrite_from(p, f, "node:22.23.0-slim"))
    assert new == "ARG NODE=22.23.0\nFROM node:${NODE}-slim\nRUN echo ok\n"
    # variable reused elsewhere -> blocked
    p2 = parse_dockerfile("ARG NODE=22.22.0\nFROM node:${NODE}-slim\nRUN echo $NODE\n")
    assert p2.froms[0].arg_edit_blocked
    with pytest.raises(ValueError):
        rewrite_from(p2, p2.froms[0], "node:22.23.0-slim")
    # two vars composed -> blocked
    p3 = parse_dockerfile("ARG A=node\nARG B=22\nFROM ${A}:${B}\n")
    assert p3.froms[0].arg_edit_blocked


def test_quoted_arg_default_kept():
    p = parse_dockerfile('ARG TAG="22.22.0-slim"\nFROM node:${TAG}\n')
    new = "".join(rewrite_from(p, p.froms[0], "node:22.23.0-slim"))
    assert new.startswith('ARG TAG="22.23.0-slim"')


def test_scratch_and_empty():
    f = froms("FROM scratch\nFROM busybox AS x\n")
    assert f[0].is_scratch and not f[1].is_scratch
    assert froms("# nothing\n") == []
