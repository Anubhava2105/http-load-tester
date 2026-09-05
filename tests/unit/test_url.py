import unittest

from http_load_tester.domain.errors import InvalidTarget
from http_load_tester.http.url import parse_target


class UrlParsingTests(unittest.TestCase):
    def test_http_defaults_port_preserves_path_query_and_drops_fragment(self) -> None:
        parsed = parse_target("http://example.test/health?full=1#ignored")
        self.assertEqual(parsed.origin.scheme, "http")
        self.assertEqual(parsed.origin.port, 80)
        self.assertEqual(parsed.request_target, "/health?full=1")

    def test_https_custom_port(self) -> None:
        parsed = parse_target("https://example.test:8443/api")
        self.assertEqual(parsed.origin.port, 8443)
        self.assertTrue(parsed.origin.tls_verify)

    def test_ipv6_literal_is_stored_without_brackets(self) -> None:
        parsed = parse_target("http://[::1]:8080/")
        self.assertEqual(parsed.origin.hostname, "::1")
        self.assertEqual(parsed.origin.port, 8080)

    def test_empty_path_becomes_root(self) -> None:
        parsed = parse_target("http://example.test?ready=yes")
        self.assertEqual(parsed.request_target, "/?ready=yes")

    def test_invalid_target_features_are_rejected(self) -> None:
        for target in (
            "ftp://example.test/file",
            "http:///missing-host",
            "http://user:pass@example.test/",
            "http://example.test:not-a-port/",
        ):
            with self.subTest(target=target):
                with self.assertRaises(InvalidTarget):
                    parse_target(target)


if __name__ == "__main__":
    unittest.main()
