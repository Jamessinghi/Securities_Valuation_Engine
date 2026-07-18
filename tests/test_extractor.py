from app.ocr.extractor import _pick_value


def test_pick_skips_note_ref():
    assert _pick_value(" 2.5 25.2 37.8") == 25.2

def test_pick_skips_footnote_markers():
    assert _pick_value("1, 2 3,391 3,706 (9)") == 3391.0

def test_pick_parses_parenthesised_negative():
    assert _pick_value(" (137) (123) 11") == -137.0

def test_pick_ignores_dash_nil():
    assert _pick_value(" – 687") == 687.0
