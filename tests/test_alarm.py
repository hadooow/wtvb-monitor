from app.protocol import SensorSample, evaluate_alarm


def sample(**values):
    return SensorSample(mac="FE6DF407B3E4", timestamp="2026-09-06T00:00:00Z", **values)


def test_alarm_uses_maximum_absolute_axis_value():
    result = evaluate_alarm(
        sample(velocity_x=-12, velocity_y=3, velocity_z=4),
        {"velocity_warn": 10, "velocity_alarm": 20},
    )
    assert result["level"] == "warning"
    assert result["reasons"][0]["value"] == 12


def test_alarm_level_wins_over_warning():
    result = evaluate_alarm(
        sample(temperature=71, displacement_x=15),
        {"temperature_alarm": 70, "displacement_warn": 10},
    )
    assert result["level"] == "alarm"
    assert len(result["reasons"]) == 2


def test_empty_thresholds_are_normal():
    assert evaluate_alarm(sample(temperature=100), {}) == {"level": "normal", "reasons": []}
