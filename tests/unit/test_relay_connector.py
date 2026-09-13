from lib.relay_connector import forward_request_headers


def test_relay_always_marks_both_authenticated_and_anonymous_requests_remote():
    for meta in [{}, {'X-Clarp-Transport': 'local', 'Authorization': 'Bearer device'}]:
        headers = forward_request_headers(meta, '127.0.0.1:7682', 0)
        assert headers['X-Clarp-Transport'] == 'relay'
        assert headers['Accept-Encoding'] == 'identity'


def test_relay_preserves_verified_client_address_for_failure_throttling():
    headers = forward_request_headers({'x-forwarded-for': '192.0.2.10'}, '127.0.0.1:7682', 0)
    assert headers['X-Forwarded-For'] == '192.0.2.10'
