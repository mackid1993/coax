import unittest

from app import server


class Round1Tests(unittest.TestCase):
    def test_fix_url(self):
        self.assertEqual(server.fix_url("http:/192.168.1.5:7780/"), "http://192.168.1.5:7780")
        self.assertEqual(server.fix_url(" HTTP:192.168.1.5 "), "http://192.168.1.5")
        self.assertEqual(server.fix_url("http:///x"), "http://x")
        self.assertEqual(server.fix_url("http://x:8089"), "http://x:8089")
        self.assertEqual(server.fix_url(""), "")


if __name__ == "__main__":
    unittest.main()
