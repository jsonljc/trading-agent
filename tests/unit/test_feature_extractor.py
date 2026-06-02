from skills.signal.feature_extractor import extract_features


def test_extracts_stated_size_pct_and_entry_verb():
    f = extract_features("Added a 2% position in CEG calls. Looking for a move.")
    assert f.stated_size_pct == 2.0
    assert f.entry_verb_present is True
    assert f.tickers_in_msg == ("CEG",)
    assert isinstance(f.tickers_in_msg, tuple)


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


def test_single_letter_uppercase_words_are_not_tickers():
    f = extract_features("I think A is interesting today")
    assert f.tickers_in_msg == ()


def test_no_partial_word_ticker_extraction():
    f = extract_features("ADDED 2% POSITION IN AAPL")
    # SITION (last 6 chars of POSITION) must NOT be extracted as a ticker.
    assert "SITION" not in f.tickers_in_msg


def test_bare_percentage_in_prose_does_not_match_size():
    f = extract_features("AAPL — earnings beat with 8% revenue growth, opened a position")
    assert f.stated_size_pct is None


def test_short_interest_percentage_does_not_match_size():
    f = extract_features("Mcap: $4B Short Interest: 23%. opened a small position")
    assert f.stated_size_pct is None


def test_size_with_pos_suffix_matches():
    f = extract_features("Added 2% pos AAPL")
    assert f.stated_size_pct == 2.0


def test_size_with_position_suffix_matches():
    f = extract_features("Added a 2% position in CEG calls")
    assert f.stated_size_pct == 2.0


def test_size_with_weighting_suffix_matches():
    f = extract_features("a small 1% weighting @ $41.22")
    assert f.stated_size_pct == 1.0


def test_exit_verb_detection():
    for msg in ["Sold half my AAPL", "out of NVDA here", "closed TSLA",
                "trimming MSFT into strength", "taking profits on AMD",
                "scaling out of META", "stopped out of GOOG"]:
        assert extract_features(msg).exit_verb_present is True, msg


def test_exit_verb_absent_on_entries_and_commentary():
    assert extract_features("Added a 2% position in CEG").exit_verb_present is False
    assert extract_features("watching TEST closely").exit_verb_present is False


def test_entry_and_exit_flags_are_independent():
    # A rotation message can carry both an exit and an entry verb.
    f = extract_features("out of AAPL, opened a position in TSLA")
    assert f.exit_verb_present is True
    assert f.entry_verb_present is True
