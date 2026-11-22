"""Closed-output parser regressions, runnable without PyArrow installed."""
import contextlib
import io
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import rgwitness.__main__ as cli


def parser_with_color(*, color=True):
    return color


def parser_without_color():
    return None


def formatter_with_color(*, prog, color=True):
    return prog, color


def formatter_without_color(*, prog):
    return prog


class ClosedProbeStream(io.StringIO):
    def isatty(self):
        raise AssertionError('a closed stdout must not be probed')


class ArgparseOutputTests(unittest.TestCase):
    def options_for(self, parser, formatter):
        stream = io.StringIO()
        stream.close()
        api = SimpleNamespace(ArgumentParser=parser, HelpFormatter=formatter)
        with contextlib.redirect_stdout(stream), patch.object(cli, 'argparse', api):
            options = cli._parser_options()
            self.assertIs(sys.stdout, stream)
        self.assertTrue(stream.closed)
        return options

    def test_closed_stream_without_color_keywords(self):
        self.assertEqual(self.options_for(parser_without_color, formatter_without_color), {})

    def test_closed_stream_with_both_color_keywords(self):
        options = self.options_for(parser_with_color, formatter_with_color)
        self.assertEqual(set(options), {'color', 'formatter_class'})
        self.assertIs(options['color'], False)
        self.assertEqual(options['formatter_class'](prog='rgwitness'), ('rgwitness', False))

    def test_closed_stream_with_parser_color_only(self):
        self.assertEqual(self.options_for(parser_with_color, formatter_without_color),
                         {'color': False})

    def test_closed_stream_with_formatter_color_only(self):
        options = self.options_for(parser_without_color, formatter_with_color)
        self.assertEqual(set(options), {'formatter_class'})
        self.assertEqual(options['formatter_class'](prog='rgwitness'), ('rgwitness', False))

    def test_open_stream_keeps_argparse_defaults(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), patch.object(cli.inspect, 'signature') as signature:
            self.assertEqual(cli._parser_options(), {})
            signature.assert_not_called()
            self.assertIs(sys.stdout, stream)
        self.assertFalse(stream.closed)

    def test_closed_stream_is_not_probed(self):
        stream = ClosedProbeStream()
        stream.close()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(cli.main(['/missing/rgwitness.parquet']), 2)
            self.assertIs(sys.stdout, stream)
        self.assertTrue(stream.closed)

    def test_closed_real_file_remains_caller_owned(self):
        stream = tempfile.TemporaryFile(mode='w+')
        stream.close()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(cli.main(['/missing/rgwitness.parquet']), 2)
            self.assertIs(sys.stdout, stream)
        self.assertTrue(stream.closed)

    def test_closed_stream_retains_usage_errors(self):
        cases = (
            ([], 'required'),
            (['one', '--not-an-option'], '--not-an-option'),
            (['one', 'two'], 'multiple files require --batch'),
            (['one', '--include-schema', '--batch'], '--include-schema is for single files'),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                stream = io.StringIO()
                stream.close()
                errors = io.StringIO()
                with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(arguments)
                    self.assertIs(sys.stdout, stream)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('usage:', errors.getvalue())
                self.assertIn(message, errors.getvalue())
                self.assertTrue(stream.closed)

    def test_open_stream_retains_help(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                cli.main(['--help'])
            self.assertIs(sys.stdout, output)
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('usage:', output.getvalue())
        self.assertIn('--include-schema', output.getvalue())
        self.assertEqual(errors.getvalue(), '')
