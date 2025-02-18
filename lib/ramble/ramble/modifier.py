# Copyright 2022-2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.
"""Define base classes for modifier definitions"""

import re
import six
import textwrap
import fnmatch
from typing import List

from llnl.util.tty.colify import colified
import llnl.util.tty as tty

from ramble.language.modifier_language import ModifierMeta
from ramble.language.shared_language import register_builtin  # noqa: F401
from ramble.error import RambleError
import ramble.util.colors as rucolor


class ModifierBase(object, metaclass=ModifierMeta):
    name = None
    uses_spack = False
    _builtin_name = 'modifier_builtin::{obj_name}::{name}'
    _mod_prefix_builtin = r'modifier_builtin::'
    _mod_builtin_regex = r'modifier_builtin::(?P<modifier>[\w-]+)::'
    _builtin_required_key = 'required'
    builtin_group = 'modifier'

    modifier_class = 'ModifierBase'

    #: Lists of strings which contains GitHub usernames of attributes.
    #: Do not include @ here in order not to unnecessarily ping the users.
    maintainers: List[str] = []
    tags: List[str] = []

    def __init__(self, file_path):
        super().__init__()

        self._file_path = file_path
        self._on_executables = ['*']
        self._usage_mode = None

        self._verbosity = 'short'

    def copy(self):
        """Deep copy a modifier instance"""
        new_copy = type(self)(self._file_path)
        new_copy._on_executables = self._on_executables.copy()
        new_copy._usage_mode = self._usage_mode
        new_copy._verbosity = self._verbosity

        return new_copy

    def set_usage_mode(self, mode):
        """Set the usage mode for this modifier.

        If not set, or given an empty string the modifier tries to auto-detect a mode.

        If it cannot auto detect the usage mode, an error is raised.
        """
        if mode:
            self._usage_mode = mode
        else:
            if len(self.modes) > 1 or len(self.modes) == 0:
                raise InvalidModeError('Cannot auto determine usage '
                                       f'mode for modifier {self.name}')

            self._usage_mode = list(self.modes.keys())[0]
            tty.msg(f'    Using default usage mode {self._usage_mode} on modifier {self.name}')

    def set_on_executables(self, on_executables):
        """Set the executables this modifier applies to.

        If given an empty list or a value of None, the default of: '*' is usage.
        """
        if on_executables:
            if not isinstance(on_executables, list):
                raise ModifierError(f'Modifier {self.name} given an unsupported on_executables '
                                    f'type of {type(on_executables)}')

            self._on_executables = []
            for exec_name in on_executables:
                self._on_executables.append(exec_name)
        else:
            self._on_executables = ['*']

    def _long_print(self):
        out_str = []
        out_str.append(rucolor.section_title('Modifier: ') + f'{self.name}\n')
        out_str.append('\n')

        out_str.append(rucolor.section_title('Description:\n'))
        if self.__doc__:
            out_str.append(f'\t{self.__doc__}\n')
        else:
            out_str.append('\tNone\n')

        if hasattr(self, 'tags'):
            out_str.append('\n')
            out_str.append(rucolor.section_title('Tags:\n'))
            out_str.append(colified(self.tags, tty=True))
            out_str.append('\n')

        if hasattr(self, 'modes'):
            out_str.append('\n')
            for mode_name, wl_conf in self.modes.items():
                out_str.append(rucolor.section_title('Mode:') + f' {mode_name}\n')

                if mode_name in self.variable_modifications:
                    out_str.append(rucolor.nested_1('\tVariable Modifications:\n'))
                    for var, conf in self.variable_modifications[mode_name].items():
                        indent = '\t\t'

                        out_str.append(rucolor.nested_2(f'{indent}{var}:\n'))
                        out_str.append(f'{indent}\tMethod: {conf["method"]}\n')
                        out_str.append(f'{indent}\tModification: {conf["modification"]}\n')

            out_str.append('\n')

        if hasattr(self, 'builtins'):
            out_str.append(rucolor.section_title('Builtin Executables:\n'))
            out_str.append('\t' + colified(self.builtins.keys(), tty=True) + '\n')

        if hasattr(self, 'executable_modifiers'):
            out_str.append(rucolor.section_title('Executable Modifiers:\n'))
            out_str.append('\t' + colified(self.executable_modifiers.keys(), tty=True) + '\n')

        if hasattr(self, 'package_manager_configs'):
            out_str.append(rucolor.section_title('Package Manager Configs:\n'))
            for name, config in self.package_manager_configs.items():
                out_str.append(f'\t{name} = {config}\n')
            out_str.append('\n')

        if hasattr(self, 'default_compilers'):
            out_str.append(rucolor.section_title('Default Compilers:\n'))
            for comp_name, comp_def in self.default_compilers.items():
                out_str.append(rucolor.nested_2(f'\t{comp_name}:\n'))
                out_str.append(rucolor.nested_3('\t\tSpack Spec:') +
                               f'{comp_def["spack_spec"].replace("@", "@@")}\n')

                if 'compiler_spec' in comp_def and comp_def['compiler_spec']:
                    out_str.append(rucolor.nested_3('\t\tCompiler Spec:\n') +
                                   f'{comp_def["compiler_spec"].replace("@", "@@")}\n')

                if 'compiler' in comp_def and comp_def['compiler']:
                    out_str.append(rucolor.nested_3('\t\tCompiler:\n') +
                                   f'{comp_def["compiler"]}\n')
            out_str.append('\n')

        if hasattr(self, 'software_specs'):
            out_str.append(rucolor.section_title('Software Specs:\n'))
            for spec_name, spec_def in self.software_specs.items():
                out_str.append(rucolor.nested_2(f'\t{spec_name}:\n'))
                out_str.append(rucolor.nested_3('\t\tSpack Spec:') +
                               f'{spec_def["spack_spec"].replace("@", "@@")}\n')

                if 'compiler_spec' in spec_def and spec_def['compiler_spec']:
                    out_str.append(rucolor.nested_3('\t\tCompiler Spec:') +
                                   f'{spec_def["compiler_spec"].replace("@", "@@")}\n')

                if 'compiler' in spec_def and spec_def['compiler']:
                    out_str.append(rucolor.nested_3('\t\tCompiler:') +
                                   f'{spec_def["compiler"]}\n')
            out_str.append('\n')

        return out_str

    def _short_print(self):
        return [self.name]

    def __str__(self):
        if self._verbosity == 'long':
            return ''.join(self._long_print())
        elif self._verbosity == 'short':
            return ''.join(self._short_print())
        return self.name

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

    def modded_variables(self, app):
        mods = {}

        if self._usage_mode not in self.variable_modifications:
            return mods

        for var, var_mod in self.variable_modifications[self._usage_mode].items():
            if var_mod['method'] in ['append', 'prepend']:
                # var_str = app.expander.expansion_str(var)
                # prev_val = app.expander.expand_var(var_str)
                prev_val = app.variables[var]
                if var_mod['method'] == 'append':
                    mods[var] = f'{prev_val} {var_mod["modification"]}'
                else:  # method == prepend
                    mods[var] = f'{var_mod["modification"]} {prev_val}'
            else:  # method == set
                mods[var] = var_mod['modification']

        return mods

    def applies_to_executable(self, executable):
        apply = False

        mod_regex = re.compile(self._mod_prefix_builtin + f'{self.name}::')
        for pattern in self._on_executables:
            if fnmatch.fnmatch(executable, pattern):
                apply = True

        exec_match = mod_regex.match(executable)
        if exec_match:
            apply = True

        return apply

    def apply_executable_modifiers(self, executable_name, executable, app_inst=None):
        pre_execs = []
        post_execs = []
        for exec_mod in self.executable_modifiers:
            mod_func = getattr(self, exec_mod)

            pre_exec, post_exec = mod_func(executable_name, executable, app_inst=app_inst)

            pre_execs.extend(pre_exec)
            post_execs.extend(post_exec)

        return pre_execs, post_execs

    def all_env_var_modifications(self):
        if self._usage_mode not in self.env_var_modifications:
            return

        for action, conf in self.env_var_modifications[self._usage_mode].items():
            yield action, conf


class ModifierError(RambleError):
    """
    Exception that is raised by modifiers
    """


class InvalidModeError(ModifierError):
    """
    Exception raised when an invalid mode is passed
    """
