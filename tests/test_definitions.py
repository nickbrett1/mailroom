from mailroom.definitions import definitions


def test_definitions_load():
    graph = definitions.resolve_asset_graph()
    keys = {k.to_user_string() for k in graph.get_all_asset_keys()}
    assert {
        "raw_psn_receipts",
        "parsed_purchases_digital",
        "classified_game_items",
        "owned_games",
        "catalog_views",
    } <= keys
    resources = definitions.get_repository_def().get_top_level_resources()
    assert "msgvault" in resources
    assert "igdb" in resources
