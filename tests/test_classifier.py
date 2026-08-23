from mailroom.verticals.game_catalog.classifier import classify_item


def test_retail_suffix_ps5_game():
    c = classify_item("Sonic Superstars - PlayStation 5")
    assert c.classification == "playstation_game"
    assert "playstation 5" in (c.platform or "").lower()


def test_vita_identified_as_platform():
    """'PS Vita' in the title (no '- PS Vita' separator) is a Vita platform."""
    c = classify_item("Rayman® Origins PS Vita (Full Game 932 MB)")
    assert c.classification == "playstation_game"
    assert c.platform == "ps vita"
    # '- PS Vita' and 'psvita' normalize the same way
    assert classify_item("Title - PS Vita").platform == "ps vita"
    assert classify_item("Title for PSVita").platform == "ps vita"
    # a plain 'vita' in a name is not a platform
    assert classify_item("Vita Nova").classification != "playstation_game"


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


def test_cable_with_ps4_compatibility_is_accessory():
    # "for Routers, PS4" = compatibility mention, not a PlayStation game
    c = classify_item("Ethernet Cable 75 ft, Supports Cat 8/Cat 7, Shielded RJ45 Network Cable for Routers, PS4")
    assert c.classification == "accessory_hardware"


def test_hdmi_cable_with_ps5_compatibility_is_accessory():
    c = classify_item("Highwings 8K 10K 4K HDMI Cable, HDCP 2.2, HDR10 Compatible with Roku TV/PS5/HDTV")
    assert c.classification == "accessory_hardware"


def test_mps4_sku_is_not_a_playstation_game():
    """'Master Plunger MPS4 Sink' — the 'ps4' substring inside 'MPS4' must
    not classify a plunger as a PlayStation game (data-quality bug: a
    cancelled Amazon order entered the catalog as a 'game')."""
    c = classify_item('Master Plunger MPS4 Sink ..." has been canceled')
    assert c.classification == "needs_review"


def test_ps4_keyword_still_word_boundary():
    # Real PlayStation titles keep matching.
    c = classify_item("Sonic Superstars PS4")
    assert c.classification == "playstation_game"
