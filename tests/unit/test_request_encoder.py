import unittest

from http_load_tester.domain.errors import InvalidBodyFraming, InvalidHeaders
from http_load_tester.domain.models import HttpRequest, Origin
from http_load_tester.http.request_encoder import encode_request


class RequestEncoderTests(unittest.TestCase):
    def test_get_generates_host_identity_encoding_and_zero_length(self) -> None:
        encoded = encode_request(
            HttpRequest("GET", "/health?ready=1"),
            Origin("http", "example.test", 80),
        )
        self.assertEqual(
            encoded,
            b"GET /health?ready=1 HTTP/1.1\r\n"
            b"Host: example.test\r\n"
            b"Accept-Encoding: identity\r\n"
            b"Content-Length: 0\r\n\r\n",
        )

    def test_post_preserves_custom_host_and_materialized_body(self) -> None:
        encoded = encode_request(
            HttpRequest(
                "post",
                "/items",
                headers=(("hOsT", "api.example.test:8443"), ("Content-Type", "text/plain")),
                body=b"hello",
            ),
            Origin("https", "api.example.test", 8443),
        )
        self.assertIn(b"POST /items HTTP/1.1\r\n", encoded)
        self.assertIn(b"hOsT: api.example.test:8443\r\n", encoded)
        self.assertIn(b"Content-Type: text/plain\r\n", encoded)
        self.assertIn(b"Content-Length: 5\r\n", encoded)
        self.assertTrue(encoded.endswith(b"\r\n\r\nhello"))

    def test_ipv6_host_is_bracketed_and_custom_port_is_kept(self) -> None:
        encoded = encode_request(
            HttpRequest("GET", "/"),
            Origin("http", "::1", 8080),
        )
        self.assertIn(b"Host: [::1]:8080\r\n", encoded)

    def test_conflicting_framing_is_rejected(self) -> None:
        origin = Origin("http", "example.test", 80)
        with self.assertRaises(InvalidBodyFraming):
            encode_request(HttpRequest("POST", "/", headers=(("Content-Length", "4"),), body=b"five!"), origin)
        with self.assertRaises(InvalidBodyFraming):
            encode_request(HttpRequest("POST", "/", headers=(("Transfer-Encoding", "chunked"),), body=b"data"), origin)

    def test_ambiguous_headers_are_rejected(self) -> None:
        origin = Origin("http", "example.test", 80)
        with self.assertRaises(InvalidHeaders):
            encode_request(
                HttpRequest("GET", "/", headers=(("Host", "one"), ("host", "two"))),
                origin,
            )


if __name__ == "__main__":
    unittest.main()
