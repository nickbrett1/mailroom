from mailroom.definitions import definitions


def test_definitions_load():
    graph = definitions.get_asset_graph()
    keys = {k.to_user_string() for k in graph.all_asset_keys}
    assert {
        "raw_psn_receipts",
        "parsed_purchases_digital",
        "classified_game_items",
        "owned_games",
        "catalog_views",
    } <= keys
    assert "msgvault" in definitions.get_resources()
    assert "igdb" in definitions.get_resources()
