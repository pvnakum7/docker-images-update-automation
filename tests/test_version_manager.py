import pytest

from src import version_manager as vm
from tests.helpers import GOLANG_TAGS, NGINX_TAGS, NODE_TAGS, PYTHON_TAGS


def test_parse_tag_parts():
    p = vm.parse_tag("22.22.0-slim")
    assert p.nums == (22, 22, 0) and p.suffix == "-slim" and p.prefix == ""
    assert vm.parse_tag("v2.0").prefix == "v"
    assert vm.parse_tag("3.13.0rc1").suffix == "rc1"
    for bad in ["latest", "stable-alpine", "lts-slim", "bookworm", ""]:
        assert vm.parse_tag(bad) is None


@pytest.mark.parametrize("tag,stable", [
    ("1.2.3", True), ("1.2.3-slim", True), ("1.2.3-alpine3.20", True), ("1.2.3-rc1", False),
    ("1.2.3-rc.1-slim", False), ("3.14.0rc1-slim", False), ("1.2.3-beta", False), ("1.2.3-alpha.2", False),
    ("1.2.3-nightly", False), ("1.2.3-dev", False), ("1.2.3-preview.1", False), ("1.2.3-SNAPSHOT", False),
    ("1.2.3-canary", False), ("10.0.100-preview.1", False), ("1.2.3-bookworm-slim", True)])
def test_stable_detection(tag, stable):
    assert vm.is_stable_tag(tag) is stable


def test_same_major_picks_newest_fully_pinned_and_keeps_variant():
    s = vm.select_same_major("22.22.0-slim", NODE_TAGS)
    assert s.tag == "22.24.0-slim"          # not 22-slim, not -alpine, not 23+


def test_same_major_never_crosses_major_or_variant():
    s = vm.select_same_major("22.22.0-slim", ["23.0.0-slim", "22.22.0-alpine", "22.30.0-alpine"])
    assert s.tag is None


def test_same_major_prefers_full_over_floating_and_match_current_keeps_precision():
    tags = ["1.28", "1.28.1", "1.29", "1.29.2", "1.29.0"]
    assert vm.select_same_major("1.27", tags).tag == "1.29.2"
    assert vm.select_same_major("1.27", tags, precision="match_current").tag == "1.29"


def test_same_major_floating_current_gets_pinned():
    assert vm.select_same_major("22-slim", NODE_TAGS).tag == "22.24.0-slim"


def test_prevents_downgrade():
    s = vm.select_same_major("22.30.0-slim", ["22.22.0-slim", "22.23.0-slim"])
    assert s.tag is None and "downgrade" in s.reason


def test_python_minor_is_a_new_major_line():
    assert vm.select_same_major("3.12.5-slim", PYTHON_TAGS, depth=2).tag == "3.12.9-slim"
    nm = vm.select_new_major("3.12.5-slim", PYTHON_TAGS, depth=2)
    assert nm.tag == "3.14.0-slim"           # rc excluded would not matter; 3.14.0 is stable
    assert vm.line_depth("library/python", {"release_line_depth": {"default": 1, "python": 2}}) == 2


def test_golang_variant_preserved_and_lines():
    assert vm.select_same_major("1.22.1", GOLANG_TAGS, depth=2).tag == "1.22.9"
    assert vm.select_new_major("1.22.1", GOLANG_TAGS, depth=2).tag == "1.24.0"   # 1.24.1-bookworm is a different variant


def test_new_major_latest_stable_variant_and_prerelease_excluded():
    s = vm.select_new_major("22.22.0-slim", NODE_TAGS, parity="even")
    assert s.tag == "26.13.0-slim"           # 27 is odd (non-LTS), rc/alpha of 26.14 excluded
    s2 = vm.select_new_major("22.22.0-slim", NODE_TAGS)
    assert s2.tag == "27.0.0-slim"


def test_new_major_none_when_already_newest():
    assert "no newer stable major" in vm.select_new_major("26.13.0-slim", NODE_TAGS, parity="even").reason


def test_new_major_alpine_variant_only():
    assert vm.select_new_major("22.23.1-alpine", NODE_TAGS, parity="even").tag == "26.13.0-alpine"


def test_nginx_variant_with_distro_version_is_exact():
    assert vm.select_same_major("1.27-alpine", NGINX_TAGS).tag == "1.29.0-alpine"
    assert vm.select_same_major("1.27-alpine", NGINX_TAGS).tag != "1.29.0-alpine3.20"


def test_prefix_must_match_and_date_tags_ignored():
    assert vm.select_same_major("v2.0", ["v2.1", "2.5", "v2.2", "v3.0"]).tag == "v2.2"
    assert vm.select_same_major("1.0.0", ["20240101", "1.0.1"]).tag == "1.0.1"


def test_unversioned_and_prerelease_current():
    assert vm.select_same_major("latest", ["1.0"]).tag is None
    assert vm.select_same_major("1.0.0-rc1", ["1.0.1"]).tag is None


def test_helpers():
    assert vm.is_downgrade("1.2.3", "1.2.0")
    assert not vm.is_downgrade("1.2.3", "1.3")
    assert vm.same_major_line("3.12.1", "3.12.9", 2) and not vm.same_major_line("3.12.1", "3.13.0", 2)


def test_build_number_suffix_is_not_part_of_the_variant():
    tags = ["17.0.9_9-jre", "17.0.13_11-jre", "17.0.13_11-jdk", "17.0.13-jre", "21.0.5_11-jre", "17.0.13_11-jre-alpine"]
    assert vm.select_same_major("17.0.9_9-jre", tags).tag == "17.0.13_11-jre"          # keeps -jre and the _build style
    assert vm.select_new_major("17.0.9_9-jre", tags).tag == "21.0.5_11-jre"
    assert vm.parse_tag("17.0.9_9-jre").variant == "-jre" and vm.parse_tag("17.0.9_9-jre").build == 9
