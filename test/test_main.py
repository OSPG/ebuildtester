"""Tests for automatic package testing without a container daemon."""

import shlex
import unittest
from unittest.mock import patch

from ebuildtester.docker import Docker
from ebuildtester.main import _run_container
from ebuildtester.parse import parse_commandline
import ebuildtester.options as options


class TestPackageTesting(unittest.TestCase):
    def test_automatic_testing(self):
        for batch in (False, True):
            for cached in (False, True):
                with self.subTest(batch=batch, cached=cached):
                    args = [
                        '--portage-dir', '/gentoo', '--pkg-testing-tools',
                        '--atom', '=app-editors/nano-8.0', 'dev-vcs/git']
                    if batch:
                        args.append('--batch')
                    if cached:
                        args += ['--ccache', '/cache', '--binhost',
                                 'https://binhost.example']
                    settings = parse_commandline(args, {})
                    with patch.object(options, 'OPTIONS', settings), \
                            patch('ebuildtester.main.Docker') as docker:
                        _run_container()
                    container = docker.return_value
                    container.execute.assert_called_once()
                    command = shlex.split(container.execute.call_args.args[0])
                    self.assertEqual(command[0], 'pkg-testing-tool')
                    self.assertIn('--package-atom==app-editors/nano-8.0-r0',
                                  command)
                    self.assertIn('--package-atom=dev-vcs/git', command)
                    self.assertEqual(
                        command[command.index('--test-feature-scope') + 1],
                        'always')
                    self.assertIn('--autounmask-continue=y',
                                  command[command.index('--append-emerge')
                                          + 1])
                    self.assertEqual('--ccache' in command, cached)
                    self.assertEqual('--binpkg' in command, cached)
                    self.assertEqual(container.shell.call_count,
                                     int(not batch))
                    container.cleanup.assert_called_once()

    def test_cleanup_when_testing_is_interrupted(self):
        settings = parse_commandline([
            '--portage-dir', '/gentoo', '--pkg-testing-tools',
            '--atom', 'app-editors/nano'], {})
        with patch.object(options, 'OPTIONS', settings), \
                patch('ebuildtester.main.Docker') as docker:
            docker.return_value.execute.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                _run_container()
        docker.return_value.cleanup.assert_called_once()

    def test_tool_installed_without_basic_packages(self):
        settings = parse_commandline([
            '--portage-dir', '/gentoo', '--pkg-testing-tools',
            '--atom', 'app-editors/nano'], {})
        container = Docker.__new__(Docker)
        with patch.object(options, 'OPTIONS', settings), \
                patch.object(container, 'execute') as execute:
            container._install_basics()
        execute.assert_called_once_with(
            'emerge --verbose app-portage/pkg-testing-tools')

    def test_default_build(self):
        settings = parse_commandline([
            '--portage-dir', '/gentoo', '--batch',
            '--atom', 'app-editors/nano'], {})
        with patch.object(options, 'OPTIONS', settings), \
                patch('ebuildtester.main.Docker') as docker:
            _run_container()
        command = docker.return_value.execute.call_args.args[0]
        self.assertEqual(command[0], 'emerge')
        self.assertIn('app-editors/nano', command)
        docker.return_value.shell.assert_not_called()
        docker.return_value.cleanup.assert_called_once()
