"""Behavioral tests for proxy_utils — proxy URL parsing, arg merging, redaction.

Pure functions, no browser. These guard the launch path: a malformed proxy must
raise (not silently launch Chrome unproxied), IPv6 hosts must be bracketed, and
credentials must never leak into logged/echoed launch args.
"""

import pytest
from proxy_utils import (
    ProxyConfig,
    ProxyConfigError,
    merge_proxy_server_arg,
    parse_proxy_config,
    redact_launch_arg,
)


class TestParseProxyConfig:
    def test_full_url_with_credentials(self):
        cfg = parse_proxy_config("http://user:pass@host.example:8080")
        assert cfg == ProxyConfig(
            server="http://host.example:8080", username="user", password="pass"
        )

    def test_bare_host_port_defaults_to_http(self):
        cfg = parse_proxy_config("10.0.0.5:3128")
        assert cfg.server == "http://10.0.0.5:3128"
        assert cfg.username is None and cfg.password is None

    def test_scheme_preserved(self):
        assert (
            parse_proxy_config("socks5://1.2.3.4:1080").server
            == "socks5://1.2.3.4:1080"
        )

    def test_ipv6_host_is_bracketed(self):
        cfg = parse_proxy_config("http://[::1]:8080")
        assert cfg.server == "http://[::1]:8080"

    def test_empty_raises(self):
        with pytest.raises(ProxyConfigError):
            parse_proxy_config("   ")

    def test_missing_port_raises(self):
        with pytest.raises(ProxyConfigError):
            parse_proxy_config("http://host.example")

    def test_username_without_password_raises(self):
        with pytest.raises(ProxyConfigError):
            parse_proxy_config("http://user@host.example:8080")

    def test_password_without_username_raises(self):
        # urlsplit gives username="" here (not None); the guard must still fire.
        with pytest.raises(ProxyConfigError):
            parse_proxy_config("http://:pass@host.example:8080")

    def test_username_with_empty_password_raises(self):
        # Symmetric to the above: "user:@host" -> password="" must be rejected,
        # not silently accepted as a real credential.
        with pytest.raises(ProxyConfigError):
            parse_proxy_config("http://user:@host.example:8080")


class TestMergeProxyServerArg:
    def test_none_leaves_args_untouched(self):
        args = ["--headless", "--no-sandbox"]
        assert merge_proxy_server_arg(args, None) == args

    def test_appends_single_proxy_arg(self):
        out = merge_proxy_server_arg(["--headless"], "http://host:8080")
        assert out == ["--headless", "--proxy-server=http://host:8080"]

    def test_replaces_existing_proxy_arg(self):
        out = merge_proxy_server_arg(
            ["--proxy-server=http://old:1", "--headless"], "http://new:2"
        )
        assert out.count("--proxy-server=http://new:2") == 1
        assert "--proxy-server=http://old:1" not in out
        assert "--headless" in out


class TestRedactLaunchArg:
    def test_redacts_credentials_in_proxy_arg(self):
        out = redact_launch_arg("--proxy-server=http://user:secret@host:8080")
        assert "user" not in out and "secret" not in out
        assert out == "--proxy-server=http://host:8080"

    def test_redacts_credentials_in_bare_url(self):
        out = redact_launch_arg("http://user:secret@host:8080/path")
        assert "secret" not in out and "user" not in out
        assert "host:8080" in out

    def test_proxy_arg_without_credentials_unchanged(self):
        arg = "--proxy-server=http://host:8080"
        assert redact_launch_arg(arg) == arg

    def test_plain_flag_unchanged(self):
        assert redact_launch_arg("--headless") == "--headless"

    def test_non_string_coerced(self):
        assert redact_launch_arg(12345) == "12345"
