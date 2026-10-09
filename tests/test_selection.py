from auto_uploader.selection import parse_selection


def test_parse_selection():
    assert parse_selection("all", 5) == [1, 2, 3, 4, 5]
    assert parse_selection("", 5) == [1, 2, 3, 4, 5]
    assert parse_selection("1,3,5", 5) == [1, 3, 5]
    assert parse_selection("1-3, 5", 5) == [1, 2, 3, 5]
    assert parse_selection("4-2", 5) == [2, 3, 4]
    assert parse_selection("10", 5) == []
    assert parse_selection("q", 5) == []
