# Copyright 2022-2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.
"""Define base classes for application definitions"""

import os
import stat
import re
import six
import textwrap
import string
import shutil
from typing import List

import llnl.util.filesystem as fs
import llnl.util.tty as tty
import llnl.util.tty.color as color
from llnl.util.tty.colify import colified

import spack.util.executable
import spack.util.spack_json
import spack.util.environment
import spack.util.compression

import ramble.config
import ramble.stage
import ramble.mirror
import ramble.fetch_strategy
import ramble.expander
import ramble.repository
import ramble.modifier
import ramble.util.executable
import ramble.util.colors as rucolor
import ramble.util.hashing

from ramble.keywords import keywords
from ramble.workspace import namespace

from ramble.language.application_language import ApplicationMeta
from ramble.language.shared_language import register_builtin
from ramble.error import RambleError


class ApplicationBase(object, metaclass=ApplicationMeta):
    name = None
    uses_spack = False
    _builtin_name = 'builtin::{name}'
    _exec_prefix_builtin = 'builtin::'
    _mod_prefix_builtin = 'modifier_builtin::'
    _builtin_required_key = 'required'
    _workload_exec_key = 'executables'
    _inventory_file_name = 'ramble_inventory.json'

    #: Lists of strings which contains GitHub usernames of attributes.
    #: Do not include @ here in order not to unnecessarily ping the users.
    maintainers: List[str] = []
    tags: List[str] = []

    def __init__(self, file_path):
        super().__init__()

        self._setup_phases = []
        self._analyze_phases = []
        self._archive_phases = ['archive_experiments']
        self._mirror_phases = ['mirror_inputs']

        self._vars_are_expanded = False
        self.expander = None
        self.variables = None
        self.experiment_set = None
        self.internals = None
        self.is_template = False
        self.chained_experiments = None
        self.chain_order = []
        self.chain_prepend = []
        self.chain_append = []
        self.chain_commands = {}
        self._env_variable_sets = None
        self.modifiers = []
        self._modifier_instances = []
        self._modifier_builtins = {}
        self._input_fetchers = None

        self.hash_inventory = {
            'attributes': [],
            'inputs': [],
            'software': [],
            'templates': [],
        }
        self.experiment_hash = None

        self._file_path = file_path

        self.application_class = 'ApplicationBase'

        self._verbosity = 'short'
        self._inject_required_builtins()

    def copy(self):
        """Deep copy an application instance"""
        new_copy = type(self)(self._file_path)

        new_copy.set_env_variable_sets(self._env_variable_sets.copy())
        new_copy.set_variables(self.variables.copy(), self.experiment_set)
        new_copy.set_internals(self.internals.copy())
        new_copy.set_template(False)
        new_copy.set_chained_experiments(None)

        return new_copy

    def _inject_required_builtins(self):
        required_builtins = []
        for builtin, blt_conf in self.builtins.items():
            if blt_conf[self._builtin_required_key]:
                required_builtins.append(builtin)

        for workload, wl_conf in self.workloads.items():
            if self._workload_exec_key in wl_conf:
                # Insert in reverse order, to make sure they are correctly ordered.
                for builtin in reversed(required_builtins):
                    blt_conf = self.builtins[builtin]
                    if builtin not in wl_conf[self._workload_exec_key]:
                        if blt_conf['injection_method'] == 'prepend':
                            wl_conf[self._workload_exec_key].insert(0, builtin)
                        else:
                            wl_conf[self._workload_exec_key].append(builtin)

    def _inject_required_modifier_builtins(self):
        """Inject builtins defined as required from each modifier into this
        application instance."""
        if not self.modifiers or len(self._modifier_instances) == 0:
            return

        required_prepend_builtins = []
        required_append_builtins = []

        mod_regex = re.compile(ramble.modifier.ModifierBase._mod_builtin_regex +
                               r'(?P<func>.*)')
        for mod_inst in self._modifier_instances:
            for builtin, blt_conf in mod_inst.builtins.items():
                if blt_conf[self._builtin_required_key]:
                    blt_match = mod_regex.match(builtin)

                    # Each builtin should only be added once.
                    added = False

                    if blt_conf['injection_method'] == 'prepend':
                        if builtin not in required_prepend_builtins:
                            required_prepend_builtins.append(builtin)
                            added = True
                    else:  # Append
                        if builtin not in required_append_builtins:
                            required_append_builtins.append(builtin)
                            added = True

                    # Only update if the builtin was added to a list.
                    if added:
                        self._modifier_builtins[builtin] = {
                            'func': getattr(mod_inst,
                                            blt_match.group("func"))
                        }

        for workload, wl_conf in self.workloads.items():
            if self._workload_exec_key in wl_conf:
                # Insert prepend builtins in reverse order, to make sure they
                # are correctly ordered.
                for builtin in reversed(required_prepend_builtins):
                    if builtin not in wl_conf[self._workload_exec_key]:
                        wl_conf[self._workload_exec_key].insert(0, builtin)

                # Append builtins can be inserted in their correct order.
                for builtin in required_append_builtins:
                    if builtin not in wl_conf[self._workload_exec_key]:
                        wl_conf[self._workload_exec_key].append(builtin)

    def _long_print(self):
        out_str = []
        out_str.append(rucolor.section_title('Application: ') + f'{self.name}\n')
        out_str.append('\n')

        out_str.append(rucolor.section_title('Description:\n'))
        if self.__doc__:
            out_str.append(f'\t{self.__doc__}\n')
        else:
            out_str.append('\tNone\n')

        if hasattr(self, 'maintainers'):
            out_str.append('\n')
            out_str.append(rucolor.section_title("Maintainers:\n"))
            out_str.append(colified(self.maintainers, tty=True))
            out_str.append('\n')

        if hasattr(self, 'tags'):
            out_str.append('\n')
            out_str.append(rucolor.section_title('Tags:\n'))
            out_str.append(colified(self.tags, tty=True))
            out_str.append('\n')

        if hasattr(self, '_setup_phases'):
            out_str.append('\n')
            out_str.append(rucolor.section_title('Setup Pipeline Phases:\n'))
            out_str.append(colified(self._setup_phases, tty=True))

        if hasattr(self, '_analyze_phases'):
            out_str.append('\n')
            out_str.append(rucolor.section_title('Analyze Pipeline Phases:\n'))
            out_str.append(colified(self._analyze_phases, tty=True))

        if hasattr(self, 'workloads'):
            out_str.append('\n')
            for wl_name, wl_conf in self.workloads.items():
                out_str.append(rucolor.section_title('Workload:') + f' {wl_name}\n')
                out_str.append('\t' + rucolor.nested_1('Executables: ') +
                               f'{wl_conf["executables"]}\n')
                out_str.append('\t' + rucolor.nested_1('Inputs: ') +
                               f'{wl_conf["inputs"]}\n')

                if wl_name in self.workload_variables:
                    out_str.append(rucolor.nested_1('\tVariables:\n'))
                    for var, conf in self.workload_variables[wl_name].items():
                        indent = '\t\t'

                        out_str.append(rucolor.nested_2(f'{indent}{var}:\n'))
                        out_str.append(f'{indent}\tDescription: {conf["description"]}\n')
                        out_str.append(f'{indent}\tDefault: {conf["default"]}\n')
                        if 'values' in conf:
                            out_str.append(f'{indent}\tSuggested Values: {conf["values"]}\n')

            out_str.append('\n')

        if hasattr(self, 'builtins'):
            out_str.append(rucolor.section_title('Builtin Executables:\n'))
            out_str.append('\t' + colified(self.builtins.keys(), tty=True) + '\n')
        return out_str

    def set_env_variable_sets(self, env_variable_sets):
        """Set internal reference to environment variable sets"""

        self._env_variable_sets = env_variable_sets.copy()

    def set_variables(self, variables, experiment_set):
        """Set internal reference to variables

        Also, create an application specific expander class.
        """

        self.variables = variables
        self.experiment_set = experiment_set
        self.expander = ramble.expander.Expander(self.variables, self.experiment_set)

    def set_internals(self, internals):
        """Set internal reference to application internals
        """

        self.internals = internals

    def set_template(self, is_template):
        """Set if this instance is a template or not"""
        self.is_template = is_template

    def set_chained_experiments(self, chained_experiments):
        """Set chained experiments for this instance"""
        self.chained_experiments = None
        if chained_experiments:
            self.chained_experiments = chained_experiments.copy()

    def set_modifiers(self, modifiers):
        """Set modifiers for this instance"""
        if modifiers:
            self.modifiers = modifiers.copy()

    def get_pipeline_phases(self, pipeline):
        phases = []
        if hasattr(self, '_%s_phases' % pipeline):
            phases = getattr(self, '_%s_phases' % pipeline).copy()
        return phases

    def _short_print(self):
        return [self.name]

    def __str__(self):
        if self._verbosity == 'long':
            return ''.join(self._long_print())
        elif self._verbosity == 'short':
            return ''.join(self._short_print())
        return self.name

    def print_vars(self, header='', vars_to_print=None, indent=''):
        print_vars = vars_to_print
        if not print_vars:
            print_vars = self.variables

        color.cprint(f'{indent}{header}:')
        for var, val in print_vars.items():
            expansion_var = self.expander.expansion_str(var)
            expanded = self.expander.expand_var(expansion_var)
            color.cprint(f'{indent}  {var} = {val} ==> {expanded}'.replace('@', '@@'))

    def print_internals(self, indent=''):
        if not self.internals:
            return

        if namespace.custom_executables in self.internals:
            header = rucolor.nested_4('Custom Executables')
            color.cprint(f'{indent}{header}:')

            for name in self.internals[namespace.custom_executables]:
                color.cprint(f'{indent}  {name}')

        if namespace.executables in self.internals:
            header = rucolor.nested_4('Executable Order')
            color.cprint(f'{indent}{header}: {str(self.internals[namespace.executables])}')

    def print_chain_order(self, indent=''):
        if not self.chain_order:
            return

        header = rucolor.nested_4('Experiment Chain')
        color.cprint(f'{indent}{header}:')
        for exp in self.chain_order:
            color.cprint(f'{indent}- {exp}')

    def format_doc(self, **kwargs):
        """Wrap doc string at 72 characters and format nicely"""
        indent = kwargs.get('indent', 0)

        if not self.__doc__:
            return ""

        doc = re.sub(r'\s+', ' ', self.__doc__)
        lines = textwrap.wrap(doc, 72)
        results = six.StringIO()
        for line in lines:
            results.write((" " * indent) + line + "\n")
        return results.getvalue()

    # Phase execution helpers
    def run_phase(self, phase, workspace):
        """Run a phase, by getting its function pointer"""
        self.build_modifier_instances()
        self._inject_required_modifier_builtins()
        self.add_expand_vars(workspace)
        if self.is_template:
            tty.debug(f'{self.name} is a template. Skipping phases')
            return

        if hasattr(self, f'_{phase}'):
            tty.verbose('    Executing phase ' + phase)
            phase_func = getattr(self, f'_{phase}')
            phase_func(workspace)

    def _get_env_set_commands(self, var_conf, var_set, shell='sh'):
        env_mods = spack.util.environment.EnvironmentModifications()
        for var, val in var_conf.items():
            var_set.add(var)
            env_mods.set(var, val)

        env_cmds_arr = env_mods.shell_modifications(shell=shell, explicit=True)

        return (env_cmds_arr.split('\n'), var_set)

    def _get_env_unset_commands(self, var_conf, var_set, shell='sh'):
        env_mods = spack.util.environment.EnvironmentModifications()
        for var in var_conf:
            if var in var_set:
                var_set.remove(var)
            env_mods.unset(var)

        env_cmds_arr = env_mods.shell_modifications(shell=shell, explicit=True)

        return (env_cmds_arr.split('\n'), var_set)

    def _get_env_append_commands(self, var_conf, var_set, shell='sh'):
        env_mods = spack.util.environment.EnvironmentModifications()

        append_funcs = {
            'vars': env_mods.append_flags,
            'paths': env_mods.append_path,
        }

        var_set_orig = var_set.copy()

        for append_group in var_conf:
            sep = ' '
            if 'var-separator' in append_group:
                sep = append_group['var-separator']

            for group in append_funcs.keys():
                if group in append_group.keys():
                    for var, val in append_group[group].items():
                        if var not in var_set:
                            env_mods.set(var, '${%s}' % var)
                            var_set.add(var)
                        append_funcs[group](var, val, sep=sep)

        env_cmds_arr = env_mods.shell_modifications(shell=shell, explicit=True)

        return (env_cmds_arr.split('\n'), var_set_orig)

    def _get_env_prepend_commands(self, var_conf, var_set, shell='sh'):
        env_mods = spack.util.environment.EnvironmentModifications()

        prepend_funcs = {
            'paths': env_mods.prepend_path,
        }

        var_set_orig = var_set.copy()

        for prepend_group in var_conf:
            for group in prepend_group.keys():
                for var, val in prepend_group[group].items():
                    if var not in var_set:
                        env_mods.set(var, '${%s}' % var)
                        var_set.add(var)
                    prepend_funcs[group](var, val)

        env_cmds_arr = env_mods.shell_modifications(shell=shell, explicit=True)

        return (env_cmds_arr.split('\n'), var_set_orig)

    def create_experiment_chain(self, workspace):
        """Create the necessary chained experiments for this instance

        This method determines which experiments need to be chained, grabs the
        base instance from the experiment set, creates a copy of it (with a
        unique name), injects the copy back into the experiment set,
        and builds an internal mapping from unique name to the chaining definition.
        """

        if not self.chained_experiments or self.is_template:
            return

        # Build initial stack. Uses a reversal of the current instance's
        # chained experiments
        parent_namespace = self.expander.experiment_namespace
        classes_in_stack = set([self])
        chain_idx = 0
        chain_stack = []
        for exp in reversed(self.chained_experiments):
            for exp_name in self.experiment_set.search_primary_experiments(exp['name']):
                child_inst = self.experiment_set.get_experiment(exp_name)

                if child_inst in classes_in_stack:
                    raise ChainCycleDetectedError('Cycle detected in experiment chain:\n' +
                                                  f'    Primary experiment {parent_namespace}\n' +
                                                  f'    Chained expeirment name: {exp_name}\n' +
                                                  f'    Chain definition: {str(exp)}')
                chain_stack.append((exp_name, exp))

        parent_run_dir = self.expander.expand_var(
            self.expander.expansion_str(keywords.experiment_run_dir)
        )

        # Continue until the stack is empty
        while len(chain_stack) > 0:
            cur_exp_name = chain_stack[-1][0]
            cur_exp_def = chain_stack[-1][1]

            # Perform basic validation on the chained experiment definition
            if 'name' not in cur_exp_def:
                raise InvalidChainError('Invalid experiment chain defined:\n' +
                                        f'    Primary experiment {parent_namespace}\n' +
                                        f'    Chain definition: {str(exp)}\n' +
                                        '    "name" keyword must be defined')

            if 'order' in cur_exp_def:
                possible_orders = ['after_chain', 'after_root', 'before_chain', 'before_root']
                if cur_exp_def['order'] not in possible_orders:
                    raise InvalidChainError('Invalid experiment chain defined:\n' +
                                            f'    Primary experiment {parent_namespace}\n' +
                                            f'    Chain definition: {str(exp)}\n' +
                                            '    Optional keyword "order" must ' +
                                            f'be one of {str(possible_orders)}\n')

            if 'command' not in cur_exp_def.keys():
                raise InvalidChainError('Invalid experiment chain defined:\n' +
                                        f'    Primary experiment {parent_namespace}\n' +
                                        f'    Chain definition: {str(exp)}\n' +
                                        '    "command" keyword must be defined')

            if 'variables' in cur_exp_def:
                if not isinstance(cur_exp_def['variables'], dict):
                    raise InvalidChainError('Invalid experiment chain defined:\n' +
                                            f'    Primary experiment {parent_namespace}\n' +
                                            f'    Chain definition: {str(exp)}\n' +
                                            '    Optional keyword "variables" ' +
                                            'must be a dictionary')

            base_inst = self.experiment_set.get_experiment(cur_exp_name)
            if base_inst in classes_in_stack:
                chain_stack.pop()
                classes_in_stack.remove(base_inst)

                order = 'after_root'
                if 'order' in cur_exp_def:
                    order = cur_exp_def['order']

                chained_name = f'{chain_idx}.{cur_exp_name}'
                new_name = f'{parent_namespace}.chain.{chained_name}'

                new_run_dir = os.path.join(parent_run_dir,
                                           namespace.chained_experiments, chained_name)

                if order == 'before_chain':
                    self.chain_prepend.insert(0, new_name)
                elif order == 'before_root':
                    self.chain_prepend.append(new_name)
                elif order == 'after_root':
                    self.chain_append.insert(0, new_name)
                elif order == 'after_chain':
                    self.chain_append.append(new_name)
                self.chain_commands[new_name] = cur_exp_def[keywords.command]

                # Skip editing the new instance if the base_inst doesn't work
                # This happens if the originating command is `workspace info`
                # The printing experiment set doesn't have access to all
                # of the experiment, so the base_inst command above
                # doesn't get an application instance.
                if base_inst:
                    new_inst = base_inst.copy()

                    if namespace.variables in cur_exp_def:
                        for var, val in cur_exp_def[namespace.variables].items():
                            new_inst.variables[var] = val

                    new_inst.expander._experiment_namespace = new_name
                    new_inst.variables[keywords.experiment_run_dir] = new_run_dir
                    new_inst.variables[keywords.experiment_name] = new_name

                    # Expand the chained experiment vars, so we can build the execution command
                    new_inst.add_expand_vars(workspace)
                    chain_cmd = new_inst.expander.expand_var(cur_exp_def[keywords.command])
                    self.chain_commands[new_name] = chain_cmd
                    cur_exp_def[keywords.command] = chain_cmd
                    self.experiment_set.add_chained_experiment(new_name, new_inst)

                chain_idx += 1
            else:
                # Avoid cycles, from children
                if base_inst in classes_in_stack:
                    chain_stack.pop()
                else:
                    if base_inst.chained_experiments:
                        for exp in reversed(base_inst.chained_experiments):
                            for exp_name in \
                                    self.experiment_set.search_primary_experiments(exp['name']):
                                child_inst = self.experiment_set.get_experiment(exp_name)
                                if child_inst in classes_in_stack:
                                    raise ChainCycleDetectedError('Cycle detected in ' +
                                                                  'experiment chain:\n' +
                                                                  '    Primary experiment ' +
                                                                  f'{parent_namespace}\n' +
                                                                  '    Chained expeirment name: ' +
                                                                  f'{cur_exp_name}\n' +
                                                                  '    Chain definition: ' +
                                                                  f'{str(cur_exp_def)}')

                                chain_stack.append((exp_name, exp))
                    classes_in_stack.add(base_inst)

        # Create the final chain order
        for exp in self.chain_prepend:
            self.chain_order.append(exp)
        self.chain_order.append(self.expander.experiment_namespace)
        for exp in self.chain_append:
            self.chain_order.append(exp)

        # Inject the chain order into the children experiments
        for exp in self.chain_prepend:
            exp_inst = self.experiment_set.get_experiment(exp)
            if exp_inst:
                exp_inst.chain_order = self.chain_order.copy()

        for exp in self.chain_append:
            exp_inst = self.experiment_set.get_experiment(exp)
            if exp_inst:
                exp_inst.chain_order = self.chain_order.copy()

    def build_modifier_instances(self):
        """Built a map of modifier names to modifier instances needed for this
           application instance
        """

        if not self.modifiers:
            return

        mod_type = ramble.repository.ObjectTypes.modifiers

        for mod in self.modifiers:
            mod_inst = ramble.repository.get(mod['name'], mod_type).copy()

            if 'on_executable' in mod:
                mod_inst.set_on_executables(mod['on_executable'])
            else:
                mod_inst.set_on_executables(None)

            if 'mode' in mod:
                mod_inst.set_usage_mode(mod['mode'])
            else:
                mod_inst.set_usage_mode(None)

            self._modifier_instances.append(mod_inst)

    def _get_executables(self):
        """Return executables for add_expand_vars"""

        executables = self.workloads[self.expander.workload_name]['executables']

        # Use yaml defined executable order, if defined
        if namespace.executables in self.internals:
            executables = self.internals[namespace.executables]

        # Define custom executables
        if namespace.custom_executables in self.internals.keys():
            for name, conf in self.internals[namespace.custom_executables].items():
                self.executables[name] = ramble.util.executable.CommandExecutable(
                    name=name,
                    **conf
                )

        return executables

    def _set_input_path(self):
        """Put input_path into self.variables[input_file] for add_expand_vars"""
        self._inputs_and_fetchers(self.expander.workload_name)

        for input_file, input_conf in self._input_fetchers.items():
            input_vars = {keywords.input_name: input_conf['input_name']}
            if not input_conf['expand']:
                input_vars[keywords.input_name] = input_file
            input_path = os.path.join(self.expander.workload_input_dir,
                                      self.expander.expand_var(input_conf['target_dir'],
                                                               extra_vars=input_vars))
            self.variables[input_conf['input_name']] = input_path

    def _set_default_experiment_variables(self):
        """Set default experiment variables (for add_expand_vars),
        if they haven't been set already"""
        # Set default experiment variables, if they haven't been set already
        if self.expander.workload_name in self.workload_variables:
            wl_vars = self.workload_variables[self.expander.workload_name]

            for var, conf in wl_vars.items():
                if var not in self.variables.keys():
                    self.variables[var] = conf['default']

    def _inject_commands(self, executables):
        """For add_expand_vars, inject all commands"""
        command = []

        # Inject all prepended chained experiments
        for chained_exp in self.chain_prepend:
            command.append(self.chain_commands[chained_exp])

        # ensure all log files are purged and set up
        logs = set()
        builtin_regex = re.compile(r'%s(?P<func>.*)' % self._exec_prefix_builtin)
        modifier_regex = re.compile(ramble.modifier.ModifierBase._mod_prefix_builtin +
                                    r'(?P<func>.*)')
        for executable in executables:
            if not builtin_regex.search(executable) and \
                    not modifier_regex.search(executable):
                command_config = self.executables[executable]
                if command_config.redirect:
                    logs.add(command_config.redirect)

        for log in logs:
            command.append('rm -f "%s"' % log)
            command.append('touch "%s"' % log)

        for executable in executables:
            builtin_match = builtin_regex.match(executable)

            exec_vars = {'executable_name': executable}

            for mod in self._modifier_instances:
                if mod.applies_to_executable(executable):
                    exec_vars.update(mod.modded_variables(self))

            if builtin_match:
                # Process builtin executables

                # Get internal method:
                func_name = f'{builtin_match.group("func")}'
                func = getattr(self, func_name)
                func_cmds = func()
                for cmd in func_cmds:
                    command.append(self.expander.expand_var(cmd, exec_vars))
            elif executable in self._modifier_builtins.keys():
                builtin_def = self._modifier_builtins[executable]
                func = builtin_def['func']
                func_cmds = func()
                for cmd in func_cmds:
                    command.append(self.expander.expand_var(cmd, exec_vars))
            else:
                # Process directive defined executables
                base_command = self.executables[executable].copy()
                pre_commands = []
                post_commands = []

                for mod in self._modifier_instances:
                    if mod.applies_to_executable(executable):
                        pre_cmd, post_cmd = mod.apply_executable_modifiers(executable,
                                                                           base_command,
                                                                           app_inst=self)
                        pre_commands.extend(pre_cmd)
                        post_commands.extend(post_cmd)

                command_configs = pre_commands.copy()
                command_configs.append(base_command)
                command_configs.extend(post_commands)

                for cmd_conf in command_configs:
                    mpi_cmd = ''
                    if cmd_conf.mpi:
                        mpi_cmd = ' ' + self.expander.expand_var('{mpi_command}', exec_vars) + ' '

                    redirect = ''
                    if cmd_conf.redirect:
                        out_log = self.expander.expand_var(cmd_conf.redirect, exec_vars)
                        output_operator = cmd_conf.output_capture
                        redirect = f' {output_operator} "{out_log}"'

                    for part in cmd_conf.template:
                        command_part = f'{mpi_cmd}{part}{redirect}'
                        command.append(self.expander.expand_var(command_part, exec_vars))

        # Inject all appended chained experiments
        for chained_exp in self.chain_append:
            command.append(self.chain_commands[chained_exp])

        self.variables['command'] = '\n'.join(command)

    def _derive_variables_for_template_path(self, workspace):
        """Define variables for template paths (for add_expand_vars)"""
        for template_name, _ in workspace.all_templates():
            expand_path = os.path.join(
                self.expander.expand_var(f'{{experiment_run_dir}}'),  # noqa: F541
                template_name)
            self.variables[template_name] = expand_path

    def _validate_experiment(self):
        """Perform validation of an experiment before performing actions with it

        This function is an entry point to validate various aspects of an
        experiment definition before it is used. It is expected to raise errors
        when validation fails.
        """
        if self.expander.workload_name not in self.workloads:
            raise ApplicationError(f'Workload {self.expander.workload_name} is not defined '
                                   f'as a workload of application {self.name}.')

    def add_expand_vars(self, workspace):
        """Add application specific expansion variables

        Applications require several variables to be defined to function properly.
        This method defines these variables, including:
        - command: set to the commands needed to execute the experiment
        - spack_setup: set to an empty string, so spack applications can override this
        """
        if not self._vars_are_expanded:
            self._validate_experiment()
            executables = self._get_executables()
            self._set_default_experiment_variables()
            self._set_input_path()
            self._inject_commands(executables)
            # ---------------------------------------------------------------------------------
            # TODO (dwj): Remove this after we validate that 'spack_setup' is not in templates.
            #             this is no longer needed, as spack was converted to builtins.
            self.variables['spack_setup'] = ''
            # ---------------------------------------------------------------------------------
            self._derive_variables_for_template_path(workspace)
            self._vars_are_expanded = True

    def _inputs_and_fetchers(self, workload=None):
        """Extract all inputs for a given workload

        Take a workload name and extract all inputs for the workload.
        If the workload is set to None, extract all inputs for all workloads.
        """

        if self._input_fetchers is not None:
            return

        workload_names = [workload] if workload else self.workloads.keys()

        inputs = {}
        for workload_name in workload_names:
            workload = self.workloads[workload_name]

            for input_file in workload['inputs']:
                input_conf = self.inputs[input_file].copy()

                # Expand input value as it may be a var
                expanded_url = self.expander.expand_var(input_conf['url'])
                input_conf['url'] = expanded_url

                fetcher = ramble.fetch_strategy.URLFetchStrategy(**input_conf)

                file_name = os.path.basename(input_conf['url'])
                if not fetcher.extension:
                    fetcher.extension = spack.util.compression.extension(file_name)

                file_name = file_name.replace(f'.{fetcher.extension}', '')

                namespace = f'{self.name}.{workload_name}'

                inputs[file_name] = {'fetcher': fetcher,
                                     'namespace': namespace,
                                     'target_dir': input_conf['target_dir'],
                                     'extension': fetcher.extension,
                                     'input_name': input_file,
                                     'expand': input_conf['expand']
                                     }
        self._input_fetchers = inputs

    def _mirror_inputs(self, workspace):
        """Mirror application inputs

        Perform mirroring of inputs within this application class.
        """
        self._inputs_and_fetchers(self.expander.workload_name)

        for input_file, input_conf in self._input_fetchers.items():
            mirror_paths = ramble.mirror.mirror_archive_paths(
                input_conf['fetcher'], os.path.join(self.name, input_file))
            fetch_dir = os.path.join(workspace._input_mirror_path, self.name)
            fs.mkdirp(fetch_dir)
            stage = ramble.stage.InputStage(input_conf['fetcher'], name=input_conf['namespace'],
                                            path=fetch_dir, mirror_paths=mirror_paths, lock=False)

            stage.cache_mirror(workspace._input_mirror_cache, workspace._input_mirror_stats)

    def _get_inputs(self, workspace):
        """Download application inputs

        Download application inputs into the proper directory within the workspace.
        """
        workload_namespace = self.expander.workload_namespace

        self._inputs_and_fetchers(self.expander.workload_name)

        for input_file, input_conf in self._input_fetchers.items():
            if not workspace.dry_run:
                input_vars = {keywords.input_name: input_conf['input_name']}
                input_namespace = workload_namespace + '.' + input_file
                input_path = self.expander.expand_var(input_conf['target_dir'],
                                                      extra_vars=input_vars)
                input_tuple = ('input-file', input_path)

                # Skip inputs that have already been cached
                if workspace.check_cache(input_tuple):
                    continue

                mirror_paths = ramble.mirror.mirror_archive_paths(
                    input_conf['fetcher'], os.path.join(self.name, input_file))

                with ramble.stage.InputStage(input_conf['fetcher'], name=input_namespace,
                                             path=self.expander.workload_input_dir,
                                             mirror_paths=mirror_paths) \
                        as stage:
                    stage.set_subdir(input_path)
                    stage.fetch()
                    if input_conf['fetcher'].digest:
                        stage.check()
                    stage.cache_local()

                    if input_conf['expand']:
                        try:
                            stage.expand_archive()
                        except spack.util.executable.ProcessError:
                            pass

                workspace.add_to_cache(input_tuple)
            else:
                tty.msg('DRY-RUN: Would download %s' % input_conf['fetcher'].url)

    def _make_experiments(self, workspace):
        """Create experiment directories

        Create the experiment this application encapsulates. This includes
        creating the experiment run directory, rendering the necessary
        templates, and injecting the experiment into the workspace all
        experiments file.
        """
        experiment_run_dir = self.expander.experiment_run_dir
        fs.mkdirp(experiment_run_dir)

        for template_name, template_conf in workspace.all_templates():
            expand_path = os.path.join(experiment_run_dir, template_name)

            with open(expand_path, 'w+') as f:
                f.write(self.expander.expand_var(template_conf['contents']))
            os.chmod(expand_path, stat.S_IRWXU | stat.S_IRWXG
                     | stat.S_IROTH | stat.S_IXOTH)

        experiment_script = workspace.experiments_script
        experiment_script.write(self.expander.expand_var('{batch_submit}\n'))

    def _clean_hash_variables(self, workspace, variables):
        """Cleanup variables to hash before computing the hash

        Perform some general cleanup operations on variables
        before hashing, to help give useful hashes.
        """

        # Purge workspace name, as this shouldn't affect the experiments
        if 'workspace_name' in variables:
            del variables['workspace_name']

        # Remove the workspace path from variable definitions before hashing
        for var in variables:
            if isinstance(variables[var], six.string_types):
                variables[var] = variables[var].replace(workspace.root + os.path.sep, '')

    def _write_inventory(self, workspace):
        """Build and write an inventory of an experiment

        Write an inventory file describing all of the contents of this
        experiment.
        """

        experiment_run_dir = self.expander.experiment_run_dir
        inventory_file = os.path.join(experiment_run_dir, self._inventory_file_name)

        # Clean up variables before hashing
        vars_to_hash = self.variables.copy()
        self._clean_hash_variables(workspace, vars_to_hash)

        # Build inventory of attributes
        attributes_to_hash = [
            ('variables', vars_to_hash),
            ('modifiers', self.modifiers),
            ('chained_experiments', self.chained_experiments),
            ('internals', self.internals),
            ('env_vars', self._env_variable_sets),
        ]

        for attr, attr_dict in attributes_to_hash:
            self.hash_inventory['attributes'].append(
                {
                    'name': attr,
                    'digest': ramble.util.hashing.hash_json(attr_dict),
                }
            )

        # Build inventory of workspace templates
        for template_name, template_conf in workspace.all_templates():
            self.hash_inventory['templates'].append(
                {
                    'name': template_name,
                    'digest': template_conf['digest'],
                }
            )

        # Build inventory of inputs
        self._inputs_and_fetchers(self.expander.workload_name)

        for input_file, input_conf in self._input_fetchers.items():
            if input_conf['fetcher'].digest:
                self.hash_inventory['inputs'].append(
                    {
                        'name': input_conf['input_name'],
                        'digest': input_conf['fetcher'].digest
                    }
                )
            else:
                self.hash_inventory['inputs'].append(
                    {
                        'name': input_conf['input_name'],
                        'digest': ramble.util.hashing.hash_string(input_conf['fetcher'].url),
                    }
                )

        with open(inventory_file, 'w+') as f:
            spack.util.spack_json.dump(self.hash_inventory, f)

        self.experiment_hash = ramble.util.hashing.hash_file(inventory_file)

    def _archive_experiments(self, workspace):
        """Archive an experiment directory

        Perform the archiving action on an experiment.
        This includes capturing:
        - Rendered templates within the experiment directory
        - All files that contain a figure of merit or success criteria
        - Any files that match an archive pattern
        """
        import glob
        experiment_run_dir = self.expander.experiment_run_dir
        ws_archive_dir = workspace.latest_archive_path

        archive_experiment_dir = experiment_run_dir.replace(workspace.root,
                                                            ws_archive_dir)

        fs.mkdirp(archive_experiment_dir)

        # Copy all of the templates to the archive directory
        for template_name, _ in workspace.all_templates():
            src = os.path.join(experiment_run_dir, template_name)
            if os.path.exists(src):
                shutil.copy(src, archive_experiment_dir)

        # Copy all figure of merit files
        criteria_list = workspace.success_list
        analysis_files, _, _ = self._analysis_dicts(criteria_list)
        for file, file_conf in analysis_files.items():
            if os.path.exists(file):
                shutil.copy(file, archive_experiment_dir)

        # Copy all archive patterns
        archive_patterns = set(self.archive_patterns.keys())
        for mod in self._modifier_instances:
            for pattern in mod.archive_patterns.keys():
                archive_patterns.add(pattern)

        for pattern in archive_patterns:
            exp_pattern = self.expander.expand_var(pattern)
            for file in glob.glob(exp_pattern):
                shutil.copy(file, archive_experiment_dir)

    def _analyze_experiments(self, workspace):
        """Perform experiment analysis.

        This method will build up the fom_values dictionary. Its structure is:

        fom_values[context][fom]

        A fom can show up in any number of explicit contexts (including zero).
        If the number of explicit contexts is zero, the fom is associated with
        the default '(null)' context.

        Success is determined at analysis time as well. This happens by checking if:
         - At least one FOM is extracted
         AND
         - Any defined success criteria pass

        Success criteria are defined within the application.py, but can also be
        injected in a workspace config.
        """

        def format_context(context_match, context_format):

            keywords = {}
            if isinstance(context_format, six.string_types):
                for group in string.Formatter().parse(context_format):
                    if group[1]:
                        keywords[group[1]] = context_match[group[1]]

            context_string = context_format.replace('{', '').replace('}', '') \
                + ' = ' + context_format.format(**keywords)
            return context_string

        fom_values = {}

        criteria_list = workspace.success_list

        files, contexts, foms = self._analysis_dicts(criteria_list)

        # Iterate over files. We already know they exist
        for file, file_conf in files.items():

            # Start with no active contexts in a file.
            active_contexts = {}
            tty.debug('Reading log file: %s' % file)

            with open(file, 'r') as f:
                for line in f.readlines():

                    for criteria in file_conf['success_criteria']:
                        tty.debug('Looking for criteria %s' % criteria)
                        criteria_obj = criteria_list.find_criteria(criteria)
                        if criteria_obj.passed(line, self):
                            criteria_obj.mark_found()

                    for context in file_conf['contexts']:
                        context_conf = contexts[context]
                        context_match = context_conf['regex'].match(line)

                        if context_match:
                            context_name = \
                                format_context(context_match,
                                               context_conf['format'])
                            tty.debug('Line was: %s' % line)
                            tty.debug(' Context match %s -- %s' %
                                      (context, context_name))

                            active_contexts[context] = context_name

                            if context_name not in fom_values:
                                fom_values[context_name] = {}

                    for fom in file_conf['foms']:
                        fom_conf = foms[fom]
                        fom_match = fom_conf['regex'].match(line)

                        if fom_match:
                            fom_vars = {}
                            for k, v in fom_match.groupdict().items():
                                fom_vars[k] = v
                            fom_name = self.expander.expand_var(fom, extra_vars=fom_vars)

                            if fom_conf['group'] in fom_conf['regex'].groupindex:
                                tty.debug(' --- Matched fom %s' % fom_name)
                                fom_contexts = []
                                if fom_conf['contexts']:
                                    for context in fom_conf['contexts']:
                                        context_name = active_contexts[context] \
                                            if context in active_contexts \
                                            else 'null'
                                        fom_contexts.append(context_name)
                                else:
                                    fom_contexts.append('null')

                                for context in fom_contexts:
                                    if context not in fom_values:
                                        fom_values[context] = {}
                                    fom_val = fom_match.group(fom_conf['group'])
                                    fom_values[context][fom_name] = {
                                        'value': fom_val,
                                        'units': fom_conf['units'],
                                        'origin': fom_conf['origin'],
                                        'origin_type': fom_conf['origin_type']
                                    }

        # Test all non-file based success criteria
        for criteria_obj in criteria_list.all_criteria():
            if criteria_obj.file is None:
                if criteria_obj.passed(app_inst=self, fom_values=fom_values):
                    criteria_obj.mark_found()

        exp_ns = self.expander.experiment_namespace
        results = {'name': exp_ns}

        success = True if fom_values else False
        success = success and criteria_list.passed()

        tty.debug('fom_vals = %s' % fom_values)
        results['EXPERIMENT_CHAIN'] = self.chain_order.copy()
        if success:
            results['RAMBLE_STATUS'] = 'SUCCESS'
        else:
            results['RAMBLE_STATUS'] = 'FAILED'

        if success or workspace.always_print_foms:
            results['RAMBLE_VARIABLES'] = {}
            results['RAMBLE_RAW_VARIABLES'] = {}
            for var, val in self.variables.items():
                results['RAMBLE_RAW_VARIABLES'][var] = val
                results['RAMBLE_VARIABLES'][var] = self.expander.expand_var(val)
            results['CONTEXTS'] = []

            for context, fom_map in fom_values.items():
                context_map = {'name': context, 'foms': []}

                for fom_name, fom in fom_map.items():
                    fom_copy = fom.copy()
                    fom_copy['name'] = fom_name
                    context_map['foms'].append(fom_copy)

                results['CONTEXTS'].append(context_map)

        workspace.append_result(results)

    def _new_file_dict(self):
        """Create a dictionary to represent a new log file"""
        return {
            'success_criteria': [],
            'contexts': [],
            'foms': []
        }

    def _analysis_dicts(self, criteria_list):
        """Extract files that need to be analyzed.

        Process figures_of_merit, and return the manipulated dictionaries
        to allow them to be extracted.

        Additionally, ensure the success criteria list is complete.

        Returns:
            files (dict): All files that need to be processed
            contexts (dict): Any contexts that have been defined
            foms (dict): All figures of merit that need to be extracted
        """

        files = {}
        contexts = {}
        foms = {}

        # Add the application defined criteria
        criteria_list.flush_scope('application_definition')

        success_lists = [
            ('application_definition', self.success_criteria),
        ]

        for mod in self._modifier_instances:
            success_lists.append(('modifier_definition', mod.success_criteria))

        for success_scope, success_list in success_lists:
            for criteria, conf in success_list.items():
                if conf['mode'] == 'string':
                    criteria_list.add_criteria(success_scope, criteria,
                                               conf['mode'], re.compile(conf['match']),
                                               conf['file'])

        criteria_list.add_criteria(scope='application_definition',
                                   name='_application_function',
                                   mode='application_function')

        # Extract file paths for all criteria
        for criteria in criteria_list.all_criteria():
            log_path = self.expander.expand_var(criteria.file)
            if log_path not in files and os.path.exists(log_path):
                files[log_path] = self._new_file_dict()

            if log_path in files:
                files[log_path]['success_criteria'].append(criteria.name)

        # Remap fom / context / file data
        # Could push this into the language features in the future
        fom_definitions = self.figures_of_merit.copy()
        for fom, fom_def in fom_definitions.items():
            fom_def['origin'] = self.name
            fom_def['origin_type'] = 'application'

        fom_contexts = self.figure_of_merit_contexts.copy()
        for mod in self._modifier_instances:
            fom_contexts.update(mod.figure_of_merit_contexts)

            mod_vars = mod.modded_variables(self)

            for fom, fom_def in mod.figures_of_merit.items():
                fom_definitions[fom] = {'origin': f'{mod}', 'origin_type': 'modifier'}
                for attr in fom_def.keys():
                    if isinstance(fom_def[attr], list):
                        fom_definitions[fom][attr] = fom_def[attr].copy()
                    else:
                        fom_definitions[fom][attr] = self.expander.expand_var(fom_def[attr],
                                                                              mod_vars)

        for fom, conf in fom_definitions.items():
            log_path = self.expander.expand_var(conf['log_file'])
            if log_path not in files and os.path.exists(log_path):
                files[log_path] = self._new_file_dict()

            if log_path in files:
                tty.debug('Log = %s' % log_path)
                tty.debug('Conf = %s' % conf)
                if conf['contexts']:
                    files[log_path]['contexts'].extend(conf['contexts'])
                files[log_path]['foms'].append(fom)

            foms[fom] = {
                'regex': re.compile(r'%s' % conf['regex']),
                'contexts': [],
                'group': conf['group_name'],
                'units': conf['units'],
                'origin': conf['origin'],
                'origin_type': conf['origin_type']
            }
            if conf['contexts']:
                foms[fom]['contexts'].extend(conf['contexts'])

            if conf['contexts']:
                for context in conf['contexts']:
                    regex_str = \
                        fom_contexts[context]['regex']
                    format_str = \
                        fom_contexts[context]['output_format']
                    contexts[context] = {
                        'regex': re.compile(r'%s' % regex_str),
                        'format': format_str
                    }

            if conf['contexts']:
                for context in conf['contexts']:
                    regex_str = \
                        fom_contexts[context]['regex']
                    format_str = \
                        fom_contexts[context]['output_format']
                    contexts[context] = {
                        'regex': re.compile(r'%s' % regex_str),
                        'format': format_str
                    }
        return files, contexts, foms

    register_builtin('env_vars', required=True)

    def env_vars(self):
        command = []
        # ensure license variables are set / modified
        # Process one scope at a time, to ensure
        # highest-precedence scopes are processed last
        config_scopes = ramble.config.scopes()
        shell = ramble.config.get('config:shell')
        var_set = set()

        action_funcs = {
            'set': self._get_env_set_commands,
            'unset': self._get_env_unset_commands,
            'append': self._get_env_append_commands,
            'prepend': self._get_env_prepend_commands
        }

        for scope in config_scopes:
            license_conf = ramble.config.config.get_config('licenses',
                                                           scope=scope)
            if license_conf:
                app_licenses = license_conf[self.name] if self.name \
                    in license_conf else {}

                for action, conf in app_licenses.items():
                    (env_cmds, var_set) = action_funcs[action](conf,
                                                               var_set,
                                                               shell=shell)

                    for cmd in env_cmds:
                        if cmd:
                            command.append(cmd)

        # Process environment variable actions
        for env_var_set in self._env_variable_sets:
            for action, conf in env_var_set.items():
                (env_cmds, _) = action_funcs[action](conf,
                                                     set(),
                                                     shell=shell)

                for cmd in env_cmds:
                    if cmd:
                        command.append(cmd)

        for mod_inst in self._modifier_instances:
            for action, conf in mod_inst.all_env_var_modifications():
                (env_cmds, _) = action_funcs[action](conf,
                                                     set(),
                                                     shell=shell)

                for cmd in env_cmds:
                    if cmd:
                        command.append(cmd)

        return command

    def evaluate_success(self):
        """Hook for applications to evaluate custom success criteria

        Expected to perform analysis and return either true or false.
        """

        return True


class ApplicationError(RambleError):
    """
    Exception that is raised by applications
    """


class ChainCycleDetectedError(ApplicationError):
    """
    Exception raised when a cycle is detected in a defined experiment chain
    """


class InvalidChainError(ApplicationError):
    """
    Exception raised when a invalid chained experiment is defined
    """
