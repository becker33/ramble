# Copyright 2022-2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

import llnl.util.tty as tty
import json

import ramble.experimental.uploader

description = "take actions on experiment results"
section = "results"
level = "short"


def setup_parser(subparser):
    sp = subparser.add_subparsers(metavar='SUBCOMMAND',
                                  dest='results_command')

    # Upload
    upload_parser = sp.add_parser('upload', help=results_upload.__doc__)
    upload_parser.add_argument(
        'filename', help='path of file to upload')


def results_upload(args):
    """Imports Ramble experiment results from JSON file and uploads them as
    specified in the upload block of Ramble's config file."""
    imported_results = import_results_file(args.filename)

    ramble.experimental.uploader.upload_results(imported_results)


def import_results_file(filename):
    """
    Import Ramble experiment results from a JSON file.
    """
    tty.debug("File to import:")
    tty.debug(filename)

    imported_file = open(filename)

    try:
        tty.msg("Import file...")
        parsed_json_file = json.load(imported_file)
        # Check if data contains an experiment
        if parsed_json_file.get('experiments'):
            return parsed_json_file
        else:
            tty.die("Error parsing file: Does not contain valid data to upload.")
    except ValueError:
        tty.die("Error parsing file: Invalid JSON formatting.")


def results(parser, args):
    action = {'upload': results_upload}
    action[args.results_command](args)
