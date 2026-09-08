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
