from app.services.sentences import split_stream


def _stream_whole(text: str) -> list[str]:
    return list(split_stream([text]))


def _stream_char_by_char(text: str) -> list[str]:
    return list(split_stream(list(text)))


def test_abbreviation_merges_forward():
    text = "Dr. Smith found the bones. It was old."
    result = _stream_whole(text)
    assert result == ["Dr. Smith found the bones.", "It was old."]


def test_decimal_is_not_a_boundary():
    text = "The dinosaur weighed 3.14 tonnes in total. That is a lot."
    result = _stream_whole(text)
    assert result == [
        "The dinosaur weighed 3.14 tonnes in total.",
        "That is a lot.",
    ]


def test_force_split_on_long_run_on():
    text = ("word " * 90).strip()  # 449 chars, no terminator anywhere
    result = _stream_whole(text)
    assert len(result) > 1
    assert all(len(s) <= 300 for s in result)
    # allow whitespace normalization at the forced cut point; word order must survive
    assert " ".join(result).split() == text.split()


def test_stream_ending_without_terminator():
    text = "The lecture is now over"
    result = _stream_whole(text)
    assert result == ["The lecture is now over"]


def test_flush_remainder_at_stream_end_even_short():
    text = "Yes."
    result = _stream_whole(text)
    assert result == ["Yes."]


def test_chunking_does_not_change_output():
    text = "Dr. Smith found the bones. It was very old, from the Jurassic period."
    assert _stream_char_by_char(text) == _stream_whole(text)


def test_multiple_sentences_in_one_chunk():
    # "Sauropods were huge." is exactly 20 chars, so it's emitted alone; the
    # next sentence alone is only 16 chars ("They ate plants.") and merges
    # forward into the final flush since nothing after it clears 20 chars
    # with a mid-buffer boundary.
    text = "Sauropods were huge. They ate plants. Their necks were very long indeed."
    result = _stream_whole(text)
    assert result == [
        "Sauropods were huge.",
        "They ate plants. Their necks were very long indeed.",
    ]
