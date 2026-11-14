"""Container lifecycle tests without a Docker daemon."""

import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ebuildtester.docker import Docker
from ebuildtester.parse import parse_commandline
import ebuildtester.options as options


class TestPortageSettings(unittest.TestCase):
    """Exercise generated configuration commands in a temporary Portage tree."""

    def test_pkg_testing_directories_and_atom_settings(self):
        settings = parse_commandline([
            '--portage-dir', '/gentoo', '--atom', 'app-editors/nano',
            '--use', 'unicode'], {})
        container = Docker.__new__(Docker)
        with tempfile.TemporaryDirectory() as directory:
            portage = Path(directory) / 'portage'

            def execute(command):
                subprocess.run(
                    ['/bin/bash', '-c', command.replace(
                        '/etc/portage', str(portage))], check=True)

            with patch.object(options, 'OPTIONS', settings), \
                    patch.object(container, 'execute', side_effect=execute):
                container._prepare_portage_dirs()
                container._enable_test()
                container._unmask_atom()
                # Repeated preparation must preserve existing settings.
                container._prepare_portage_dirs()

            for name in ('env', 'package.env', 'package.accept_keywords',
                         'package.use', 'package.mask', 'package.unmask',
                         'package.license', 'repos.conf',
                         'profile/package.use.mask',
                         'profile/package.use.force',
                         'profile/package.use.stable.mask',
                         'profile/package.use.stable.force'):
                self.assertTrue((portage / name).is_dir(), name)
            self.assertEqual(
                (portage / 'package.env/testbuild').read_text(),
                'app-editors/nano tester.conf\n')
            self.assertEqual((portage / 'env/tester.conf').read_text(),
                             'FEATURES="test splitdebug"\n')
            self.assertEqual((portage / 'package.use/testbuild').read_text(),
                             'app-editors/nano unicode\n')
            self.assertEqual(
                (portage / 'package.accept_keywords/testbuild').read_text(),
                'app-editors/nano ~amd64\n')

    def test_ccache_make_conf(self):
        for cache in (None, '/host/compiler-cache'):
            with self.subTest(cache=cache):
                settings = parse_commandline([
                    '--portage-dir', '/gentoo', '--manual'] +
                    (['--ccache', cache] if cache else []), {})
                container = Docker.__new__(Docker)
                with patch.object(options, 'OPTIONS', settings), \
                        patch.object(container, 'execute') as execute:
                    container._tweak_settings()

                with tempfile.TemporaryDirectory() as directory:
                    make_conf = Path(directory) / 'make.conf'
                    for call in execute.call_args_list:
                        command = call.args[0]
                        if command.endswith('>> /etc/portage/make.conf'):
                            subprocess.run([
                                '/bin/bash', '-c', command.replace(
                                    '/etc/portage/make.conf', str(make_conf))],
                                check=True)
                    configuration = make_conf.read_text()
                    if cache:
                        self.assertIn('CCACHE_DIR="/var/tmp/ccache"\n',
                                      configuration)
                        self.assertIn('CCACHE_SIZE="4G"\n', configuration)
                    else:
                        self.assertNotIn('CCACHE_', configuration)


class TestCleanup(unittest.TestCase):
    def test_stop_with_optional_removal(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                container = Docker.__new__(Docker)
                container.cid = 'test-container'
                container._cleaned_up = False
                settings = SimpleNamespace(docker_command=['docker'], rm=remove)
                with patch.object(options, 'OPTIONS', settings), \
                        patch('ebuildtester.docker.subprocess.Popen') as popen:
                    container.cleanup()
                    container.cleanup()
                commands = [call.args[0] for call in popen.call_args_list]
                expected = [['docker', 'kill', 'test-container']]
                if remove:
                    expected.append(['docker', 'rm', 'test-container'])
                self.assertEqual(commands, expected)

    def test_cleanup_on_setup_failure(self):
        def fail(container, *args):
            container.cid = 'test-container'
            raise RuntimeError('setup failed')

        with patch.object(Docker, '_initialize', fail), \
                patch.object(Docker, 'cleanup') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'setup failed'):
                Docker('/portage', [])
        cleanup.assert_called_once()


class TestSessionExit(unittest.TestCase):
    """Exercise terminal input and signals with a local container stand-in."""

    def run_session(self, *, signum=None, setup=False, remove=True,
                    group=False, batch=False, repeat_interrupt=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = root / 'backend.py'
            backend.write_text(textwrap.dedent('''\
                from pathlib import Path
                import os
                import pty
                import sys

                root = Path(sys.argv[1])
                command = sys.argv[2]
                with (root / 'commands').open('a') as log:
                    log.write(command + '\\n')
                if command == 'exec':
                    if '-c' in sys.argv:
                        args = ['/bin/bash', '-c', sys.argv[-1]]
                    else:
                        args = [sys.executable, str(root / 'shell.py')]
                    if '--tty' in sys.argv:
                        assert sys.stdin.isatty()
                        status = os.waitstatus_to_exitcode(pty.spawn(args))
                        sys.exit(128 - status if status < 0 else status)
                    os.execv(args[0], args)
                elif command == 'kill':
                    (root / 'stop').touch()
            '''))
            (root / 'program.py').write_text(textwrap.dedent('''\
                from pathlib import Path
                import signal
                import sys
                import time

                root = Path(__file__).parent

                def interrupt(signum, frame):
                    (root / 'interrupted').touch()
                    sys.exit(130)

                signal.signal(signal.SIGINT, interrupt)
                (root / 'ready').touch()
                print('build stdout', flush=True)
                print('build stderr', file=sys.stderr, flush=True)
                while not (root / 'release').exists():
                    time.sleep(0.01)
            '''))
            (root / 'shell.py').write_text(textwrap.dedent('''\
                from pathlib import Path
                import signal
                import sys

                root = Path(__file__).parent
                signal.signal(signal.SIGINT, lambda *args:
                              (root / 'shell-interrupted').touch())
                (root / 'shell-ready').touch()
                (root / 'ready').touch()
                for line in sys.stdin:
                    if line.strip() == 'exit':
                        break
            '''))
            driver = textwrap.dedent('''\
                import sys
                from ebuildtester.docker import Docker
                from ebuildtester.main import _exit_on_signal, _run_container
                from ebuildtester.parse import parse_commandline
                import ebuildtester.options as options

                def initialize(self, *args):
                    self.cid = 'test-container'
                    if sys.argv[3] == 'True':
                        self.execute(f'{sys.executable} {sys.argv[2]}/program.py')

                Docker._initialize = initialize
                args = ['--manual', '--portage-dir', '/portage']
                if sys.argv[4] != 'True':
                    args.append('--no-rm')
                if sys.argv[5] == 'True':
                    args.append('--batch')
                options.OPTIONS = parse_commandline(args, {})
                options.OPTIONS.docker_command = [
                    sys.executable, sys.argv[1], sys.argv[2]]
                with _exit_on_signal():
                    _run_container()
            ''')
            terminal, stdin = pty.openpty()
            process = subprocess.Popen(
                [sys.executable, '-c', driver, str(backend), directory,
                 str(setup), str(remove), str(batch)],
                stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
            os.close(stdin)

            def wait_for(filename):
                deadline = time.monotonic() + 10
                while not (root / filename).exists():
                    if process.poll() is not None:
                        self.fail(process.communicate()[1])
                    if time.monotonic() > deadline:
                        self.fail(f'timed out waiting for {filename}')
                    time.sleep(0.01)

            try:
                wait_for('ready')
                if signum is None:
                    if setup:
                        (root / 'release').touch()
                        wait_for('shell-ready')
                    os.write(terminal, b'exit\n')
                elif signum == signal.SIGINT and not batch:
                    # Send the actual Ctrl-C keystroke through Docker's TTY.
                    os.write(terminal, b'\x03')
                    if setup:
                        wait_for('interrupted')
                        wait_for('shell-ready')
                    else:
                        wait_for('shell-interrupted')
                    if repeat_interrupt:
                        os.write(terminal, b'\x03')
                        wait_for('shell-interrupted')
                    self.assertIsNone(process.poll())
                    self.assertFalse((root / 'stop').exists())
                    self.assertNotIn('rm', (root / 'commands').read_text())
                    os.write(terminal, b'exit\n')
                elif group:
                    os.killpg(process.pid, signum)
                else:
                    process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=10)
                expected = (128 + signum if signum is not None
                            and (signum != signal.SIGINT or batch) else 0)
                self.assertEqual(process.returncode, expected, stdout + stderr)
                commands = (root / 'commands').read_text().splitlines()
                self.assertEqual(commands.count('kill'), 1)
                self.assertEqual(commands.count('rm'), int(remove))
                self.assertTrue((root / 'stop').exists())
                if setup and signum in (None, signal.SIGINT) and not batch:
                    self.assertIn('build stdout', stderr)
                    self.assertIn('build stderr', stderr)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                os.close(terminal)

    def test_shell_exit(self):
        self.run_session()

    def test_shell_exit_without_removal(self):
        self.run_session(remove=False)

    def test_build_completion(self):
        self.run_session(setup=True)

    def test_signals_during_shell(self):
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum):
                self.run_session(signum=signum)

    def test_signals_during_setup(self):
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum):
                self.run_session(signum=signum, setup=True)

    def test_terminal_hangup(self):
        self.run_session(signum=signal.SIGHUP, setup=True, group=True)

    def test_interrupt_recovery_shell(self):
        self.run_session(signum=signal.SIGINT, setup=True, repeat_interrupt=True)

    def test_interrupt_without_removal(self):
        self.run_session(signum=signal.SIGINT, setup=True, remove=False)

    def test_batch_interrupt(self):
        self.run_session(signum=signal.SIGINT, setup=True, batch=True)
