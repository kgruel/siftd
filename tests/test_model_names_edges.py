from siftd.model_names import parse_model_name


def test_parse_model_name_gemini_unknown_variant_falls_back():
    out = parse_model_name("gemini-2.5-nano")
    assert out["name"] == "gemini-2.5-nano"
    assert all(out[k] is None for k in ("creator", "family", "version", "variant", "released"))
