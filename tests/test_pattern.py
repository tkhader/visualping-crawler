from crawler.main import PATTERN


def test_visualping_pattern():
    assert PATTERN.findall("x VISUALPING{abc123} y") == ["VISUALPING{abc123}"]


def test_rejects_nested_braces():
    assert PATTERN.findall("VISUALPING{a{b}}") == []
