# Copyright 2022-2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.
"""
Spack environments house software stacks.

This module contains classes and methods that will help manage a spack
environment by calling an externally available spack.
"""

import os
import re
import shutil

import llnl.util.tty as tty
import llnl.util.filesystem as fs
from spack.util.executable import which, CommandNotFoundError, ProcessError
import spack.util.spack_yaml as syaml

import ramble.config
import ramble.error
import ramble.util.hashing

spack_namespace = 'spack'

package_name_regex = re.compile(r"\s*(?P<package_name>[\w][\w-]+).*")


class SpackRunner(object):
    """Runner for executing several spack commands

    The SpackRunner class is primarily used to manage spack environments
    for executing experiments under.

    This class provides methods for creating and manaving spack environments,
    and for ensuring required compilers are installed. It also provides a
    method for generating variables that can be used to ensure a spack env
    is loaded within an execution script.
    """
    env_key = 'SPACK_ENV'

    global_arg_config_name = 'config:spack_flags:global_args'

    env_create_args = [
        'env',
        'create',
        '-d',
        '.'
    ]

    compiler_find_args = ['compiler', 'find']

    _allowed_config_files = ['compilers.yaml', 'concretizer.yaml',
                             'mirrors.yaml', 'repos.yaml',
                             'packages.yaml', 'modules.yaml',
                             'config.yaml', 'upstreams.yaml',
                             'bootstrap.yaml', 'spack.yaml']

    def __init__(self, shell='bash', dry_run=False):
        """
        Ensure spack is found in the path, and setup some default variables.
        """
        try:
            self.exe = which('spack', required=True)
        except CommandNotFoundError:
            raise RunnerError("Spack command is not found in path")

        # Add default arguments to spack command.
        # This allows us to inject custom config scope dirs
        # primarily for unit testing.
        global_args = ramble.config.get(self.global_arg_config_name)
        if global_args:
            for arg in global_args.split():
                self.exe.add_default_arg(arg)

        self.spack_dir = os.path.dirname(os.path.dirname(self.exe.exe[0]))
        self.shell = shell

        if self.shell == 'bash':
            script = 'setup-env.sh'
        elif self.shell == 'csh':
            script = 'setup-env.csh'
        elif self.shell == 'fish':
            script = 'setup-env.fish'
        self.source_script = os.path.join(self.spack_dir,
                                          'share', 'spack', script)

        self.concretized = False
        self.installed = False
        self.hash = None
        self.env_path = None
        self.active = False
        self.compilers = []
        self.includes = []
        self.dry_run = dry_run
        self.concretized = False
        self.compiler_config_dir = None
        self.configs = []
        self.configs_applied = False

    def get_version(self):
        """Get spack's version"""
        from ramble.main import get_git_hash
        import importlib.util

        version_spec = importlib.util.spec_from_file_location(
            'spack_version',
            os.path.join(self.spack_dir,
                         'lib', 'spack',
                         'spack', '__init__.py')
        )
        version_mod = importlib.util.module_from_spec(version_spec)
        version_spec.loader.exec_module(version_mod)

        spack_version = version_mod.spack_version
        spack_hash = get_git_hash(path=self.spack_dir)

        if spack_hash:
            spack_version += f' ({spack_hash})'

        return spack_version

    def set_dry_run(self, dry_run=False):
        """
        Set the dry_run state of this spack runner
        """
        self.dry_run = dry_run

    def set_compiler_config_dir(self, path=None):
        """
        Set the config path to use when installing compilers
        """
        self.compiler_config_dir = path

    def set_env(self, env_path):
        if not os.path.isdir(env_path) or not os.path.exists(os.path.join(env_path, 'spack.yaml')):
            tty.die(f'Path {env_path} is not a spack environment')

        self.env_path = env_path

    def generate_source_command(self, shell='bash'):
        """
        Generate a string to source spack into an environment
        """

        commands = ['. %s' % self.source_script]

        return commands

    def generate_activate_command(self, shell='bash'):
        """
        Generate a string to activate a spack environment
        """

        commands = []
        if self.active:
            commands.append('spack env activate %s' % self.env_path)

        return commands

    def generate_deactivate_command(self, shell='bash'):
        """
        Generate a string to deactivate a spack environment
        """

        commands = []

        if self.active:
            commands.append('spack env deactivate')

        return commands

    def configure_env(self, path):
        """
        Configured the spack environment path for subsequent spack commands
        """

        # Ensure subsequent commands use the created env now.
        self.env_path = path

    def add_config(self, config):
        """
        Add a config option to this spack environment.
        """
        self.configs.append(config)

    def create_env(self, path, output=None, error=None):
        """
        Ensure a spack environment is created, and set the path to it within
        this runner.
        """
        if os.path.exists(path) and not os.path.isdir(path):
            raise RunnerError('Unable to create environment %s' % path)

        if not os.path.exists(path):
            fs.mkdirp(path)

        # Create a spack env
        if not self.dry_run:
            if not os.path.exists(os.path.join(path, 'spack.yaml')):
                with fs.working_dir(path):
                    self.exe(*self.env_create_args)

        # Ensure subsequent commands use the created env now.
        self.env_path = path

    def load_compiler(self, spec):
        """
        Add commands to load a package to the executable
        """
        if self.shell == 'bash':
            regex = \
                re.compile('\A.*export ' +  # noqa: W605
                           '(?P<var>[\S^=]+)=' +  # noqa: W605
                           '(?P<val>[\S]+);\Z')  # noqa: W605

            shell_flag = '--sh'
        elif self.shell == 'csh':
            regex = \
                re.compile('\A.*setenv ' +  # noqa: W605
                           '(?P<var>[\S^=]+) ' +  # noqa: W605
                           '(?P<val>[\S]+);\Z')  # noqa: W605
            shell_flag = '--csh'
        elif self.shell == 'fish':
            regex = \
                re.compile('\A.*set -gx ' +  # noqa: W605
                           '(?P<var>[\S^=]+) ' +  # noqa: W605
                           '(?P<val>[\S]+);\Z')  # noqa: W605
            shell_flag = '--fish'
        else:
            raise RunnerError('Shell %s not supported' % self.shell)

        self._load_compiler_shell(spec, shell_flag, regex)

    def _load_compiler_shell(self, spec, shell_flag, regex):
        args = [
            'load',
            shell_flag,
            spec
        ]

        if not self.dry_run:
            load_cmds = self.exe(*args, output=str).split('\n')

            for cmd in load_cmds:
                env_var = regex.match(cmd)
                if env_var:
                    self.exe.add_default_env(env_var.group('var'),
                                             env_var.group('val'))
        else:
            self._dry_run_print(args)

    def install_compiler(self, spec):
        """
        Ensure a compiler is installed, before using it to install packages
        within an environment.

        This command always executes outside of an environment.

        If it is executed within an environment, then it adds the compiler to
        the list of environment specs. This can cause conflicts if an
        incompatible package is explicitly added to the environment later.

        Also, if it is added to an environment, sometimes it will cause a
        compiler to be installed multiple times with different base compilers.
        """
        active_env = None
        if self.active:
            active_env = self.exe.default_env[self.env_key]
            if self.env_key in self.exe.default_env:
                del self.exe.default_env[self.env_key]

        comp_info_args = []
        if self.compiler_config_dir:
            comp_info_args.extend(['-C', self.env_path])
        comp_info_args.extend(['compiler', 'info', spec])

        try:
            self.exe(*comp_info_args, output=os.devnull, error=os.devnull)
            tty.msg(f'{spec} is already an available compiler')
        except ProcessError:
            args = [
                'install',
                '--reuse',
                spec
            ]

            if not self.dry_run:
                self.exe(*args)
            else:
                self._dry_run_print(args)

            self.load_compiler(spec)

            if not self.dry_run:
                self.exe(*self.compiler_find_args)

                self.compilers.append(spec)

                if self.active:
                    self.exe.add_default_env(self.env_key, active_env)
            else:
                self._dry_run_print(self.compiler_find_args)

    def activate(self):
        """
        Ensure the spack environment is active in subsequent commands.
        """
        if not self.env_path:
            raise NoPathRunnerError('Environment runner has no ' +
                                    'path congfigured')

        self.exe.add_default_env(self.env_key, self.env_path)

        self.env_contents = []

        self.active = True

    def deactivate(self):
        """
        Ensure the spack environment is not active in subsequent commands.
        """
        if not self.env_path:
            raise NoPathRunnerError('Environment runner has no ' +
                                    'path congfigured')

        if self.active and self.env_key in self.exe.default_env.keys():
            del self.exe.default_env[self.env_key]
            self.active = False

    def _check_active(self):
        if not self.env_path:
            raise NoPathRunnerError('Environment runner has no ' +
                                    'path congfigured')

        if not self.active:
            raise NoActiveEnvironmentError('Runner has no active ' +
                                           'environment to work with.')

    def add_spec(self, spec):
        """
        Add a spec to the spack environment.

        This command requires an active spack environment.
        """
        self._check_active()

        if spec not in self.env_contents:
            self.env_contents.append(spec)

    def added_packages(self):
        """
        Return a list of base package names that are added to an environment
        """
        self._check_active()

        args = [
            'find'
        ]

        pkg_names = []

        for pkg in self.exe(*args, output=str).split('\n'):
            match = package_name_regex.match(pkg)
            if match:
                pkg_names.append(match.group('package_name'))

        return pkg_names

    def add_include_file(self, include_file):
        """
        Add an include file to this spack environment.

        This file needs to be a config section supported by spack, otherwise
        spack will error. So, we validate against a list of supported sections.
        """

        file_name = os.path.basename(include_file)
        if file_name in self._allowed_config_files:
            self.includes.append(include_file)

    def apply_configs(self):
        """
        Add all defined configs to the environment
        """

        if self.configs_applied:
            return

        self._check_active()

        config_args = [
            'config',
            'add'
        ]

        for config in self.configs:
            args = config_args.copy()
            args.append(config)
            self.exe(*args)
            if self.dry_run:
                self._dry_run_print(args)

        self.configs_applied = True

    def copy_from_external_env(self, env_name_or_path):
        """
        Copy an external spack environment file into the generated environment.

        env_name_or_path can be either:
         - Name of a named spack environment
         - Path to an external spack environment

         Sets self.concretized if a spack.lock file is found in the env

         Args:
         - env_name_or_path: Name or path to existing spack environment
        """

        self._check_active()

        named_location_args = [
            'location',
            '-e',
            env_name_or_path
        ]

        # If the path doesn't exist, check if it's a named environment
        path = env_name_or_path
        if not os.path.exists(path):
            try:
                path = self.exe(*named_location_args, output=str).strip('\n')
            # If a named environment fails, copy directly from the path
            except ProcessError:
                raise InvalidExternalEnvironment(f'{path} is not a spack environment.')

        found_lock = False

        lock_file = os.path.join(path, 'spack.lock')
        if os.path.exists(lock_file):
            found_lock = True
            shutil.copyfile(lock_file, os.path.join(self.env_path, 'spack.lock'))

        conf_file = os.path.join(path, 'spack.yaml')
        if not os.path.exists(conf_file):
            raise InvalidExternalEnvironment(f'{path} is not a spack environment.')

        shutil.copyfile(conf_file, os.path.join(self.env_path, 'spack.yaml'))

        if self.configs:
            self.apply_configs()

        self.concretized = found_lock

    def generate_env_file(self):
        """
        Generate a spack environment file
        """
        self._check_active()

        env_file = syaml.syaml_dict()
        env_file[spack_namespace] = syaml.syaml_dict()
        env_file[spack_namespace]['concretizer'] = syaml.syaml_dict()
        env_file[spack_namespace]['concretizer']['unify'] = True

        env_file[spack_namespace]['specs'] = syaml.syaml_list()
        env_file[spack_namespace]['specs'].extend(self.env_contents)

        env_file[spack_namespace]['include'] = self.includes

        spack_env_file = os.path.join(self.env_path, 'spack.yaml')
        spack_lock_file = os.path.join(self.env_path, 'spack.lock')

        # Check that a spack.yaml and spack.lock file exist already
        if os.path.exists(spack_env_file) and os.path.exists(spack_lock_file):
            existing_env_mtime = os.path.getmtime(spack_env_file)
            existing_lock_mtime = os.path.getmtime(spack_lock_file)

            # If the lock file was last modified after the yaml file...
            if existing_lock_mtime > existing_env_mtime:
                env_data = syaml.load_config(syaml.dump_config(env_file, default_flow_style=False))
                with open(spack_env_file, 'r') as f:
                    existing_data = syaml.load_config(f)
                gen_env_hash = ramble.util.hashing.hash_json(env_data)
                existing_env_hash = ramble.util.hashing.hash_json(existing_data)
                tty.msg(f' Generated hash: {gen_env_hash}')
                tty.msg(f' Existing hash: {existing_env_hash}')

                # If the yaml hash matches the new generated data hash...
                if gen_env_hash == existing_env_hash:
                    self.concretized = True
                    tty.msg(f'Environment {self.env_path} will not be regenerated.')
                    return

        # Write spack.yaml to environment before concretizing
        with open(os.path.join(self.env_path, 'spack.yaml'), 'w+') as f:
            syaml.dump_config(env_file, f, default_flow_style=False)

        if self.configs:
            self.apply_configs()

    def concretize(self):
        """
        Concretize a spack environment.

        This command requires an active spack environment.
        """
        self._check_active()

        if self.concretized:
            tty.msg(f'Environment {self.env_path} is already concretized. Skipping concretize...')
            return

        concretize_flags = ramble.config.get('config:spack_flags:concretize')

        args = [
            'concretize'
        ]
        args.extend(concretize_flags.split())
        if not self.dry_run:
            self.exe(*args)
        else:
            self._dry_run_print(args)

        self.concretized = True

    def inventory_hash(self):
        """
        Create a hash of the spack.lock file for ramble inventory purposes

        This command requires an active spack environment.
        """
        self._check_active()

        spack_file = os.path.join(self.env_path, 'spack.lock')
        if self.dry_run:
            spack_file = os.path.join(self.env_path, 'spack.yaml')

        spack_hash = ramble.util.hashing.hash_file(spack_file)
        hash_path = os.path.join(self.env_path, 'ramble.hash')
        with open(hash_path, 'w+') as f:
            f.write(spack_hash)
        return spack_hash

    def install(self):
        """
        Install spack packages that have been added to an environment.

        This command requires an active spack environment.
        """
        self._check_active()

        if self.installed:
            tty.msg(f'Environment {self.env_path} is already installed. Skipping installation...')
            return

        install_flags = ramble.config.get('config:spack_flags:install')

        args = [
            'install'
        ]
        args.extend(install_flags.split())
        if not self.dry_run:
            self.exe(*args)
        else:
            self._dry_run_print(args)

        self.installed = True

    def get_package_path(self, package_spec):
        """Return the installation directory for a package"""
        loc_args = ['location', '-i']
        loc_args.extend(package_spec.split())

        name_args = ['find', '--format={name}']
        name_args.extend(package_spec.split())

        if not self.dry_run:
            name = self.exe(*name_args, output=str).strip()
            location = self.exe(*loc_args, output=str).strip()
            return (name, location)
        else:
            self._dry_run_print(name_args)
            self._dry_run_print(loc_args)

            name = os.path.join(package_spec.split()[0])
            location = os.path.join('dry-run', 'path', 'to', package_spec.split()[0])
            return (name, location)

    def mirror_environment(self, mirror_path):
        """Create a spack mirror from the activated environment"""
        self._check_active()

        args = [
            "mirror",
            "create",
            "--all",  # All packages in the environment
            "-D",  # Include dependencies
            "-d",
            mirror_path
        ]

        if not self.dry_run:
            return self.exe(*args, output=str).strip()
        else:
            self._dry_run_print(args)
            return """
  %-4d already present
  %-4d added
  %-4d failed to fetch.""" % (0, 0, 0)

    def _dry_run_print(self, args):
        tty.msg('DRY-RUN: would run %s' % self.exe.command)
        tty.msg('         with args: %s' % args)


class RunnerError(ramble.error.RambleError):
    """Raised when a problem occurs with a spack environment"""


class NoPathRunnerError(ramble.error.RambleError):
    """Raised when a runner is used that does not have a path set"""


class NoActiveEnvironmentError(RunnerError):
    """Raised when an environment command is executed without an active
    environment."""


class InvalidExternalEnvironment(RunnerError):
    """Raised when an invalid external spack environment is passed in"""
