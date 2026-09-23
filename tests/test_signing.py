"""Signature vectors taken verbatim from the official docs."""

from agent.exchange.signing import Clock, NonceGenerator, encode_params, sign_sha256, sign_sha512

LEGACY_SECRET = "f60617a68fcce028f0a90bc9eb765d17379eb548cc935c01a7ee3186eecf870e9b68f27a31bcfe8d"
DMS_SECRET = "da78a39e9dda31c399bcc293d997a347dc7fd0408cb5151931243a302b273ec3238510ea61e11f7c"


def test_legacy_tapi_getinfo_vector():
    # Private-RestAPI.md "SIGNED Endpoint Examples for POST getInfo"
    body = encode_params({"method": "getInfo", "timestamp": 1578304294000, "recvWindow": 1578303937000})
    assert body == "method=getInfo&timestamp=1578304294000&recvWindow=1578303937000"
    assert sign_sha512(LEGACY_SECRET, body) == (
        "bab004e5a518740d7a33b38b44dbebecd3fb39f40b42391af39fcce06edabff5"
        "233b3e8064a07c528d1c751a6923d5116026c7786e01b22e2d35277a098cae99"
    )


def test_deadman_body_vector_uses_secret_key():
    # Deadman-switch.md "Using Request Body" (raw comma, text/plain body)
    body = "pair=btc_idr,eth_idr&countdownTime=10000&timestamp=1578304294001&recvWindow=1578303937000"
    assert sign_sha512(DMS_SECRET, body) == (
        "b4f03574d264ffbaa37eadd8460f50dbb9ae6f12d4852a46d8654d472838aaa1"
        "de99248e958c904333e61738a00462d49f32bcd3258d8a3defca8c73b8d60d09"
    )


def test_deadman_querystring_vector_url_encoded():
    # Deadman-switch.md "Using Query String + URL Encoded"
    qs = encode_params({"pair": "btc_idr,eth_idr", "countdownTime": 10000,
                        "timestamp": 1578304294001, "recvWindow": 1578303937000})
    assert qs.startswith("pair=btc_idr%2Ceth_idr&")
    assert sign_sha512(DMS_SECRET, qs) == (
        "29ff89378b9f33954b0f5319488190078f091c7723d886c5c2a4a0b06ef793d7"
        "d3b99155d63410203a21355e5e2757cb4e566adbd67ec37b8257a68d8c72877c"
    )


def test_v2_sha256_is_64_hex_chars():
    # The v2 docs example hash is a copy of the SHA512 one (documented error, see notes §5).
    qs = "symbol=btcidr&limit=100&timestamp=1578304294000&recvWindow=1578303937000"
    sig = sign_sha256(LEGACY_SECRET, qs)
    assert len(sig) == 64
    assert sig == "eeb688c782ef4cad36908b54ab4ff8fa259a44c40f7e573529f9d88c1070ae66"


def test_encode_params_preserves_order_and_bools():
    assert encode_params({"b": 1, "a": True, "c": "x y"}) == "b=1&a=true&c=x+y"


def test_clock_applies_offset():
    c = Clock(offset_ms=-250, now=lambda: 1000.0)
    assert c.timestamp_ms() == 1_000_000 - 250


def test_nonce_strictly_increasing_even_with_frozen_clock():
    g = NonceGenerator(Clock(now=lambda: 1.0))
    a, b, c = g.next(), g.next(), g.next()
    assert a < b < c
