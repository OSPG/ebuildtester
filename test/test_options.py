"""Console logging tests, including Docker's raw terminal mode."""

import io
import logging
import os
import pty
import select
import tempfile
import tty
import unittest

from ebuildtester.options import _ConsoleHandler


class TestConsoleLogging(unittest.TestCase):
    def test_raw_terminal_lines_start_at_left_margin(self):
        master, slave = pty.openpty()
        try:
            tty.setraw(slave)
            with os.fdopen(os.dup(slave), 'w') as stream:
                handler = _ConsoleHandler(stream)
                logger = logging.Logger('raw-terminal-test')
                logger.addHandler(handler)
                logger.info('first line')
                logger.info('second line')
                expected = b'first line\r\nsecond line\r\n'
                output = b''
                while len(output) < len(expected):
                    ready, _, _ = select.select([master], [], [], 1)
                    self.assertTrue(ready, 'timed out reading terminal output')
                    output += os.read(master, 1024)
                self.assertEqual(output, expected)
        finally:
            os.close(slave)
            os.close(master)

    def test_redirected_console_and_logfile_use_plain_newlines(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'session.log')
            console = io.StringIO()
            logger = logging.Logger('redirected-test')
            logger.addHandler(_ConsoleHandler(console))
            handler = logging.FileHandler(filename)
            logger.addHandler(handler)
            try:
                logger.info('first line')
                logger.info('second line')
            finally:
                handler.close()
            self.assertEqual(console.getvalue(), 'first line\nsecond line\n')
            with open(filename, 'rb') as logfile:
                self.assertEqual(logfile.read(), b'first line\nsecond line\n')
