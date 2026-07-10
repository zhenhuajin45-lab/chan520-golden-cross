from chan520_skill.gm_data import gm_symbol


def test_gm_symbol_maps_mainland_indexes_and_stocks() -> None:
    assert gm_symbol("000300") == "SHSE.000300"
    assert gm_symbol("000001") == "SHSE.000001"
    assert gm_symbol("399001") == "SZSE.399001"
    assert gm_symbol("600288") == "SHSE.600288"
    assert gm_symbol("300750") == "SZSE.300750"
