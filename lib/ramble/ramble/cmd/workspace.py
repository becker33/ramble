# Copyright 2022-2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

import os
import sys
import tempfile

import llnl.util.tty as tty
import llnl.util.tty.color as color
from llnl.util.tty.colify import colify

import spack.util.string as string
from spack.util.editor import editor
import spack.util.environment

import ramble.cmd
import ramble.cmd.common.arguments
import ramble.cmd.common.arguments as arguments

import ramble.config
import ramble.workspace
import ramble.workspace.shell
import ramble.experiment_set
import ramble.experimental.uploader
import ramble.software_environments
import ramble.util.colors as rucolor

if sys.version_info >= (3, 3):
    from collections.abc import Sequence  # novm noqa: F401
else:
    from collections import Sequence  # noqa: F401


description = 'manage experiment workspaces'
section = 'workspaces'
level = 'short'

subcommands = [
    'activate',
    'archive',
    'deactivate',
    'create',
    'concretize',
    'setup',
    'analyze',
    'info',
    'edit',
    'mirror',
    ['list', 'ls'],
    ['remove', 'rm'],
]


def workspace_activate_setup_parser(subparser):
    """Set the current workspace"""
    shells = subparser.add_mutually_exclusive_group()
    shells.add_argument(
        '--sh', action='store_const', dest='shell', const='sh',
        help="print sh commands to activate the workspace")
    shells.add_argument(
        '--csh', action='store_const', dest='shell', const='csh',
        help="print csh commands to activate the workspace")
    shells.add_argument(
        '--fish', action='store_const', dest='shell', const='fish',
        help="print fish commands to activate the workspace")
    shells.add_argument(
        '--bat', action='store_const', dest='shell', const='bat',
        help="print bat commands to activate the environment")

    subparser.add_argument(
        '-p', '--prompt', action='store_true', default=False,
        help="decorate the command line prompt when activating")

    ws_options = subparser.add_mutually_exclusive_group()
    ws_options.add_argument(
        '--temp', action='store_true', default=False,
        help='create and activate a workspace in a temporary directory')
    ws_options.add_argument(
        '-d', '--dir', default=None,
        help="activate the workspace in this directory")
    ws_options.add_argument(
        metavar='workspace', dest='activate_workspace', nargs='?', default=None,
        help='name of workspace to activate')


def create_temp_workspace_directory():
    """
    Returns the path of a temporary directory in which to
    create a workspace
    """
    return tempfile.mkdtemp(prefix="ramble-")


def workspace_activate(args):
    if not args.activate_workspace and not args.dir and not args.temp:
        tty.die('ramble workspace activate requires a workspace name, directory, or --temp')

    if not args.shell:
        ramble.cmd.common.shell_init_instructions(
            "ramble workspace activate",
            "    eval `ramble workspace activate {sh_arg} [...]`")
        return 1

    workspace_name_or_dir = args.activate_workspace or args.dir

    # Temporary workspace
    if args.temp:
        workspace = create_temp_workspace_directory()
        workspace_path = os.path.abspath(workspace)
        short_name = os.path.basename(workspace_path)
        ramble.workspace.Workspace(workspace).write()

    # Named workspace
    elif ramble.workspace.exists(workspace_name_or_dir) and not args.dir:
        workspace_path = ramble.workspace.root(workspace_name_or_dir)
        short_name = workspace_name_or_dir

    # Workspace directory
    elif ramble.workspace.is_workspace_dir(workspace_name_or_dir):
        workspace_path = os.path.abspath(workspace_name_or_dir)
        short_name = os.path.basename(workspace_path)

    else:
        tty.die("No such workspace: '%s'" % workspace_name_or_dir)

    workspace_prompt = '[%s]' % short_name

    # We only support one active workspace at a time, so deactivate the current one.
    if ramble.workspace.active_workspace() is None:
        cmds = ''
        env_mods = spack.util.environment.EnvironmentModifications()
    else:
        cmds = ramble.workspace.shell.deactivate_header(shell=args.shell)
        env_mods = ramble.workspace.shell.deactivate()

    # Activate new workspace
    active_workspace = ramble.workspace.Workspace(workspace_path)
    cmds += ramble.workspace.shell.activate_header(
        ws=active_workspace,
        shell=args.shell,
        prompt=workspace_prompt if args.prompt else None
    )
    env_mods.extend(ramble.workspace.shell.activate(
        ws=active_workspace
    ))
    cmds += env_mods.shell_modifications(args.shell)
    sys.stdout.write(cmds)


def workspace_deactivate_setup_parser(subparser):
    """deactivate any active workspace in the shell"""
    shells = subparser.add_mutually_exclusive_group()
    shells.add_argument(
        '--sh', action='store_const', dest='shell', const='sh',
        help="print sh commands to deactivate the workspace")
    shells.add_argument(
        '--csh', action='store_const', dest='shell', const='csh',
        help="print csh commands to deactivate the workspace")
    shells.add_argument(
        '--fish', action='store_const', dest='shell', const='fish',
        help="print fish commands to activate the workspace")
    shells.add_argument(
        '--bat', action='store_const', dest='shell', const='bat',
        help="print bat commands to activate the environment")


def workspace_deactivate(args):
    if not args.shell:
        ramble.cmd.common.shell_init_instructions(
            "ramble workspace deactivate",
            "    eval `ramble workspace deactivate {sh_arg}`",
        )
        return 1

    # Error out when -w, -W, -D flags are given, cause they are ambiguous.
    if args.workspace or args.no_workspace or args.workspace_dir:
        tty.die('Calling ramble workspace deactivate with --workspace,'
                ' --workspace-dir, and --no-workspace '
                'is ambiguous')

    if ramble.workspace.active_workspace() is None:
        tty.die('No workspace is currently active.')

    cmds = ramble.workspace.shell.deactivate_header(args.shell)
    env_mods = ramble.workspace.shell.deactivate()
    cmds += env_mods.shell_modifications(args.shell)
    sys.stdout.write(cmds)


def workspace_create_setup_parser(subparser):
    """create a new workspace"""
    subparser.add_argument(
        'create_workspace', metavar='wrkspc',
        help='name of workspace to create')
    subparser.add_argument(
        '-c', '--config',
        help='configuration file to create workspace with')
    subparser.add_argument(
        '-t', '--template_execute',
        help='execution template file to use when creating workspace')
    subparser.add_argument(
        '-d', '--dir', action='store_true',
        help='create a workspace in a specific directory')


def workspace_create(args):
    _workspace_create(args.create_workspace, args.dir,
                      args.config, args.template_execute)


def _workspace_create(name_or_path, dir=False,
                      config=None, template_execute=None):
    """Create a new workspace

    Arguments:
        name_or_path (str): name of the workspace to create, or path
                            to it
        dir (bool): if True, create a workspace in a directory instead
            of a named workspace
        config (str): path to a configuration file that should
                      generate the workspace
        template_execute (str): Path to a template execute script to
                                create the workspace with
    """

    if dir:
        workspace = ramble.workspace.Workspace(name_or_path)
        workspace.write()
        tty.msg("Created workspace in %s" % workspace.path)
        tty.msg("You can activate this workspace with:")
        tty.msg("  ramble workspace activate %s" % workspace.path)
    else:
        workspace = ramble.workspace.create(name_or_path)
        workspace.write()
        tty.msg("Created workspace in %s" % name_or_path)
        tty.msg("You can activate this workspace with:")
        tty.msg("  ramble workspace activate %s" % name_or_path)

    if config:
        with open(config, 'r') as f:
            workspace._read_config('workspace', f)
            workspace._write_config('workspace')

    if template_execute:
        with open(template_execute, 'r') as f:
            _, file_name = os.path.split(template_execute)
            template_name = os.path.splitext(file_name)[0]
            workspace._read_template(template_name, f.read())
            workspace._write_templates()

    return workspace


def workspace_remove_setup_parser(subparser):
    """remove an existing workspace"""
    subparser.add_argument(
        'rm_wrkspc', metavar='wrkspc', nargs='+',
        help='workspace(s) to remove')
    arguments.add_common_arguments(subparser, ['yes_to_all'])


def workspace_remove(args):
    """Remove a *named* workspace.

    This removes an environment managed by Ramble. Directory workspaces
    should be removed manually.
    """
    read_workspaces = []
    for workspace_name in args.rm_wrkspc:
        workspace = ramble.workspace.read(workspace_name)
        read_workspaces.append(workspace)

    tty.debug('Removal args: {}'.format(args))

    if not args.yes_to_all:
        answer = tty.get_yes_or_no(
            'Really remove %s %s?' % (
                string.plural(len(args.rm_wrkspc), 'workspace', show_n=False),
                string.comma_and(args.rm_wrkspc)),
            default=False)
        if not answer:
            tty.die("Will not remove any workspaces")

    for workspace in read_workspaces:
        if workspace.active:
            tty.die("Workspace %s can't be removed while activated."
                    % workspace.name)

        workspace.destroy()
        tty.msg("Successfully removed workspace '%s'" % workspace.name)


def workspace_concretize_setup_parser(subparser):
    """Concretize a workspace"""
    pass


def workspace_concretize(args):
    ws = ramble.cmd.require_active_workspace(cmd_name='workspace concretize')

    tty.debug('Concretizing workspace')
    ws.concretize()


def workspace_setup_setup_parser(subparser):
    """Setup a workspace"""
    subparser.add_argument(
        '--dry-run', dest='dry_run',
        action='store_true',
        help='perform a dry run. Sets up directories and generates ' +
             'all scripts. Prints commands that would be executed ' +
             'for installation, and files that would be downloaded.')


def workspace_setup(args):
    ws = ramble.cmd.require_active_workspace(cmd_name='workspace setup')

    if args.dry_run:
        ws.dry_run = True

    tty.debug('Setting up workspace')
    with ws.write_transaction():
        ws.run_pipeline('setup')


def workspace_analyze_setup_parser(subparser):
    """Analyze a workspace"""
    subparser.add_argument(
        '-f', '--formats', dest='output_formats',
        nargs='+',
        default=['text'],
        help='list of output formats to write.' +
             'Supported formats are json, yaml, or text',
        required=False)

    subparser.add_argument(
        '-u', '--upload',
        dest='upload',
        action='store_true',
        help='Push experiment data to remote store (as defined in config)',
        required=False)

    subparser.add_argument(
        '--always-print-foms',
        dest='always_print_foms',
        action='store_true',
        help='Control if figures of merit are printed even if an experiment fails',
        required=False)


def workspace_analyze(args):
    ws = ramble.cmd.require_active_workspace(cmd_name='workspace analyze')
    ws.always_print_foms = args.always_print_foms

    tty.debug('Analyzing workspace')
    with ws.write_transaction():
        ws.run_pipeline('analyze')
        ws.dump_results(output_formats=args.output_formats)

    # FIXME: this will fire the analyze logic of twice currently
    if args.upload:
        ramble.experimental.uploader.upload_results(ws.results)


def workspace_info_setup_parser(subparser):
    """Information about a workspace"""
    subparser.add_argument('-v', '--verbose', action='count', default=0,
                           help='level of verbosity. Add flags to ' +
                                'increase description of workspace')


def workspace_info(args):
    ws = ramble.cmd.require_active_workspace(cmd_name='workspace info')

    color.cprint(rucolor.section_title('Workspace: ') + ws.name)
    color.cprint('')
    color.cprint(rucolor.section_title('Location: ') + ws.path)
    color.cprint('')

    # Print workspace templates that currently exist
    color.cprint(rucolor.section_title('Workspace Templates:'))
    for template, _ in ws.all_templates():
        color.cprint('    %s' % template)

    # Print workspace variables information
    workspace_vars = ws.get_workspace_vars()

    # Build experiment set
    experiment_set = ws.build_experiment_set()

    # Print experiment information
    # We built a "print_experiment_set" to access the scopes of variables for each
    # experiment, rather than having merged scopes as we do in the base experiment_set.
    # The base experiment_set is used to list *all* experiments.
    color.cprint('')
    color.cprint(rucolor.section_title('Experiments:'))
    for app, workloads, app_vars, app_env_vars, app_internals, app_template, app_chained_exps, \
            app_mods in ws.all_applications():
        for workload, experiments, workload_vars, workload_env_vars, workload_internals, \
                workload_template, workload_chained_exps, workload_mods \
                in ws.all_workloads(workloads):
            for exp, _, exp_vars, exp_env_vars, exp_zips, exp_matrices, exp_internals, \
                    exp_template, exp_chained_exps, exp_mods, exp_excludes \
                    in ws.all_experiments(experiments):
                print_experiment_set = ramble.experiment_set.ExperimentSet(ws)
                print_experiment_set.set_application_context(app, app_vars,
                                                             app_env_vars, app_internals,
                                                             app_template, app_chained_exps,
                                                             app_mods)
                print_experiment_set.set_workload_context(workload, workload_vars,
                                                          workload_env_vars, workload_internals,
                                                          workload_template, workload_chained_exps,
                                                          workload_mods)
                print_experiment_set.set_experiment_context(exp,
                                                            exp_vars,
                                                            exp_env_vars,
                                                            exp_zips,
                                                            exp_matrices,
                                                            exp_internals,
                                                            exp_template,
                                                            exp_chained_exps,
                                                            exp_mods,
                                                            exp_excludes)

                print_experiment_set.build_experiment_chains()

                color.cprint(rucolor.nested_1('  Application: ') + app)
                color.cprint(rucolor.nested_2('    Workload: ') + workload)

                # Define variable printing groups.
                var_indent = '        '
                var_group_names = [
                    rucolor.config_title('Config'),
                    rucolor.section_title('Workspace'),
                    rucolor.nested_1('Application'),
                    rucolor.nested_2('Workload'),
                    rucolor.nested_3('Experiment'),
                ]
                header_base = rucolor.nested_4('Variables from')
                config_vars = ramble.config.config.get('config:variables')

                for exp_name, _ in print_experiment_set.all_experiments():
                    app_inst = experiment_set.get_experiment(exp_name)
                    if app_inst.is_template:
                        color.cprint(rucolor.nested_3('      Template Experiment: ') + exp_name)
                    else:
                        color.cprint(rucolor.nested_3('      Experiment: ') + exp_name)

                    if args.verbose >= 1:
                        var_groups = [config_vars, workspace_vars,
                                      app_vars, workload_vars, exp_vars]

                        # Print each group that has variables in it
                        for group, name in zip(var_groups, var_group_names):
                            if group:
                                header = f'{header_base} {name}'
                                app_inst.print_vars(header=header,
                                                    vars_to_print=group,
                                                    indent=var_indent)

                        app_inst.print_internals(indent=var_indent)
                        app_inst.print_chain_order(indent=var_indent)

    # Print software stack information
    color.cprint('')
    software_environments = ramble.software_environments.SoftwareEnvironments(ws)
    software_environments.print_environments(verbosity=args.verbose)

#
# workspace list
#


def workspace_list_setup_parser(subparser):
    """list available workspaces"""
    pass


def workspace_list(args):
    names = ramble.workspace.all_workspace_names()

    color_names = []
    for name in names:
        if ramble.workspace.active(name):
            name = color.colorize('@*g{%s}' % name)
        color_names.append(name)

    # say how many there are if writing to a tty
    if sys.stdout.isatty():
        if not names:
            tty.msg('No workspaces')
        else:
            tty.msg('%d workspaces' % len(names))

    colify(color_names, indent=4)


def workspace_edit_setup_parser(subparser):
    """edit workspace config or template"""
    subparser.add_argument(
        '-c', '--config_only', dest='config_only',
        action='store_true',
        help='Only open config files',
        required=False)

    subparser.add_argument(
        '-t', '--template_only', dest='template_only',
        action='store_true',
        help='Only open template files',
        required=False)

    subparser.add_argument(
        '-p', '--print-file', action='store_true',
        help='print the file name that would be edited')


def workspace_edit(args):
    ramble_ws = ramble.cmd.find_workspace_path(args)

    if not ramble_ws:
        tty.die('ramble workspace edit requires either a command '
                'line workspace or an active workspace')

    config_file = ramble.workspace.config_file(ramble_ws)
    template_files = ramble.workspace.all_template_paths(ramble_ws)

    edit_files = [config_file] + template_files

    if args.config_only:
        edit_files = [config_file]
    elif args.template_only:
        edit_files = template_files

    if args.print_file:
        for f in edit_files:
            print(f)
    else:
        editor(*edit_files)


def workspace_archive_setup_parser(subparser):
    """archive current workspace state"""
    subparser.add_argument(
        '--tar-archive', '-t', action='store_true',
        dest='tar_archive',
        help='create a tar.gz of the archive directory for backing up.')

    subparser.add_argument(
        '--upload-url', '-u', dest='upload_url',
        default=None,
        help='URL to upload tar archive into. Does nothing if `-t` is not specified.')


def workspace_archive(args):
    ws = ramble.cmd.require_active_workspace(cmd_name='workspace archive')

    ws.archive(create_tar=args.tar_archive,
               archive_url=args.upload_url)


def workspace_mirror_setup_parser(subparser):
    """mirror current workspace state"""
    subparser.add_argument(
        '-d', dest='mirror_path',
        default=None,
        help='Path to create mirror in.')

    subparser.add_argument(
        '--dry-run', dest='dry_run',
        action='store_true',
        help='perform a dry run. Creates spack environments, ' +
             'prints commands that would be executed ' +
             'for installation, and files that would be downloaded.')


def workspace_mirror(args):
    ws = ramble.cmd.require_active_workspace(cmd_name='workspace archive')

    if args.dry_run:
        ws.dry_run = True

    ws.create_mirror(args.mirror_path)
    ws.run_pipeline('mirror')


#: Dictionary mapping subcommand names and aliases to functions
subcommand_functions = {}


def setup_parser(subparser):
    sp = subparser.add_subparsers(metavar='SUBCOMMAND',
                                  dest='workspace_command')

    for name in subcommands:
        if isinstance(name, (list, tuple)):
            name, aliases = name[0], name[1:]
        else:
            aliases = []

        # add commands to subcommands dict
        function_name = 'workspace_%s' % name
        function = globals()[function_name]
        for alias in [name] + aliases:
            subcommand_functions[alias] = function

        # make a subparser and run the command's setup function on it
        setup_parser_cmd_name = 'workspace_%s_setup_parser' % name
        setup_parser_cmd = globals()[setup_parser_cmd_name]

        subsubparser = sp.add_parser(
            name, aliases=aliases, help=setup_parser_cmd.__doc__)
        setup_parser_cmd(subsubparser)


def workspace(parser, args):
    """Look for a function called environment_<name> and call it."""
    action = subcommand_functions[args.workspace_command]
    action(args)
