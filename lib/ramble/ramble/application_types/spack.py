# Copyright 2022-2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

import os
import six

import llnl.util.tty as tty

from ramble.language.shared_language import register_builtin
from ramble.application import ApplicationBase, ApplicationError
import ramble.spack_runner
from ramble.keywords import Keywords

header_color = '@*b'
level1_color = '@*g'
plain_format = '@.'


def section_title(s):
    return header_color + s + plain_format


def subsection_title(s):
    return level1_color + s + plain_format


class SpackApplication(ApplicationBase):
    """Specialized class for applications that are installed from spack.

    This class can be used to set up an application that will be installed
    via spack.

    It currently only utilizes phases defined in the base class.
    """

    uses_spack = True
    _spec_groups = [('default_compilers', 'Default Compilers'),
                    ('mpi_libraries', 'MPI Libraries'),
                    ('software_specs', 'Software Specs')]
    _spec_keys = ['spack_spec', 'compiler_spec', 'compiler']

    def __init__(self, file_path):
        super().__init__(file_path)
        self._setup_phases = [
            'create_spack_env',
            'install_compilers',
            'concretize_spack_env',
            'install_software',
            'define_package_paths',
            'get_inputs',
            'make_experiments',
            'write_inventory',
        ]

        self._analyze_phases = ['analyze_experiments']
        self._archive_phases = ['archive_experiments']
        self._mirror_phases = [
            'mirror_inputs',
            'create_spack_env',
            'mirror_software'
        ]

        self.spack_runner = ramble.spack_runner.SpackRunner()
        self.application_class = 'SpackApplication'

    def _long_print(self):
        out_str = super()._long_print()

        if hasattr(self, 'package_manager_configs'):
            out_str.append('\n')
            out_str.append(section_title('Package Manager Configs:\n'))
            for name, config in self.package_manager_configs.items():
                out_str.append(f'\t{name} = {config}\n')

        for group in self._spec_groups:
            if hasattr(self, group[0]):
                out_str.append('\n')
                out_str.append(section_title('%s:\n' % group[1]))
                for name, info in getattr(self, group[0]).items():
                    out_str.append(subsection_title('  %s:\n' % name))
                    for key in self._spec_keys:
                        if key in info and info[key]:
                            out_str.append('    %s = %s\n' % (key,
                                                              info[key].replace('@', '@@')))

        return ''.join(out_str)

    def _install_compilers(self, workspace):
        """Install compilers an application uses"""

        # See if we cached this already, and if so return
        namespace = self.expander.env_namespace
        if not namespace:
            raise ApplicationError('Ramble env_namespace is set to None.')
        spec_name = namespace.split('.')[0]

        cache_tupl = ('spack-compilers', spec_name)
        if workspace.check_cache(cache_tupl):
            tty.debug('{} already in cache.'.format(cache_tupl))
            return
        else:
            workspace.add_to_cache(cache_tupl)

        try:
            self.spack_runner.set_compiler_config_dir(workspace.auxiliary_software_dir)
            self.spack_runner.set_dry_run(workspace.dry_run)

            app_context = self.expander.expand_var('{env_name}')

            for pkg_name in workspace.software_environments.get_env_packages(app_context):
                pkg_spec = workspace.software_environments.get_spec(pkg_name)
                if 'compiler' in pkg_spec:
                    tty.debug(f'Trying to install compiler: {pkg_spec["compiler"]}')
                    comp_spec = workspace.software_environments.get_spec(pkg_spec['compiler'])
                    self.spack_runner.install_compiler(comp_spec['spack_spec'])

        except ramble.spack_runner.RunnerError as e:
            tty.die(e)

    def _create_spack_env(self, workspace):
        """Create the spack environment for this experiment

        Extract all specs this experiment uses, and write the spack environment
        file for it.
        """

        # See if we cached this already, and if so return
        namespace = self.expander.env_namespace
        if not namespace:
            raise ApplicationError('Ramble env_namespace is set to None.')

        cache_tupl = ('spack-env', namespace)
        if workspace.check_cache(cache_tupl):
            tty.debug('{} already in cache.'.format(cache_tupl))
            return
        else:
            workspace.add_to_cache(cache_tupl)

        package_manager_config_dicts = [self.package_manager_configs]
        for mod_inst in self._modifier_instances:
            package_manager_config_dicts.append(mod_inst.package_manager_configs)

        for config_dict in package_manager_config_dicts:
            for _, config in config_dict.items():
                self.spack_runner.add_config(config)

        try:
            self.spack_runner.set_dry_run(workspace.dry_run)
            self.spack_runner.create_env(self.expander.expand_var('{spack_env}'))
            self.spack_runner.activate()

            # Write auxiliary software files into created spack env.
            for name, contents in workspace.all_auxiliary_software_files():
                aux_file_path = self.expander.expand_var(os.path.join('{spack_env}', f'{name}'))
                self.spack_runner.add_include_file(aux_file_path)
                with open(aux_file_path, 'w+') as f:
                    f.write(self.expander.expand_var(contents))

            env_context = self.expander.expand_var('{env_name}')
            external_spack_env = workspace.external_spack_env(env_context)
            if external_spack_env:
                self.spack_runner.copy_from_external_env(external_spack_env)
            else:
                for pkg_name in workspace.software_environments.get_env_packages(env_context):
                    spec_str = workspace.software_environments.get_spec_string(pkg_name)
                    self.spack_runner.add_spec(spec_str)

                self.spack_runner.generate_env_file()

            added_packages = set(self.spack_runner.added_packages())
            for pkg in self.required_packages.keys():
                if pkg not in added_packages:
                    tty.die(f'Software spec {pkg} is not defined '
                            f'in environment {env_context}, but is required '
                            f'to by the {self.name} application '
                            'definition')

            for mod_inst in self._modifier_instances:
                for pkg in mod_inst.required_packages.keys():
                    if pkg not in added_packages:
                        tty.die(f'Software spec {pkg} is not defined '
                                f'in environment {env_context}, but is required '
                                f'to by the {mod_inst.name} modifier '
                                'definition')

            self.spack_runner.deactivate()

        except ramble.spack_runner.RunnerError as e:
            tty.die(e)

    def _concretize_spack_env(self, workspace):
        """Concretize the spack environment for this experiment

        Perform spack's concretize step on the software environment generated
        for  this experiment.
        """

        # See if we cached this already, and if so return
        env_path_or_name = self.expander.expand_var(
            self.expander.expansion_str(Keywords.env_name)
        )

        cache_tupl = ('concretize-env', env_path_or_name)
        if workspace.check_cache(cache_tupl):
            tty.debug('{} already in cache.'.format(cache_tupl))
            return
        else:
            workspace.add_to_cache(cache_tupl)

        try:
            self.spack_runner.set_dry_run(workspace.dry_run)

            self.spack_runner.activate()

            env_concretized = self.spack_runner.concretized

            if not env_concretized:
                self.spack_runner.concretize()

        except ramble.spack_runner.RunnerError as e:
            tty.die(e)

    def _install_software(self, workspace):
        """Install application's software using spack"""

        # See if we cached this already, and if so return
        env_path_or_name = self.expander.expand_var(
            self.expander.expansion_str(Keywords.env_name)
        )

        cache_tupl = ('spack-install', env_path_or_name)
        if workspace.check_cache(cache_tupl):
            tty.debug('{} already in cache.'.format(cache_tupl))
            return
        else:
            workspace.add_to_cache(cache_tupl)

        try:
            self.spack_runner.set_dry_run(workspace.dry_run)
            self.spack_runner.set_env(self.expander.expand_var('{spack_env}'))

            self.spack_runner.activate()
            self.spack_runner.install()
        except ramble.spack_runner.RunnerError as e:
            tty.die(e)

    def _define_package_paths(self, workspace):
        """Define variables containing the path to all spack packages

        For every spack package defined within an application context, define
        a variable that refers to that packages installation location.

        As an example:
        <ramble.yaml>
        spack:
          applications:
            wrfv4:
              wrf:
                base: wrf
                version: 4.2.2

        Would define a variable `wrf` that contains the installation path of
        wrf@4.2.2
        """
        try:
            self.spack_runner.set_dry_run(workspace.dry_run)
            self.spack_runner.set_env(self.expander.expand_var('{spack_env}'))

            self.spack_runner.activate()

            app_context = self.expander.expand_var('{env_name}')

            for pkg_name in \
                    workspace.software_environments.get_env_packages(app_context):
                spec_str = workspace.software_environments.get_spec_string(pkg_name)
                spack_pkg_name, package_path = self.spack_runner.get_package_path(spec_str)
                self.variables[spack_pkg_name] = package_path

        except ramble.spack_runner.RunnerError as e:
            tty.die(e)

    def _mirror_software(self, workspace):
        """Mirror software source for this experiment using spack"""
        import re

        # See if we cached this already, and if so return
        namespace = self.expander.env_namespace
        if not namespace:
            raise ApplicationError('Ramble env_namespace is set to None.')

        cache_tupl = ('spack-mirror', namespace)
        if workspace.check_cache(cache_tupl):
            tty.debug('{} already in cache.'.format(cache_tupl))
            return
        else:
            workspace.add_to_cache(cache_tupl)

        try:
            self.spack_runner.set_dry_run(workspace.dry_run)
            self.spack_runner.set_env(self.expander.expand_var('{spack_env}'))

            self.spack_runner.activate()

            mirror_output = self.spack_runner.mirror_environment(workspace._software_mirror_path)

            present = 0
            added = 0
            failed = 0

            present_regex = re.compile(r'\s+(?P<num>[0-9]+)\s+already present')
            present_match = present_regex.search(mirror_output)
            if present_match:
                present = int(present_match.group('num'))

            added_regex = re.compile(r'\s+(?P<num>[0-9]+)\s+added')
            added_match = added_regex.search(mirror_output)
            if added_match:
                added = int(added_match.group('num'))

            failed_regex = re.compile(r'\s+(?P<num>[0-9]+)\s+failed to fetch.')
            failed_match = failed_regex.search(mirror_output)
            if failed_match:
                failed = int(failed_match.group('num'))

            added_start = len(workspace._software_mirror_stats.new)
            for i in range(added_start, added_start + added):
                workspace._software_mirror_stats.new[i] = i

            present_start = len(workspace._software_mirror_stats.present)
            for i in range(present_start, present_start + present):
                workspace._software_mirror_stats.present[i] = i

            error_start = len(workspace._software_mirror_stats.errors)
            for i in range(error_start, error_start + failed):
                workspace._software_mirror_stats.errors.add(i)

        except ramble.spack_runner.RunnerError as e:
            tty.die(e)

    def _write_inventory(self, workspace):
        """Add software environment information to hash inventory"""

        self.hash_inventory['software'].append(
            {
                'name': self.spack_runner.env_path.replace(workspace.root + os.path.sep, ''),
                'digest': self.spack_runner.inventory_hash()
            }
        )

        super()._write_inventory(workspace)

    def _clean_hash_variables(self, workspace, variables):
        """Perform spack specific cleanup of variables before hashing"""

        self.spack_runner.configure_env(self.expander.expand_var('{spack_env}'))
        self.spack_runner.activate()

        for var in variables:
            if isinstance(variables[var], six.string_types):
                variables[var] = variables[var].replace(
                    '\n'.join(self.spack_runner.generate_source_command()),
                    'spack_source'
                )
                variables[var] = variables[var].replace(
                    '\n'.join(self.spack_runner.generate_activate_command()),
                    'spack_activate'
                )

        self.spack_runner.deactivate()

        super()._clean_hash_variables(workspace, variables)

    register_builtin('spack_source', required=True)
    register_builtin('spack_activate', required=True)
    register_builtin('spack_deactivate', required=False)

    def spack_source(self):
        return self.spack_runner.generate_source_command()

    def spack_activate(self):
        self.spack_runner.configure_env(self.expander.expand_var('{spack_env}'))
        self.spack_runner.activate()
        cmds = self.spack_runner.generate_activate_command()
        self.spack_runner.deactivate()
        return cmds

    def spack_deactivate(self):
        return self.spack_runner.generate_deactivate_command()
