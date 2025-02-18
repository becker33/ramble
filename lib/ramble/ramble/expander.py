# Copyright 2022-2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

import string
import ast
import six
import operator

import llnl.util.tty as tty

import ramble.error
import ramble.keywords

supported_math_operators = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow:
    operator.pow, ast.BitXor: operator.xor, ast.USub: operator.neg,
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.And: operator.and_, ast.Or: operator.or_
}

formatter = string.Formatter()


class ExpansionDict(dict):
    def __missing__(self, key):
        return '{' + key + '}'


class Expander(object):
    """A class that will track and expand keyword arguments

    This class will track variables and their definitions, to allow for
    expansion within string.

    The variables can come from workspace variables, software stack variables,
    and experiment variables.

    Additionally, math will be evaluated as part of expansion.
    """

    _keywords = ramble.keywords.keywords

    def __init__(self, variables, experiment_set):
        self._variables = variables

        self._experiment_set = experiment_set

        self._application_name = None
        self._workload_name = None
        self._experiment_name = None

        self._application_namespace = None
        self._workload_namespace = None
        self._experiment_namespace = None
        self._env_namespace = None

        self._application_input_dir = None
        self._workload_input_dir = None

        self._application_run_dir = None
        self._workload_run_dir = None
        self._experiment_run_dir = None

    @property
    def application_name(self):
        if not self._application_name:
            self._application_name = self.expand_var_name(self._keywords.application_name)

        return self._application_name

    @property
    def workload_name(self):
        if not self._workload_name:
            self._workload_name = self.expand_var_name(self._keywords.workload_name)

        return self._workload_name

    @property
    def experiment_name(self):
        if not self._experiment_name:
            self._experiment_name = self.expand_var_name(self._keywords.experiment_name)

        return self._experiment_name

    @property
    def application_namespace(self):
        if not self._application_namespace:
            self._application_namespace = self.application_name

        return self._application_namespace

    @property
    def workload_namespace(self):
        if not self._workload_namespace:
            self._workload_namespace = '%s.%s' % (self.application_name,
                                                  self.workload_name)

        return self._workload_namespace

    @property
    def experiment_namespace(self):
        if not self._experiment_namespace:
            self._experiment_namespace = '%s.%s.%s' % (self.application_name,
                                                       self.workload_name,
                                                       self.experiment_name)

        return self._experiment_namespace

    @property
    def env_namespace(self):
        if not self._env_namespace:
            var = self.expansion_str(self._keywords.env_name) + \
                '.' + self.expansion_str(self._keywords.workload_name)
            self._env_namespace = self.expand_var(var)

        return self._env_namespace

    @property
    def application_input_dir(self):
        if not self._application_input_dir:
            self._application_input_dir = \
                self.expand_var_name(self._keywords.application_input_dir)

        return self._application_input_dir

    @property
    def workload_input_dir(self):
        if not self._workload_input_dir:
            self._workload_input_dir = self.expand_var_name(self._keywords.workload_input_dir)

        return self._workload_input_dir

    @property
    def application_run_dir(self):
        if not self._application_run_dir:
            self._application_run_dir = self.expand_var_name(self._keywords.application_run_dir)

        return self._application_run_dir

    @property
    def workload_run_dir(self):
        if not self._workload_run_dir:
            self._workload_run_dir = self.expand_var_name(self._keywords.workload_run_dir)

        return self._workload_run_dir

    @property
    def experiment_run_dir(self):
        if not self._experiment_run_dir:
            self._experiment_run_dir = self.expand_var_name(self._keywords.experiment_run_dir)

        return self._experiment_run_dir

    def expand_lists(self, var):
        """Expand a variable into a list if possible

        If expanding a variable would generate a list, this function will
        return a list. If any error case happens, this function will return
        the unmodified input value.

        NOTE: This function is generally called early in the expansion. This allows
        lists to be generated before rendering experiments, but does not support
        pulling a list from a different experiment.
        """
        try:
            math_ast = ast.parse(str(var), mode='eval')
            value = self.eval_math(math_ast.body)
            if isinstance(value, list):
                return value
            return var
        except MathEvaluationError:
            return var
        except AttributeError:
            return var
        except ValueError:
            return var
        except SyntaxError:
            return var

    def expand_var_name(self, var_name, extra_vars=None, allow_passthrough=True):
        """Convert a variable name to an expansion string, and expand it

        Take a variable name (var) and convert it to an expansion string by
        calling the expansion_str function. Pass the expansion string into
        expand_var, and return the result.

        Args:
            var_name: String name of variable to expand
            extra_vars: Variable definitions to use with highest precedence
            allow_passthrough: Whether the string is allowed to have keywords
                               after expansion
        """
        return self.expand_var(self.expansion_str(var_name),
                               extra_vars=extra_vars,
                               allow_passthrough=allow_passthrough)

    def expand_var(self, var, extra_vars=None, allow_passthrough=True):
        """Perform expansion of a string

        Expand a string by building up a dict of all
        expansion variables.

        Args:
            var: String variable to expand
            extra_vars: Variable definitions to use with highest precedence
            allow_passthrough: Whether the string is allowed to have keywords
                               after expansion
        """

        expansions = self._variables
        if extra_vars:
            expansions = self._variables.copy()
            expansions.update(extra_vars)

        expanded = self._partial_expand(expansions, str(var), allow_passthrough=allow_passthrough)

        if self._fully_expanded(expanded):
            try:
                math_ast = ast.parse(str(expanded), mode='eval')
                evaluated = self.eval_math(math_ast.body)
                expanded = evaluated
            except MathEvaluationError as e:
                tty.debug(e)
            except SyntaxError:
                pass
        elif not allow_passthrough:
            tty.debug('Passthrough expansion not allowed.')
            tty.debug('    Variable definitions are: {str(self._variables)}')
            raise ExpanderError(f'Expander was unable to fully expand "{var}", '
                                'and is not allowed to passthrough undefined variables.')

        return str(expanded).lstrip()

    @staticmethod
    def expansion_str(in_str):
        l_delimiter = '{'
        r_delimiter = '}'
        return f'{l_delimiter}{in_str}{r_delimiter}'

    def _all_keywords(self, in_str):
        """Iterator for all keyword arguments in a string

        Args:
            in_str (string): Input string to detect keywords from

        Yields:
          Each keyword argument in in_str
        """
        if isinstance(in_str, six.string_types):
            for keyword in string.Formatter().parse(in_str):
                if keyword[1]:
                    yield keyword[1]

    def _fully_expanded(self, in_str):
        """Test if a string is fully expanded

        Args:
            in_str (string): Input string to test as expanded

        Returns boolean. True if `in_str` contains no keywords, false if a
        keyword is detected.
        """
        for kw in self._all_keywords(in_str):
            return False
        return True

    def _partial_expand(self, expansion_vars, in_str, allow_passthrough=True):
        """Perform expansion of a string with some variables

        args:
          expansion_vars (dict): Variables to perform expansion with
          in_str (str): Input template string to expand
          allow_passthrough (bool): Define if variables are allowed to passthrough
                                    without being expanded.

        returns:
          in_str (str): Expanded version of input string
        """

        exp_dict = ExpansionDict()
        exp_positional = []
        if isinstance(in_str, six.string_types):
            for tup in formatter.parse(in_str):
                kw = tup[1]
                if kw is not None:
                    if len(kw) > 0 and kw in expansion_vars:
                        exp_dict[kw] = self._partial_expand(expansion_vars,
                                                            expansion_vars[kw])
                    elif len(kw) == 0:
                        exp_positional.append('{}')

            passthrough_vars = {}
            for kw, val in exp_dict.items():
                if self._fully_expanded(val):
                    try:
                        math_ast = ast.parse(str(val), mode='eval')
                        evaluated = self.eval_math(math_ast.body)
                        exp_dict[kw] = evaluated
                    except MathEvaluationError as e:
                        tty.debug(e)
                    except SyntaxError:
                        pass
                elif not allow_passthrough:
                    tty.debug(f'Expansion stack errors: attempted to expand "{kw}" = "{val}"')
                else:
                    for kw in self._all_keywords(val):
                        passthrough_vars[kw] = '{' + kw + '}'
            exp_dict.update(passthrough_vars)

            try:
                return formatter.vformat(in_str, exp_positional, exp_dict)
            except IndexError as e:
                if allow_passthrough:
                    return in_str

                tty.debug('Index error when parsing:\n')
                tty.debug(in_str)
                tty.debug(e)
                raise RambleSyntaxError('Error occurred while parsing an expansion string.')
            except KeyError as e:
                tty.debug('Invalid variable name encountered')
                tty.debug(e)
                raise RambleSyntaxError('Expansion failed on:\n'
                                        f'{in_str}\n'
                                        'Which contains an invalid variable name')
            except ValueError as e:
                return in_str
                tty.debug('JSON/YAML dict syntax should not be manually escaped.')
                tty.debug('   {} will be automatically escaped')
                tty.debug('   {{}} raises a syntax error')
                tty.debug(e)
                raise RambleSyntaxError('Expansion failed on:\n'
                                        f'{in_str}\n'
                                        'JSON/YAML dict syntax should not be manually escaped')
            except AttributeError:
                tty.debug(f'Error encountered while trying to expand variable {in_str}')
                tty.debug(f'Expansion dict was: {exp_dict}')
                raise RambleSyntaxError(f'Expansion failed on variable {in_str}',
                                        'Variable names cannot contain decimals.')

        return in_str

    def eval_math(self, node):
        """Evaluate math from parsing the AST

        Does not assume a specific type of operands.
        Some operators will generate floating point, while
        others will generate integers (if the inputs are integers).
        """
        if isinstance(node, ast.Num):
            return self._ast_num(node)
        elif isinstance(node, ast.Constant):
            return self._ast_constant(node)
        elif isinstance(node, ast.Name):
            return self._ast_name(node)
        elif isinstance(node, ast.Attribute):
            return self._ast_attr(node)
        elif isinstance(node, ast.Compare):
            return self._eval_comparisons(node)
        elif isinstance(node, ast.BoolOp):
            return self._eval_bool_op(node)
        elif isinstance(node, ast.BinOp):
            return self._eval_binary_ops(node)
        elif isinstance(node, ast.UnaryOp):
            return self._eval_unary_ops(node)
        elif isinstance(node, ast.Call):
            return self._eval_function_call(node)
        else:
            node_type = str(type(node))
            raise MathEvaluationError(f'Unsupported math AST node {node_type}:\n' +
                                      f'\t{node.__dict__}')

    # Ast logic helper methods
    def __raise_syntax_error(self, node):
        node_type = str(type(node))
        raise RambleSyntaxError(f'Syntax error while processing {node_type} node:\n' +
                                f'{node.__dict__}')

    def _ast_num(self, node):
        """Handle a number node in the ast"""
        return node.n

    def _ast_constant(self, node):
        """Handle a constant node in the ast"""
        return node.value

    def _ast_name(self, node):
        """Handle a name node in the ast"""
        return node.id

    def _ast_attr(self, node):
        """Handle an attribute node in the ast"""
        if isinstance(node.value, ast.Attribute):
            base = self._ast_attr(node.value)
        elif isinstance(node.value, ast.Name):
            base = self._ast_name(node.value)
        else:
            self.__raise_syntax_error(node)

        val = f'{base}.{node.attr}'
        return val

    def _eval_function_call(self, node):
        """Handle a subset of function call nodes in the ast"""

        args = []
        kwargs = {}
        for arg in node.args:
            args.append(self.eval_math(arg))
        for kw in node.keywords:
            kwargs[self.eval_math(kw.arg)] = self.eval_math(kw.value)

        if node.func.id == 'range':
            return list(range(*args, **kwargs))

    def _eval_bool_op(self, node):
        """Handle a boolean operator node in the ast"""
        try:
            op = supported_math_operators[type(node.op)]

            result = self.eval_math(node.values[0])

            for value in node.values[1:]:
                result = op(result, self.eval_math(value))

            return result

        except TypeError:
            raise SyntaxError('Unsupported operand type in boolean operator')
        except KeyError:
            raise SyntaxError('Unsupported boolean operator')

    def _eval_comparisons(self, node):
        """Handle a comparison node in the ast"""

        # Extract In nodes, and call their helper
        if len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
            return self._eval_comp_in(node)

        # Try to evaluate the comparison logic, if not return the node as is.
        try:
            cur_left = self.eval_math(node.left)

            op = supported_math_operators[type(node.ops[0])]
            cur_right = self.eval_math(node.comparators[0])

            result = op(cur_left, cur_right)

            if len(node.ops) > 1:
                cur_left = cur_right
                for comp, right in zip(node.ops, node.comparators)[1:]:
                    op = supported_math_operators[type(comp)]
                    cur_right = self.eval_math(right)

                    result = result and op(cur_left, cur_right)

                    cur_left = cur_right
            return result
        except TypeError:
            raise SyntaxError('Unsupported operand type in binary comparison operator')
        except KeyError:
            raise SyntaxError('Unsupported binary comparison operator')

    def _eval_comp_in(self, node):
        """Handle in nodes in the ast

        Perform extraction of `<variable> in <experiment>` syntax.

        Raises an exception if the experiment does not exist.
        """
        if isinstance(node.left, ast.Name):
            var_name = self._ast_name(node.left)
            if isinstance(node.comparators[0], ast.Attribute):
                namespace = self.eval_math(node.comparators[0])
                val = self._experiment_set.get_var_from_experiment(namespace,
                                                                   self.expansion_str(var_name))
                if not val:
                    raise RambleSyntaxError(f'{namespace} does not exist in: ' +
                                            f'"{var_name} in {namespace}"')
                    self.__raise_syntax_error(node)
                return val
        self.__raise_syntax_error(node)

    def _eval_binary_ops(self, node):
        """Evaluate binary operators in the ast

        Extract the binary operator, and evaluate it.
        """
        try:
            left_eval = self.eval_math(node.left)
            right_eval = self.eval_math(node.right)
            op = supported_math_operators[type(node.op)]
            if isinstance(left_eval, six.string_types) or isinstance(right_eval, six.string_types):
                raise SyntaxError('Unsupported operand type in binary operator')
            return op(left_eval, right_eval)
        except TypeError:
            raise SyntaxError('Unsupported operand type in binary operator')
        except KeyError:
            raise SyntaxError('Unsupported binary operator')

    def _eval_unary_ops(self, node):
        """Evaluate unary operators in the ast

        Extract the unary operator, and evaluate it.
        """
        try:
            operand = self.eval_math(node.operand)
            if isinstance(operand, six.string_types):
                raise SyntaxError('Unsupported operand type in unary operator')
            op = supported_math_operators[type(node.op)]
            return op(operand)
        except TypeError:
            raise SyntaxError('Unsupported operand type in unary operator')
        except KeyError:
            raise SyntaxError('Unsupported unary operator')


class ExpanderError(ramble.error.RambleError):
    """Raised when an error happens within an expander"""


class MathEvaluationError(ExpanderError):
    """Raised when an error happens while evaluating math during
    expansion
    """


class RambleSyntaxError(ExpanderError):
    """Raised when a syntax error happens within variable definitions"""


class ApplicationNotDefinedError(ExpanderError):
    """Raised when an application is not defined properly"""


class WorkloadNotDefinedError(ExpanderError):
    """Raised when a workload is not defined properly"""


class ExperimentNotDefinedError(ExpanderError):
    """Raised when an experiment is not defined properly"""
