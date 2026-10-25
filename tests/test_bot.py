from bot import should_ignore

G, C = 1, 2


def test_ignore_rule():
    assert not should_ignore(False, G, C, G, C)
    assert should_ignore(True, G, C, G, C)  # bots, incl. ourselves
    assert should_ignore(False, None, C, G, C)  # DM
    assert should_ignore(False, G, 99, G, C)  # other channel
    assert should_ignore(False, 99, C, G, C)  # other guild
