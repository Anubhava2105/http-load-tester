import unittest

from http_load_tester.domain.errors import (
    ConnectionClosedEarly,
    InvalidBodyFraming,
    InvalidHeaders,
    ProtocolError,
    ResponseTooLarge,
)
from http_load_tester.domain.models import SafetyLimits
from http_load_tester.http.response_parser import ResponseParser


class ChunkReader:
    def __init__(self, payload: bytes, chunk_size: int = 1) -> None:
        self._payload = payload
        self._chunk_size = chunk_size

    def recv_some(self, max_bytes: int, deadline_ns: int | None = None) -> bytes:
        del deadline_ns
        if not self._payload:
            return b""
        amount = min(max_bytes, self._chunk_size, len(self._payload))
        chunk = self._payload[:amount]
        self._payload = self._payload[amount:]
        return chunk


class ResponseParserTests(unittest.TestCase):
    def test_content_length_survives_one_byte_fragmentation_and_preserves_next_response(
        self,
    ) -> None:
        payload = (
            b"HTTP/1.1 200 OK\r\ncOnNection: keep-alive\r\nContent-Length: 5\r\n\r\nhello"
            b"HTTP/1.1 204 No Content\r\n\r\n"
        )
        reader = ChunkReader(payload)
        parser = ResponseParser()

        first = parser.parse_response(reader, SafetyLimits())
        second = parser.parse_response(reader, SafetyLimits())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.body_bytes, 5)
        self.assertTrue(first.connection_reusable)
        self.assertEqual(second.status_code, 204)
        self.assertEqual(second.body_bytes, 0)
        self.assertTrue(second.connection_reusable)

    def test_chunked_body_accepts_extensions_and_trailers(self) -> None:
        payload = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"4;token=value\r\nWiki\r\n"
            b"5\r\npedia\r\n"
            b"0\r\nExpires: tomorrow\r\nX-Trace: yes\r\n\r\n"
        )
        response = ResponseParser().parse_response(ChunkReader(payload), SafetyLimits())

        self.assertEqual(response.body_bytes, 9)
        self.assertTrue(response.body_complete)
        self.assertTrue(response.connection_reusable)

    def test_no_body_status_does_not_consume_following_bytes(self) -> None:
        payload = (
            b"HTTP/1.1 204 No Content\r\nContent-Length: 4\r\n\r\n"
            b"next-response"
        )
        reader = ChunkReader(payload, chunk_size=len(payload))
        parser = ResponseParser()

        response = parser.parse_response(reader, SafetyLimits())

        self.assertEqual(response.body_bytes, 0)
        self.assertEqual(bytes(parser._buffer), b"next-response")
        self.assertTrue(response.connection_reusable)

    def test_close_delimited_body_is_complete_but_not_reusable(self) -> None:
        payload = b"HTTP/1.0 200 OK\r\n\r\nbody"
        response = ResponseParser().parse_response(ChunkReader(payload), SafetyLimits())

        self.assertEqual(response.body_bytes, 4)
        self.assertTrue(response.body_complete)
        self.assertFalse(response.connection_reusable)

    def test_incomplete_content_length_body_is_classified(self) -> None:
        payload = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhey"
        with self.assertRaises(ConnectionClosedEarly):
            ResponseParser().parse_response(ChunkReader(payload), SafetyLimits())

    def test_invalid_status_and_headers_are_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            ResponseParser().parse_response(
                ChunkReader(b"HTTP/2 200 OK\r\n\r\n"),
                SafetyLimits(),
            )
        with self.assertRaises(InvalidHeaders):
            ResponseParser().parse_response(
                ChunkReader(b"HTTP/1.1 200 OK\r\nBroken\r\n\r\n"),
                SafetyLimits(),
            )

    def test_duplicate_content_length_and_conflicting_framing_are_rejected(self) -> None:
        with self.assertRaises(InvalidBodyFraming):
            ResponseParser().parse_response(
                ChunkReader(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\ncontent-length: 1\r\n\r\nx"
                ),
                SafetyLimits(),
            )
        with self.assertRaises(InvalidBodyFraming):
            ResponseParser().parse_response(
                ChunkReader(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\nx"
                ),
                SafetyLimits(),
            )

    def test_header_and_body_limits_are_enforced(self) -> None:
        with self.assertRaises(ResponseTooLarge):
            ResponseParser().parse_response(
                ChunkReader(b"HTTP/1.1 200 Very Long Reason\r\n\r\n"),
                SafetyLimits(max_header_bytes=20),
            )
        with self.assertRaises(ResponseTooLarge):
            ResponseParser().parse_response(
                ChunkReader(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody"),
                SafetyLimits(max_response_body_bytes=3),
            )


if __name__ == "__main__":
    unittest.main()
