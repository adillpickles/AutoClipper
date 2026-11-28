"""Fixtures shared by the API test modules."""

from dataclasses import replace

import pytest

from autoclipper.api import mock


@pytest.fixture
def short_preview_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the first candidate inside the small local media fixtures."""
    scenario_for = mock.scenario_for

    def with_short_candidate(video_id: str) -> mock.Scenario:
        scenario = scenario_for(video_id)
        assert scenario.result is not None
        first, *others = scenario.result.candidates
        first = first.model_copy(update={"start_s": 0.5, "end_s": 1.5})
        result = scenario.result.model_copy(update={"candidates": (first, *others)})
        return replace(scenario, result=result)

    monkeypatch.setattr(mock, "scenario_for", with_short_candidate)
