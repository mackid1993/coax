import os as _os

_os.environ.setdefault("COAX_SKIP_URL_CHECK", "1")

import logging
import unittest

from app import server


class LogTests(unittest.TestCase):
    def test_buffer_size_and_tail(self):
        h = server.RingLogHandler(10000)
        log = logging.getLogger("coax.test.logs")
        log.addHandler(h)
        log.setLevel(logging.INFO)
        for i in range(12000):
            log.info("line %d", i)
        log.removeHandler(h)
        all_ = h.tail(0, 0)["lines"]
        self.assertEqual(len(all_), 10000)
        self.assertEqual(all_[-1]["msg"], "line 11999")
        self.assertEqual(len(h.tail(0, 50)["lines"]), 50)


if __name__ == "__main__":
    unittest.main()
