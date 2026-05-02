from skills.signal.feature_extractor import extract_features


def test_extracts_stated_size_pct_and_entry_verb():
    f = extract_features("Added a 2% position in CEG calls. Looking for a move.")
    assert f.stated_size_pct == 2.0
    assert f.entry_verb_present is True
    assert "CEG" in f.tickers_in_msg


def test_extracts_dollar_prefixed_tickers():
    f = extract_features("OPEN $SHEN @Wall - Alerts taking a stab")
    assert "SHEN" in f.tickers_in_msg
    assert f.entry_verb_present is True


def test_no_entry_verb_in_commentary():
    f = extract_features("watching TEST closely, no position yet")
    assert f.entry_verb_present is False
    assert f.stated_size_pct is None


def test_detects_availability_phrase():
    f = extract_features("will be off the grid for passover", availability_phrases=["off the grid", "passover"])
    assert f.availability_phrase == "off the grid"


def test_msg_length_and_thread_reply():
    msg = "looks ready"
    f = extract_features(msg, is_thread_reply=True)
    assert f.msg_length == len(msg)
    assert f.is_thread_reply is True


def test_size_capped_phrase_match_case_insensitive():
    f = extract_features("ADDED 5% pos AAPL")
    assert f.stated_size_pct == 5.0
    assert f.entry_verb_present is True
