"""The Docker backend."""

import os
import re
import sys
import subprocess
import signal
from threading import Thread
from platformdirs import user_cache_dir

import ebuildtester.options as options
from ebuildtester.utils import massage_string


class ExecuteFailure(Exception):
    """Failure to execute command."""
    pass


class Docker:
    """The Docker class."""
    def __init__(self, local_portage, overlay_dirs):
        """Create a new container."""

        self.cid = None
        self._cleaned_up = False
        try:
            self._initialize(local_portage, overlay_dirs)
        except BaseException:
            self.cleanup()
            raise

    def _initialize(self, local_portage, overlay_dirs):
        """Create and configure the container."""

        docker_image = options.OPTIONS.docker_image
        repo_names = self._get_repo_names(overlay_dirs)
        overlay_mountpoints = [os.path.join("/var/lib/overlays", r)
                               for r in repo_names]

        self._setup_container(docker_image)
        self._create_container(docker_image, local_portage,
                               zip(overlay_dirs, overlay_mountpoints))
        self._start_container()
        self._prepare_portage_dirs()
        self._set_profile()
        self._enable_global_use()
        self._tweak_settings()
        self._enable_overlays(repo_names)
        self._enable_test()
        self._unmask_atom()
        self._unmask()
        self._update()
        self._install_basics()
        self._set_gcc()
        self._print_summary()

    def execute(self, cmd):
        """Execute command in container.

        cmd is a list of strings or a simple string with a command that is
        executed within a bash shell.
        """

        if isinstance(cmd, list):
            cmd_string = ' '.join(cmd)
        else:
            cmd_string = cmd
        options.log.info("%s %s", self.cid[:6], cmd)
        interactive = sys.stdin.isatty() and not options.OPTIONS.batch
        docker_cmd = options.OPTIONS.docker_command + ["exec"]
        if interactive:
            # Docker forwards terminal input, including Ctrl-C, to the command.
            docker_cmd += ["--tty", "--interactive"]
        docker_cmd += [self.cid, "/bin/bash", "-c", cmd_string]
        docker = subprocess.Popen(docker_cmd,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  stdin=None if interactive
                                  else subprocess.DEVNULL,
                                  universal_newlines=True)
        readers = [Thread(target=self._reader, args=(stream, name))
                   for stream, name in ((docker.stdout, "stdout"),
                                        (docker.stderr, "stderr"))]
        for reader in readers:
            reader.start()
        try:
            while True:
                try:
                    docker.wait()
                    break
                except KeyboardInterrupt:
                    if not interactive:
                        raise SystemExit(128 + signal.SIGINT)
                    # Keep the client attached while the command handles Ctrl-C.
        finally:
            if docker.poll() is None:
                docker.terminate()
                docker.wait()
            for reader in readers:
                reader.join()
            docker.stdout.close()
            docker.stderr.close()

        if interactive and docker.returncode == 128 + signal.SIGINT:
            options.log.info("command interrupted, opening interactive shell")
            self.shell()
            # Do not resume setup or the build after leaving the recovery shell.
            raise SystemExit(0)

        if docker.returncode != 0:
            options.log.error("running in container %s" % (str(self.cid)))
            options.log.error("failed command \"%s\"" % (cmd))

    def shell(self):
        """Run an interactive shell in container."""

        options.log.info("running interactive shell in container")
        docker = subprocess.Popen(options.OPTIONS.docker_command
                                  + ["exec", "--tty", "--interactive",
                                     self.cid, "/bin/bash"])
        try:
            while True:
                try:
                    docker.wait()
                    return
                except KeyboardInterrupt:
                    # The attached shell handles Ctrl-C; wait for its exit.
                    continue
        finally:
            if docker.poll() is None:
                docker.terminate()
                docker.wait()

    def cleanup(self):
        """Stop the container and optionally remove it."""

        if not self.cid or self._cleaned_up:
            return
        self._cleaned_up = True
        self.stop()
        if options.OPTIONS.rm:
            self.remove()

    def stop(self):
        """Kill the container and all commands running inside it."""

        options.log.info("killing container")
        docker = subprocess.Popen(options.OPTIONS.docker_command
                                  + ["kill", self.cid])
        docker.wait()

    def remove(self):
        """Remove the stopped docker container."""

        options.log.info("deleting container")
        docker = subprocess.Popen(options.OPTIONS.docker_command
                                  + ["rm", self.cid])
        docker.wait()

    def _reader(self, stream, name):
        """Read from a subprocess stream."""

        for out in stream:
            options.log.info("%s (%s): %s" %
                             (self.cid[:6], name, out.rstrip()))
            options._log_ch.flush()

    def _setup_container(self, docker_image):
        """Setup the container."""

        if options.OPTIONS.pull:
            docker_args = options.OPTIONS.docker_command \
                + ["pull", docker_image]
            docker = subprocess.Popen(docker_args)
            docker.wait()

    def _create_container(self, docker_image, portdir, overlays):
        """Create new container."""

        if not os.path.isdir(portdir):
            raise Exception(f"Portage dir does not exists: {portdir}")

        cache_dir = user_cache_dir("ebuildtester")
        os.makedirs(cache_dir, exist_ok=True)

        distdir = "{}/distfiles".format(cache_dir)
        os.makedirs(distdir, exist_ok=True)

        pkgdir = "{}/packages".format(cache_dir)
        os.makedirs(pkgdir, exist_ok=True)

        if options.OPTIONS.batch:
            portdir_opt = "ro"
        else:
            portdir_opt = "rw"

        docker_args = options.OPTIONS.docker_command \
            + ["create",
               "--tty",
               "--cap-add", "CAP_SYS_ADMIN",
               "--cap-add", "CAP_MKNOD",
               "--cap-add", "CAP_NET_ADMIN",
               # https://github.com/moby/moby/issues/16429
               "--security-opt", "apparmor:unconfined",
               "--device", "/dev/fuse",
               "--workdir", "/root",
               "--volume", f"{portdir}:/var/db/repos/gentoo:{portdir_opt}",
               "--volume", f"{distdir}:/var/cache/distfiles",
               "--volume", f"{pkgdir}:/var/cache/binpkgs"]

        if options.OPTIONS.storage_opt:
            for s in options.OPTIONS.storage_opt:
                docker_args += ["--storage-opt", "%s" % s]

        ccache = options.OPTIONS.ccache
        if ccache:
            docker_args += ["--volume=%s:/var/tmp/ccache" % ccache]

        for o in overlays:
            docker_args += ["--volume=%s:%s" % o]

        docker_args += [docker_image]

        # for compatibility with podman
        e = os.environ.copy()
        e["PODMAN_USERNS"] = "keep-id:uid=0"

        options.log.info("creating docker container with: %s" %
                         " ".join(docker_args))
        docker = subprocess.Popen(docker_args, stdout=subprocess.PIPE, env=e)
        docker.wait()

        if docker.returncode != 0:
            raise Exception("failure creating docker container")

        lines = docker.stdout.readlines()
        if len(lines) > 1:
            raise Exception("more output than expected")
        self.cid = massage_string(lines[0]).strip()
        options.log.info("container id %s" % (self.cid))

    def _start_container(self):
        """Start the container."""

        docker_args = options.OPTIONS.docker_command \
            + ["start", "%s" % self.cid]
        docker = subprocess.Popen(docker_args, stdout=subprocess.PIPE)
        docker.wait()
        if docker.returncode != 0:
            raise Exception("failure creating docker container")

    def _set_profile(self):
        """Set the Gentoo profile."""

        options.log.info("setting Gentoo profile to %s" %
                         options.OPTIONS.profile)
        self.execute("eselect profile set %s" % options.OPTIONS.profile)

    def _prepare_portage_dirs(self):
        """Create configuration directories used by pkg-testing-tools."""

        directories = [
            "env", "package.accept_keywords", "package.env", "package.license",
            "package.mask", "package.unmask", "package.use", "repos.conf",
            "profile/package.use.mask", "profile/package.use.force",
            "profile/package.use.stable.mask",
            "profile/package.use.stable.force",
        ]
        self.execute("mkdir -p " + " ".join(
            "/etc/portage/" + directory for directory in directories))

    def _tweak_settings(self):
        """Tweak portage settings."""

        options.log.info("tweaking portage settings")

        # Disable the usersandbox feature, it's not working well inside a
        # docker container.
        self.execute(
            f"echo FEATURES=\\\"{' '.join(options.OPTIONS.features)}\\\" "
            ">> /etc/portage/make.conf")

        self.execute(("echo MAKEOPTS=\\\"-j%d\\\" " %
                      (options.OPTIONS.threads)) +
                     ">> /etc/portage/make.conf")
        if options.OPTIONS.ccache:
            self.execute("echo 'CCACHE_DIR=\"/var/tmp/ccache\"' "
                         ">> /etc/portage/make.conf")
            self.execute("echo 'CCACHE_SIZE=\"4G\"' "
                         ">> /etc/portage/make.conf")
        if options.OPTIONS.binhost:
            self.execute("getuto")
            verify_signature = str(options.OPTIONS.verify_signature).lower()
            self.execute("echo -e '[ebuildtester]\n"
                         "priority = 999\n"
                         "verify-signature = {}\n"
                         "sync-uri = {}\n'"
                         ">> /etc/portage/binrepos.conf/ebuildtester.conf"
                         .format(verify_signature, options.OPTIONS.binhost))
        if options.OPTIONS.unstable:
            self.execute("echo ACCEPT_KEYWORDS=\\\"~amd64\\\" " +
                         ">> /etc/portage/make.conf")
        if options.OPTIONS.with_X:
            self.execute("emerge --verbose gentoolkit")
            self.execute("euse --enable X")
        if options.OPTIONS.python_single_target:
            self.execute(("echo */* PYTHON_SINGLE_TARGET: %s" %
                          (options.OPTIONS.python_single_target)) +
                         " >> /etc/portage/package.use/python")
        if options.OPTIONS.python_targets:
            self.execute(("echo */* PYTHON_TARGETS: %s" %
                          (options.OPTIONS.python_targets)) +
                         " >> /etc/portage/package.use/python")

        # Avoid wasting time generating the whole set
        self.execute('echo "C.UTF-8 UTF-8" > /etc/locale.gen')

        # Use callers UID as portage UID for compatibility with podman
        self.execute(f'sed -i "s/250/{os.getuid()}/g" /etc/passwd')

    def _get_repo_names(self, overlay_dirs):
        """Get repo names from local overlay settings."""

        repo_names = []
        for o in overlay_dirs:
            with open(os.path.join(o, "profiles/repo_name"), "r") as f:
                for repo_name in f:
                    repo_names.append(repo_name.replace("\n", ""))

        return repo_names

    def _enable_overlays(self, repo_names):
        """Enable overlays."""

        for r in repo_names:
            self.execute(
                "mkdir -p /etc/portage/repos.conf && " +
                "echo -e \"[%s]\\n" % str(r) +
                "location = /var/lib/overlays/%s\\n" % str(r) +
                "master = gentoo\" >> /etc/portage/repos.conf/overlays.conf"
            )

    def _enable_test(self):
        """Enable test FEATURES for ATOM."""

        if options.OPTIONS.atom is not None:
            options.log.info("enabling test feature for %s" %
                             options.OPTIONS.atom)
            for a in options.OPTIONS.atom:
                self.execute(
                    "echo \"%s tester.conf\" >> "
                    "/etc/portage/package.env/testbuild" % a)
            self.execute(
                "echo \"FEATURES=\\\"test splitdebug\\\"\" " +
                "> /etc/portage/env/tester.conf")
        else:
            options.log.info("enabling tests skipped, no atoms specified")

    def _unmask_atom(self):
        """Unmask the atom to install."""

        if options.OPTIONS.atom is not None:
            options.log.info("unmasking %s" % options.OPTIONS.atom)
            for a in options.OPTIONS.atom:
                if options.OPTIONS.live_ebuild:
                    unmask_keyword = "**"
                else:
                    unmask_keyword = "~amd64"
                self.execute("mkdir -p /etc/portage/package.accept_keywords")
                self.execute(
                    "echo \"" + str(a) + "\" " + unmask_keyword + " >> " +
                    "/etc/portage/package.accept_keywords/testbuild")
            if len(options.OPTIONS.use) > 0:
                for a in options.OPTIONS.atom:
                    self.execute(
                        "echo %s %s >> /etc/portage/package.use/testbuild" %
                        (str(a), " ".join(options.OPTIONS.use)))
        else:
            options.log.info("no atoms to unmask")

    def _unmask(self):
        """Unmask other atoms."""

        options.log.info("unmasking additional atoms")
        for a in options.OPTIONS.unmask:
            options.log.info("  unmasking %s" % a)
            self.execute("mkdir -p /etc/portage/package.accept_keywords")
            self.execute(
                "echo \"%s\" ~amd64 >> "
                "/etc/portage/package.accept_keywords/testbuild" % a)

    def _update(self):
        """Update container."""

        if not options.OPTIONS.update:
            options.log.info("skipping update")
        else:
            options.log.info("updating container")
            update_options = ["--verbose", "--update",
                              "--deep", "--newuse", "--changed-deps"]
            self.execute("emerge " + " ".join(update_options) + " @world")

    def _install_basics(self):
        """Install some basic packages."""

        if options.OPTIONS.ccache:
            self.execute("emerge --verbose dev-util/ccache")

        if not options.OPTIONS.install_basic_packages:
            options.log.info("skipping basic packages")
            if options.OPTIONS.pkg_testing_tools:
                self.execute("emerge --verbose app-portage/pkg-testing-tools")
        else:
            options.log.info("installing basic packages: %s" %
                             options.base_packages)
            self.execute("emerge --verbose %s" %
                         " ".join(map(str, options.base_packages)))

        if options.OPTIONS.with_vnc:
            t = ["net-misc/tigervnc", "x11-wm/icewm"]
            options.log.info("installing VNC packages: %s" % t)
            self.execute("emerge --verbose %s" % " ".join(map(str, t)))

    def _enable_global_use(self):
        """Enable global USE settings."""
        if not options.OPTIONS.global_use:
            options.log.info("no global USE flags given, skipping")
        else:
            options.log.info("setting global USE flags")
            self.execute("emerge --verbose gentoolkit")
            for u in options.OPTIONS.global_use:
                self.execute("euse --enable %s" % u)

    def _set_gcc(self):
        """Set gcc in the container."""

        options.log.info("setting gcc")
        if options.OPTIONS.gcc_version:
            self.execute("mkdir -p /etc/portage/package.accept_keywords")
            self.execute(
                ("echo =sys-devel/gcc-%s ** >> " %
                 options.OPTIONS.gcc_version) +
                "/etc/portage/package.accept_keywords/testbuild")
            self.execute("emerge --verbose sys-devel/gcc")
            gcc = re.sub("-r[0-9]+$", "", options.OPTIONS.gcc_version)
            self.execute("gcc-config $(gcc-config --list-profiles | " +
                         ("grep %s | " % gcc) +
                         "sed -e 's:^.*\\[\\([0-9]\\+\\)\\].*:\\1:')")
            self.execute("emerge --verbose --oneshot sys-devel/libtool")

    def _print_summary(self):
        """Print summary."""

        options.log.info("summary")
        self.execute(
            "if [[ -d /etc/portage/package.accept_keywords ]]; then " +
            "cat /etc/portage/package.accept_keywords/*; fi")
        self.execute("if [[ -f /etc/portage/package.use/testbuild ]]; then " +
                     "cat /etc/portage/package.use/testbuild; fi")
        self.execute("emerge --info")
        self.execute("qlop")
