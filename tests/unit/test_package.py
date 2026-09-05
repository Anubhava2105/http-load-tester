import unittest

import http_load_tester


class PackageSmokeTests(unittest.TestCase):
    def test_package_exposes_version(self) -> None:
        self.assertEqual(http_load_tester.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()
