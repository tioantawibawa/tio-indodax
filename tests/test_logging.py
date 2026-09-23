from agent.logging_setup import REDACTED, SecretRedactor


def test_sensitive_keys_masked():
    r = SecretRedactor()
    out = r(None, "info", {"event": "x", "api_key": "abc", "Sign": "deadbeef", "headers": {"Key": "k", "ok": 1}})
    assert out["api_key"] == REDACTED and out["Sign"] == REDACTED
    assert out["headers"]["Key"] == REDACTED and out["headers"]["ok"] == 1


def test_known_secret_values_scrubbed_everywhere():
    secret = "AEDHIGAT-QATEGWOX-OPCSCPQX"
    r = SecretRedactor([secret, "abc"])  # too-short values ignored
    out = r(None, "error", {"event": f"request failed with key {secret}", "ctx": [f"x{secret}y", "abc"]})
    assert secret not in str(out)
    assert out["ctx"][1] == "abc"
