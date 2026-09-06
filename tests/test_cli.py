from __future__ import annotations

import unittest

from cursor_usage_monitor.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_parser_documents_once_and_serve(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("--once", help_text)
        self.assertIn("serve", help_text)
        self.assertIn("usage", help_text)

    def test_help_via_main(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_version(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_no_args_prints_help(self) -> None:
        self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()
