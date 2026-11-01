import pytest
from pydantic import ValidationError

from qa_runtime.browser_models import BrowserCommand, BrowserSettings, http_origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://@web.invalid",
        "https://user:secret@web.invalid",
        "https://web.invalid/path",
        "https://web.invalid?key=value",
        "javascript:alert(1)",
        "http://web.invalid:0",
        "http://web.invalid:65536",
        "https://web.invalid\\other",
        "https://web.invalid\n",
    ],
)
def test_browser_scope_requires_explicit_valid_origins(origin):
    with pytest.raises(ValidationError):
        BrowserSettings(allowedOrigins=[origin])


def test_origin_normalization_is_consistent_for_ports_and_ipv6():
    assert http_origin("https://WEB.invalid:443/path") == "https://web.invalid"
    assert http_origin("http://[::1]:8010/path") == "http://[::1]:8010"
    assert BrowserSettings(allowedOrigins=["https://web.invalid/", "https://web.invalid"]).allowed_origins == [
        "https://web.invalid",
    ]


@pytest.mark.parametrize(
    "command",
    [
        {"action": "fill", "elementRef": "el_1", "snapshotId": "snap_1"},
        {"action": "fill", "elementRef": "el_1", "snapshotId": "snap_1", "value": "text", "credentialRef": "secret"},
        {"action": "click", "elementRef": "el_1"},
        {"action": "observe", "value": "text"},
        {"action": "navigate", "url": "file:///etc/passwd"},
        {"action": "evaluate", "script": "arbitrary code"},
    ],
)
def test_browser_capabilities_reject_ambiguous_or_executable_input(command):
    with pytest.raises(ValidationError):
        BrowserCommand.model_validate(command)
