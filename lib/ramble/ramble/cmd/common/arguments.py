# Copyright 2022-2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.


import argparse

from spack.util.pattern import Args

__all__ = ['add_common_arguments']

#: dictionary of argument-generating functions, keyed by name
_arguments = {}


def arg(fn):
    """Decorator for a function that generates a common argument.

    This ensures that argument bunches are created lazily. Decorate
    argument-generating functions below with @arg so that
    ``add_common_arguments()`` can find them.

    """
    _arguments[fn.__name__] = fn
    return fn


def add_common_arguments(parser, list_of_arguments):
    """Extend a parser with extra arguments

    Args:
        parser: parser to be extended
        list_of_arguments: arguments to be added to the parser
    """
    for argument in list_of_arguments:
        if argument not in _arguments:
            message = 'Trying to add non existing argument "{0}" to a command'
            raise KeyError(message.format(argument))

        x = _arguments[argument]()
        parser.add_argument(*x.flags, **x.kwargs)


@arg
def yes_to_all():
    return Args(
        '-y', '--yes-to-all', action='store_true', dest='yes_to_all',
        help='assume "yes" is the answer to every confirmation request')


@arg
def tags():
    return Args(
        '-t', '--tags', action='append',
        help='filter a package query by tags')


@arg
def application():
    return Args('application', help='application name')


@arg
def workspace():
    return Args('workspace', help='workspace name')


@arg
def specs():
    return Args(
        'specs', nargs=argparse.REMAINDER, help='one or more workload specs')


@arg
def repo_type():
    from ramble.repository import default_type, OBJECT_NAMES
    return Args(
        '-t', '--type', default=default_type.name,
        help=f"type of repositories to manage. Defaults to '{default_type.name}'. "
        f"Allowed types are {str(OBJECT_NAMES)}",
    )


@arg
def no_checksum():
    return Args(
        '-n', '--no-checksum', action='store_true', default=False,
        help="do not use checksums to verify downloaded files (unsafe)")
