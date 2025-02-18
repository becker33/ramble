# Copyright 2022-2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

import ramble.language.language_base
import ramble.language.shared_language
from ramble.schema.types import OUTPUT_CAPTURE
import ramble.language.language_helpers
import ramble.success_criteria


"""This package contains directives that can be used within a package.

Directives are functions that can be called inside a package
definition to modify the package, for example:

    .. code-block:: python

      class Gromacs(SpackApplication):
          # Workload directive:
          workload('water_bare', executables=['pre-process', 'execute-gen'],
               input='water_bare_hbonds')

In the above example, 'workload' is a ramble directive

There are many available directives, the majority of which are implemented here.

Some examples include:

  workload
  executable
  figure_of_merit
  figure_of_merit_context
  input_file

For a full list see below, or consult the existing application definitions for
examples

"""


class ApplicationMeta(ramble.language.shared_language.SharedMeta):
    _directive_names = set()
    _directives_to_be_executed = []


application_directive = ApplicationMeta.directive


@application_directive('workloads')
def workload(name, executables=None, executable=None, input=None,
             inputs=None, **kwargs):
    """Adds a workload to this application

    Defines a new workload that can be used within the context of
    its application.

    Args:
        executable: The name of an executable to be used
        executables: A list of executable names to be used
        input (Optional): The name of an input be used
        inputs (Optional): A list of input names that will be used

    Either executable, or executables is a required input argument.
    """

    def _execute_workload(app):
        app.workloads[name] = {
            'executables': [],
            'inputs': []
        }

        all_execs = ramble.language.language_helpers.require_definition(executable,
                                                                        executables,
                                                                        'executable',
                                                                        'executables',
                                                                        'workload')

        app.workloads[name]['executables'] = all_execs.copy()

        all_inputs = ramble.language.language_helpers.merge_definitions(input, inputs)

        app.workloads[name]['inputs'] = all_inputs.copy()

    return _execute_workload


@application_directive('executables')
def executable(name, template, use_mpi=False, redirect='{log_file}',
               output_capture=OUTPUT_CAPTURE.DEFAULT, **kwargs):
    """Adds an executable to this application

    Defines a new executable that can be used to configure workloads and
    experiments with.

    Executables may or may not use MPI.

    Args:
        template: The template command this executable should generate from
        use_mpi: (Boolean) determines if this executable should be
                 wrapped with an `mpirun` like command or not.
        redirect (Optional): Sets the path for outputs to be written to.
                             defaults to {log_file}
        output_capture (Optional): Declare which ouptu (stdout, stderr, both) to
                                   capture. Defaults to stdout

    """

    def _execute_executable(app):
        from ramble.util.executable import CommandExecutable
        app.executables[name] = CommandExecutable(
            name=name, template=template, use_mpi=use_mpi, redirect=redirect,
            output_capture=output_capture)

    return _execute_executable


@application_directive('inputs')
def input_file(name, url, description, target_dir='{input_name}', sha256=None, extension=None,
               expand=True, **kwargs):
    """Adds an input file definition to this application

    Defines a new input file.
    An input file must define it's name, and a url where the input can be
    fetched from.

    Args:
        url: Path to the input file / archive
        description: Description of this input file
        target_dir (Optional): The directory where the archive will be
                               expanded. Defaults to 'input'
        sha256 (Optional): The expected sha256 checksum for the input file
        extension (Optional): The extension to use for the input, if it isn't part of the
                              file name.
        expand (Optional): Whether the input should be expanded or not. Defaults to True
    """

    def _execute_input_file(app):
        app.inputs[name] = {
            'url': url,
            'description': description,
            'target_dir': target_dir,
            'sha256': sha256,
            'extension': extension,
            'expand': expand
        }

    return _execute_input_file


@application_directive('workload_variables')
def workload_variable(name, default, description, values=None, workload=None,
                      workloads=None, **kwargs):
    """Define a new variable to be used in experiments

    Defines a new variable that can be defined within the
    experiments.yaml config file, to control various aspects of
    an experiment.

    These are specific to each workload.
    """

    def _execute_workload_variable(app):
        all_workloads = ramble.language.language_helpers.require_definition(workload,
                                                                            workloads,
                                                                            'workload',
                                                                            'workloads',
                                                                            'workload_variable')

        for wl_name in all_workloads:
            if wl_name not in app.workload_variables:
                app.workload_variables[wl_name] = {}

            app.workload_variables[wl_name][name] = {
                'default': default,
                'description': description
            }
            if values:
                app.workload_variables[wl_name][name]['values'] = values

    return _execute_workload_variable
