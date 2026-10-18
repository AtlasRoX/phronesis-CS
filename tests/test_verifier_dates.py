"""Calendar dates are not study figures (a run removed a correct approval-date claim as a 'pancreatic' figure)."""

from phronesis.phronesis.agents.verifier import bare_numbers


def test_calendar_dates_are_not_figures():
    assert bare_numbers("olaparib (approved 19 Dec 2014) and niraparib (approved 27 Mar 2017)") == []
    assert bare_numbers("approved March 27, 2017; label effective 2025-07-10") == []


def test_real_figures_still_extracted():
    assert bare_numbers("HR 0.30 (95% CI 0.23-0.41), median 13.8 months") == [0.3, 0.23, 0.41, 13.8]
    assert bare_numbers("19 patients enrolled in May 2014") == [19.0]
