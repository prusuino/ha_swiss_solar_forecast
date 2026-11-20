"""The repair notices: a failure counts up to an issue, a success withdraws
it, and unloading an entry takes its issues along."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.swiss_solar_forecast.const import DOMAIN
from custom_components.swiss_solar_forecast.issues import IssueReporter, async_clear_entry_issues


async def test_a_failure_becomes_an_issue_after_several_in_a_row(hass: HomeAssistant) -> None:
    registry = ir.async_get(hass)
    issues = IssueReporter(hass, "entry1", after=3)
    for n in range(3):
        issues.failure("forecast_unreachable", {"error": "HTTP 503"})
        assert (registry.async_get_issue(DOMAIN, "forecast_unreachable_entry1") is not None) is (n == 2)
    assert registry.async_get_issue(DOMAIN, "forecast_unreachable_entry1").translation_placeholders == {"error": "HTTP 503"}

    issues.clear("forecast_unreachable")
    assert registry.async_get_issue(DOMAIN, "forecast_unreachable_entry1") is None
    issues.clear("forecast_unreachable")  # nothing open: nothing to do
    issues.failure("forecast_unreachable")  # the count starts again
    assert registry.async_get_issue(DOMAIN, "forecast_unreachable_entry1") is None


async def test_issues_of_an_entry_go_with_it(hass: HomeAssistant) -> None:
    registry = ir.async_get(hass)
    IssueReporter(hass, "entry1").failure("forecast_unreachable", {"error": 503}, after=1, suffix="plane2")
    IssueReporter(hass, "entry2").raise_now("forecast_unreachable")
    assert registry.async_get_issue(DOMAIN, "forecast_unreachable_plane2_entry1").translation_placeholders == {"error": "503"}

    async_clear_entry_issues(hass, "entry1")
    assert registry.async_get_issue(DOMAIN, "forecast_unreachable_plane2_entry1") is None
    assert registry.async_get_issue(DOMAIN, "forecast_unreachable_entry2") is not None
