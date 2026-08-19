"""Tests for get_default_web_url WEB_HOST scheme handling."""

from openhands.app_server.config import get_default_web_url


class TestGetDefaultWebUrl:
    def test_unset(self, monkeypatch):
        monkeypatch.delenv('WEB_HOST', raising=False)
        assert get_default_web_url() is None

    def test_empty(self, monkeypatch):
        monkeypatch.setenv('WEB_HOST', '')
        assert get_default_web_url() is None

    def test_whitespace(self, monkeypatch):
        monkeypatch.setenv('WEB_HOST', '   ')
        assert get_default_web_url() is None

    def test_bare_host_keeps_https_default(self, monkeypatch):
        monkeypatch.setenv('WEB_HOST', 'app.all-hands.dev')
        assert get_default_web_url() == 'https://app.all-hands.dev'

    def test_bare_host_with_port(self, monkeypatch):
        monkeypatch.setenv('WEB_HOST', 'host.docker.internal:3000')
        assert get_default_web_url() == 'https://host.docker.internal:3000'

    def test_explicit_http_scheme_is_preserved(self, monkeypatch):
        monkeypatch.setenv('WEB_HOST', 'http://host.docker.internal:3000')
        assert get_default_web_url() == 'http://host.docker.internal:3000'

    def test_explicit_https_scheme_is_preserved(self, monkeypatch):
        monkeypatch.setenv('WEB_HOST', 'https://app.all-hands.dev')
        assert get_default_web_url() == 'https://app.all-hands.dev'

    def test_trailing_slash_stripped_when_scheme_present(self, monkeypatch):
        monkeypatch.setenv('WEB_HOST', 'http://localhost:3000/')
        assert get_default_web_url() == 'http://localhost:3000'
