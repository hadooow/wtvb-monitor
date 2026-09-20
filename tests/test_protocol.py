from app.protocol import WtvbStreamDecoder, parse_wtvb01_frame


EXAMPLE = bytes.fromhex("5561110016000200020000000100E60A430047000A0025002500250000000000")


def test_parse_documented_wtvb01_frame():
    sample = parse_wtvb01_frame("FE:6D:F4:07:B3:E4", EXAMPLE)
    assert sample is not None
    assert (sample.velocity_x, sample.velocity_y, sample.velocity_z) == (17.0, 22.0, 2.0)
    assert sample.temperature == 27.9
    assert (sample.displacement_x, sample.displacement_y, sample.displacement_z) == (67.0, 71.0, 10.0)
    assert (sample.frequency_x, sample.frequency_y, sample.frequency_z) == (37.0, 37.0, 37.0)
    assert sample.vibration_angle_x == 0.011


def test_stream_decoder_reassembles_split_notification():
    decoder = WtvbStreamDecoder()
    assert decoder.feed("FE6DF407B3E4", EXAMPLE[:9]) == []
    samples = decoder.feed("FE6DF407B3E4", EXAMPLE[9:])
    assert len(samples) == 1
    assert samples[0].temperature == 27.9


def test_repeated_truncated_frames_never_create_fabricated_samples():
    decoder = WtvbStreamDecoder()
    for _ in range(8):
        assert decoder.feed("02A000000001", EXAMPLE[:20]) == []
    samples = decoder.feed("02A000000001", EXAMPLE)
    assert len(samples) == 1
    assert samples[0].displacement_z == 10.0


def test_twenty_byte_fragment_with_real_continuation_still_decodes():
    decoder = WtvbStreamDecoder()
    assert decoder.feed("02A000000001", EXAMPLE[:20]) == []
    samples = decoder.feed("02A000000001", EXAMPLE[20:])
    assert len(samples) == 1 and samples[0].temperature == 27.9
