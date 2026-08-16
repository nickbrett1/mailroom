from mailroom.verticals.game_catalog.classifier import classify_item


def test_retail_suffix_ps5_game():
    c = classify_item("Sonic Superstars - PlayStation 5")
    assert c.classification == "playstation_game"
    assert "playstation 5" in (c.platform or "").lower()


def test_gamefly_ps5_parenthetical():
    c = classify_item("Atomfall (PS5)")
    assert c.classification == "playstation_game"


def test_shopify_variant():
    c = classify_item("Animal Well", variant="PS5")
    assert c.classification == "playstation_game"


def test_switch_excluded():
    c = classify_item("Zelda - Nintendo Switch")
    assert c.classification == "non_playstation"


def test_accessory_reviewed():
    c = classify_item("Cover Plates for PS5 Pro")
    assert c.classification == "accessory_hardware"


def test_ambiguous_review():
    c = classify_item("Some Vague Product")
    assert c.classification == "needs_review"
