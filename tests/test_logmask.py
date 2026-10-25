from app.logmask import mask


def test_masks():
    assert "cw-" + "a" * 40 not in mask("key cw-" + "a" * 40)
    assert mask("Authorization: Bearer abc.def") == "Authorization: Bearer ***"
    assert "secret" not in mask('GET /search?api_key=secret&x=1')


def test_access_record_args_shape_preserved():
    import logging
    from app.logmask import MaskSecretsFilter
    rec = logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/search?api_key=topsecret", "1.1", 200), None)
    MaskSecretsFilter().filter(rec)
    assert len(rec.args) == 5 and "topsecret" not in rec.getMessage()
