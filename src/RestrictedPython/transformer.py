##############################################################################
#
# Copyright (c) 2002 Zope Foundation and Contributors.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE
#
##############################################################################
"""
transformer module:

uses Python standard library ast module and its containing classes to transform
the parsed python code to create a modified AST for a byte code generation.
"""

# This package should follow the Plone Sytleguide for Python,
# which differ from PEP8:
# http://docs.plone.org/develop/styleguide/python.html


from ._compat import IS_PY2
from ._compat import IS_PY3
from ._compat import IS_PY34_OR_GREATER
from ._compat import IS_PY35_OR_GREATER

import ast
import contextlib
import textwrap


# For AugAssign the operator must be converted to a string.
IOPERATOR_TO_STR = {
    # Shared by python2 and python3
    ast.Add: '+=',
    ast.Sub: '-=',
    ast.Mult: '*=',
    ast.Div: '/=',
    ast.Mod: '%=',
    ast.Pow: '**=',
    ast.LShift: '<<=',
    ast.RShift: '>>=',
    ast.BitOr: '|=',
    ast.BitXor: '^=',
    ast.BitAnd: '&=',
    ast.FloorDiv: '//='
}

if IS_PY35_OR_GREATER:
    IOPERATOR_TO_STR[ast.MatMult] = '@='


# When new ast nodes are generated they have no 'lineno' and 'col_offset'.
# This function copies these two fields from the incoming node
def copy_locations(new_node, old_node):
    assert 'lineno' in new_node._attributes
    new_node.lineno = old_node.lineno

    assert 'col_offset' in new_node._attributes
    new_node.col_offset = old_node.col_offset

    ast.fix_missing_locations(new_node)


class PrintInfo(object):
    def __init__(self):
        self.print_used = False
        self.printed_used = False

    @contextlib.contextmanager
    def new_print_scope(self):
        old_print_used = self.print_used
        old_printed_used = self.printed_used

        self.print_used = False
        self.printed_used = False

        try:
            yield
        finally:
            self.print_used = old_print_used
            self.printed_used = old_printed_used


class RestrictingNodeTransformer(ast.NodeTransformer):

    def __init__(self, errors=None, warnings=None, used_names=None):
        super(RestrictingNodeTransformer, self).__init__()
        self.errors = [] if errors is None else errors
        self.warnings = [] if warnings is None else warnings

        # All the variables used by the incoming source.
        # Internal names/variables, like the ones from 'gen_tmp_name', don't
        # have to be added.
        # 'used_names' is for example needed by 'RestrictionCapableEval' to
        # know wich names it has to supply when calling the final code.
        self.used_names = {} if used_names is None else used_names

        # Global counter to construct temporary variable names.
        self._tmp_idx = 0

        self.print_info = PrintInfo()

    def gen_tmp_name(self):
        # 'check_name' ensures that no variable is prefixed with '_'.
        # => Its safe to use '_tmp..' as a temporary variable.
        name = '_tmp%i' % self._tmp_idx
        self._tmp_idx += 1
        return name

    def error(self, node, info):
        """Record a security error discovered during transformation."""
        lineno = getattr(node, 'lineno', None)
        self.errors.append(
            'Line {lineno}: {info}'.format(lineno=lineno, info=info))

    def warn(self, node, info):
        """Record a security error discovered during transformation."""
        lineno = getattr(node, 'lineno', None)
        self.warnings.append(
            'Line {lineno}: {info}'.format(lineno=lineno, info=info))

    def guard_iter(self, node):
        """
        Converts:
            for x in expr
        to
            for x in _getiter_(expr)

        Also used for
        * list comprehensions
        * dict comprehensions
        * set comprehensions
        * generator expresions
        """
        node = self.node_contents_visit(node)

        if isinstance(node.target, ast.Tuple):
            spec = self.gen_unpack_spec(node.target)
            new_iter = ast.Call(
                func=ast.Name('_iter_unpack_sequence_', ast.Load()),
                args=[node.iter, spec, ast.Name('_getiter_', ast.Load())],
                keywords=[])
        else:
            new_iter = ast.Call(
                func=ast.Name("_getiter_", ast.Load()),
                args=[node.iter],
                keywords=[])

        copy_locations(new_iter, node.iter)
        node.iter = new_iter
        return node

    def is_starred(self, ob):
        if IS_PY3:
            return isinstance(ob, ast.Starred)
        else:
            return False

    def gen_unpack_spec(self, tpl):
        """Generate a specification for 'guarded_unpack_sequence'.

        This spec is used to protect sequence unpacking.
        The primary goal of this spec is to tell which elements in a sequence
        are sequences again. These 'child' sequences have to be protected
        again.

        For example there is a sequence like this:
            (a, (b, c), (d, (e, f))) = g

        On a higher level the spec says:
            - There is a sequence of len 3
            - The element at index 1 is a sequence again with len 2
            - The element at index 2 is a sequence again with len 2
              - The element at index 1 in this subsequence is a sequence again
                with len 2

        With this spec 'guarded_unpack_sequence' does something like this for
        protection (len checks are omitted):

            t = list(_getiter_(g))
            t[1] = list(_getiter_(t[1]))
            t[2] = list(_getiter_(t[2]))
            t[2][1] = list(_getiter_(t[2][1]))
            return t

        The 'real' spec for the case above is then:
            spec = {
                'min_len': 3,
                'childs': (
                    (1, {'min_len': 2, 'childs': ()}),
                    (2, {
                            'min_len': 2,
                            'childs': (
                                (1, {'min_len': 2, 'childs': ()})
                            )
                        }
                    )
                )
            }

        So finally the assignment above is converted into:
            (a, (b, c), (d, (e, f))) = guarded_unpack_sequence(g, spec)
        """
        spec = ast.Dict(keys=[], values=[])

        spec.keys.append(ast.Str('childs'))
        spec.values.append(ast.Tuple([], ast.Load()))

        # starred elements in a sequence do not contribute into the min_len.
        # For example a, b, *c = g
        # g must have at least 2 elements, not 3. 'c' is empyt if g has only 2.
        min_len = len([ob for ob in tpl.elts if not self.is_starred(ob)])
        offset = 0

        for idx, val in enumerate(tpl.elts):
            # After a starred element specify the child index from the back.
            # Since it is unknown how many elements from the sequence are
            # consumed by the starred element.
            # For example a, *b, (c, d) = g
            # Then (c, d) has the index '-1'
            if self.is_starred(val):
                offset = min_len + 1

            elif isinstance(val, ast.Tuple):
                el = ast.Tuple([], ast.Load())
                el.elts.append(ast.Num(idx - offset))
                el.elts.append(self.gen_unpack_spec(val))
                spec.values[0].elts.append(el)

        spec.keys.append(ast.Str('min_len'))
        spec.values.append(ast.Num(min_len))

        return spec

    def protect_unpack_sequence(self, target, value):
        spec = self.gen_unpack_spec(target)
        return ast.Call(
            func=ast.Name('_unpack_sequence_', ast.Load()),
            args=[value, spec, ast.Name('_getiter_', ast.Load())],
            keywords=[])

    def gen_unpack_wrapper(self, node, target, ctx='store'):
        """Helper function to protect tuple unpacks.

        node: used to copy the locations for the new nodes.
        target: is the tuple which must be protected.
        ctx: Defines the context of the returned temporary node.

        It returns a tuple with two element.

        Element 1: Is a temporary name node which must be used to
                   replace the target.
                   The context (store, param) is defined
                   by the 'ctx' parameter..

        Element 2: Is a try .. finally where the body performs the
                   protected tuple unpack of the temporary variable
                   into the original target.
        """

        # Generate a tmp name to replace the tuple with.
        tmp_name = self.gen_tmp_name()

        # Generates an expressions which protects the unpack.
        # converter looks like 'wrapper(tmp_name)'.
        # 'wrapper' takes care to protect sequence unpacking with _getiter_.
        converter = self.protect_unpack_sequence(
            target,
            ast.Name(tmp_name, ast.Load()))

        # Assign the expression to the original names.
        # Cleanup the temporary variable.
        # Generates:
        # try:
        #     # converter is 'wrapper(tmp_name)'
        #     arg = converter
        # finally:
        #     del tmp_arg
        try_body = [ast.Assign(targets=[target], value=converter)]
        finalbody = [self.gen_del_stmt(tmp_name)]

        if IS_PY2:
            cleanup = ast.TryFinally(body=try_body, finalbody=finalbody)
        else:
            cleanup = ast.Try(
                body=try_body, finalbody=finalbody, handlers=[], orelse=[])

        if ctx == 'store':
            ctx = ast.Store()
        elif ctx == 'param':
            ctx = ast.Param()
        else:
            raise Exception('Unsupported context type.')

        # This node is used to catch the tuple in a tmp variable.
        tmp_target = ast.Name(tmp_name, ctx)

        copy_locations(tmp_target, node)
        copy_locations(cleanup, node)

        return (tmp_target, cleanup)

    def gen_none_node(self):
        if IS_PY34_OR_GREATER:
            return ast.NameConstant(value=None)
        else:
            return ast.Name(id='None', ctx=ast.Load())

    def gen_lambda(self, args, body):
        return ast.Lambda(
            args=ast.arguments(
                args=args, vararg=None, kwarg=None, defaults=[]),
            body=body)

    def gen_del_stmt(self, name_to_del):
        return ast.Delete(targets=[ast.Name(name_to_del, ast.Del())])

    def transform_slice(self, slice_):
        """Transform slices into function parameters.

        ast.Slice nodes are only allowed within a ast.Subscript node.
        To use a slice as an argument of ast.Call it has to be converted.
        Conversion is done by calling the 'slice' function from builtins
        """

        if isinstance(slice_, ast.Index):
            return slice_.value

        elif isinstance(slice_, ast.Slice):
            # Create a python slice object.
            args = []

            if slice_.lower:
                args.append(slice_.lower)
            else:
                args.append(self.gen_none_node())

            if slice_.upper:
                args.append(slice_.upper)
            else:
                args.append(self.gen_none_node())

            if slice_.step:
                args.append(slice_.step)
            else:
                args.append(self.gen_none_node())

            return ast.Call(
                func=ast.Name('slice', ast.Load()),
                args=args,
                keywords=[])

        elif isinstance(slice_, ast.ExtSlice):
            dims = ast.Tuple([], ast.Load())
            for item in slice_.dims:
                dims.elts.append(self.transform_slice(item))
            return dims

        else:
            raise Exception("Unknown slice type: {0}".format(slice_))

    def check_name(self, node, name):
        if name is None:
            return

        if name.startswith('_') and name != '_':
            self.error(
                node,
                '"{name}" is an invalid variable name because it '
                'starts with "_"'.format(name=name))

        elif name.endswith('__roles__'):
            self.error(node, '"%s" is an invalid variable name because '
                       'it ends with "__roles__".' % name)

        elif name == "printed":
            self.error(node, '"printed" is a reserved name.')

        elif name == 'print':
            # Assignments to 'print' would lead to funny results.
            self.error(node, '"print" is a reserved name.')

    def check_function_argument_names(self, node):
        # In python3 arguments are always identifiers.
        # In python2 the 'Python.asdl' specifies expressions, but
        # the python grammer allows only identifiers or a tuple of
        # identifiers. If its a tuple 'tuple parameter unpacking' is used,
        # which is gone in python3.
        # See https://www.python.org/dev/peps/pep-3113/

        if IS_PY2:
            # Needed to handle nested 'tuple parameter unpacking'.
            # For example 'def foo((a, b, (c, (d, e)))): pass'
            to_check = list(node.args.args)
            while to_check:
                item = to_check.pop()
                if isinstance(item, ast.Tuple):
                    to_check.extend(item.elts)
                else:
                    self.check_name(node, item.id)

            self.check_name(node, node.args.vararg)
            self.check_name(node, node.args.kwarg)

        else:
            for arg in node.args.args:
                self.check_name(node, arg.arg)

            if node.args.vararg:
                self.check_name(node, node.args.vararg.arg)

            if node.args.kwarg:
                self.check_name(node, node.args.kwarg.arg)

            for arg in node.args.kwonlyargs:
                self.check_name(node, arg.arg)

    def check_import_names(self, node):
        """Check the names being imported.

        This is a protection against rebinding dunder names like
        _getitem_, _write_ via imports.

        => 'from _a import x' is ok, because '_a' is not added to the scope.
        """
        for alias in node.names:
            self.check_name(node, alias.name)
            if alias.asname:
                self.check_name(node, alias.asname)

        return self.node_contents_visit(node)

    def inject_print_collector(self, node, position=0):
        print_used = self.print_info.print_used
        printed_used = self.print_info.printed_used

        if print_used or printed_used:
            # Add '_print = _print_(_getattr_)' add the top of a
            # function/module.
            _print = ast.Assign(
                targets=[ast.Name('_print', ast.Store())],
                value=ast.Call(
                    func=ast.Name("_print_", ast.Load()),
                    args=[ast.Name("_getattr_", ast.Load())],
                    keywords=[]))

            if isinstance(node, ast.Module):
                _print.lineno = position
                _print.col_offset = position
                ast.fix_missing_locations(_print)
            else:
                copy_locations(_print, node)

            node.body.insert(position, _print)

            if not printed_used:
                self.warn(node, "Prints, but never reads 'printed' variable.")

            elif not print_used:
                self.warn(node, "Doesn't print, but reads 'printed' variable.")

    def gen_attr_check(self, node, attr_name):
        """Check if 'attr_name' is allowed on the object in node.

        It generates (_getattr_(node, attr_name) and node).
        """

        call_getattr = ast.Call(
            func=ast.Name('_getattr_', ast.Load()),
            args=[node, ast.Str(attr_name)],
            keywords=[])

        return ast.BoolOp(op=ast.And(), values=[call_getattr, node])

    # Special Functions for an ast.NodeTransformer

    def generic_visit(self, node):
        """Reject ast nodes which do not have a corresponding `visit_` method.

        This is needed to prevent new ast nodes from new Python versions to be
        trusted before any security review.

        To access `generic_visit` on the super class use `node_contents_visit`.
        """
        # TODO: To be discussed - For whom that info is relevant
        # import warnings
        # warnings.warn(
        #     '{o.__class__.__name__}'
        #     ' statement is not known to RestrictedPython'.format(node),
        #     SyntaxWarning
        # )
        self.warn(
            node,
            '{0.__class__.__name__}'
            ' statement is not known to RestrictedPython'.format(node)
        )
        self.not_allowed(node)

    def not_allowed(self, node):
        self.error(
            node,
            '{0.__class__.__name__} statements are not allowed.'.format(node))

    def node_contents_visit(self, node):
        """Visit the contents of a node."""
        return super(RestrictingNodeTransformer, self).generic_visit(node)

    # ast for Literals

    def visit_Num(self, node):
        """Allow integer numbers without restrictions."""
        return self.node_contents_visit(node)

    def visit_Str(self, node):
        """Allow string literals without restrictions."""
        return self.node_contents_visit(node)

    def visit_Bytes(self, node):
        """Allow bytes literals without restrictions.

        Bytes is Python 3 only.
        """
        return self.node_contents_visit(node)

    def visit_List(self, node):
        """Allow list literals without restrictions."""
        return self.node_contents_visit(node)

    def visit_Tuple(self, node):
        """Allow tuple literals without restrictions."""
        return self.node_contents_visit(node)

    def visit_Set(self, node):
        """Allow set literals without restrictions."""
        return self.node_contents_visit(node)

    def visit_Dict(self, node):
        """Allow dict literals without restrictions."""
        return self.node_contents_visit(node)

    def visit_Ellipsis(self, node):
        """Deny using `...`.

        Ellipsis is exists only in Python 3.
        """
        self.not_allowed(node)

    def visit_NameConstant(self, node):
        """

        """
        return self.node_contents_visit(node)

    # ast for Variables

    def visit_Name(self, node):
        """Prevents access to protected names.

        Converts use of the name 'printed' to this expression: '_print()'
        """

        node = self.node_contents_visit(node)

        if isinstance(node.ctx, ast.Load):
            if node.id == 'printed':
                self.print_info.printed_used = True
                new_node = ast.Call(
                    func=ast.Name("_print", ast.Load()),
                    args=[],
                    keywords=[])

                copy_locations(new_node, node)
                return new_node

            elif node.id == 'print':
                self.print_info.print_used = True
                new_node = ast.Attribute(
                    value=ast.Name('_print', ast.Load()),
                    attr="_call_print",
                    ctx=ast.Load())

                copy_locations(new_node, node)
                return new_node

            self.used_names[node.id] = True

        self.check_name(node, node.id)
        return node

    def visit_Load(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_Store(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_Del(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_Starred(self, node):
        """

        """
        return self.node_contents_visit(node)

    # Expressions

    def visit_Expression(self, node):
        """Allow Expression statements without restrictions.

        They are in the AST when using the `eval` compile mode.
        """
        return self.node_contents_visit(node)

    def visit_Expr(self, node):
        """Allow Expr statements (any expression) without restrictions."""
        return self.node_contents_visit(node)

    def visit_UnaryOp(self, node):
        """
        UnaryOp (Unary Operations) is the overall element for:
        * Not --> which should be allowed
        * UAdd --> Positive notation of variables (e.g. +var)
        * USub --> Negative notation of variables (e.g. -var)
        """
        return self.node_contents_visit(node)

    def visit_UAdd(self, node):
        """Allow positive notation of variables. (e.g. +var)"""
        return self.node_contents_visit(node)

    def visit_USub(self, node):
        """Allow negative notation of variables. (e.g. -var)"""
        return self.node_contents_visit(node)

    def visit_Not(self, node):
        """Allow the `not` operator."""
        return self.node_contents_visit(node)

    def visit_Invert(self, node):
        """Allow `~` expressions."""
        return self.node_contents_visit(node)

    def visit_BinOp(self, node):
        """Allow binary operations."""
        return self.node_contents_visit(node)

    def visit_Add(self, node):
        """Allow `+` expressions."""
        return self.node_contents_visit(node)

    def visit_Sub(self, node):
        """Allow `-` expressions."""
        return self.node_contents_visit(node)

    def visit_Mult(self, node):
        """Allow `*` expressions."""
        return self.node_contents_visit(node)

    def visit_Div(self, node):
        """Allow `/` expressions."""
        return self.node_contents_visit(node)

    def visit_FloorDiv(self, node):
        """Allow `//` expressions."""
        return self.node_contents_visit(node)

    def visit_Mod(self, node):
        """Allow `%` expressions."""
        return self.node_contents_visit(node)

    def visit_Pow(self, node):
        """Allow `**` expressions."""
        return self.node_contents_visit(node)

    def visit_LShift(self, node):
        """Allow `<<` expressions."""
        return self.node_contents_visit(node)

    def visit_RShift(self, node):
        """Allow `>>` expressions."""
        return self.node_contents_visit(node)

    def visit_BitOr(self, node):
        """Allow `|` expressions."""
        return self.node_contents_visit(node)

    def visit_BitXor(self, node):
        """Allow `^` expressions."""
        return self.node_contents_visit(node)

    def visit_BitAnd(self, node):
        """Allow `&` expressions."""
        return self.node_contents_visit(node)

    def visit_MatMult(self, node):
        """Matrix multiplication (`@`) is currently not allowed.

        Matrix multiplication is a Python 3.5+ feature.
        """
        self.not_allowed(node)

    def visit_BoolOp(self, node):
        """Allow bool operator without restrictions."""
        return self.node_contents_visit(node)

    def visit_And(self, node):
        """Allow bool operator `and` without restrictions."""
        return self.node_contents_visit(node)

    def visit_Or(self, node):
        """Allow bool operator `or` without restrictions."""
        return self.node_contents_visit(node)

    def visit_Compare(self, node):
        """Allow comparison expressions without restrictions."""
        return self.node_contents_visit(node)

    def visit_Eq(self, node):
        """Allow == expressions."""
        return self.node_contents_visit(node)

    def visit_NotEq(self, node):
        """Allow != expressions."""
        return self.node_contents_visit(node)

    def visit_Lt(self, node):
        """Allow < expressions."""
        return self.node_contents_visit(node)

    def visit_LtE(self, node):
        """Allow <= expressions."""
        return self.node_contents_visit(node)

    def visit_Gt(self, node):
        """Allow > expressions."""
        return self.node_contents_visit(node)

    def visit_GtE(self, node):
        """Allow >= expressions."""
        return self.node_contents_visit(node)

    def visit_Is(self, node):
        """Allow `is` expressions."""
        return self.node_contents_visit(node)

    def visit_IsNot(self, node):
        """Allow `is not` expressions."""
        return self.node_contents_visit(node)

    def visit_In(self, node):
        """Allow `in` expressions."""
        return self.node_contents_visit(node)

    def visit_NotIn(self, node):
        """Allow `not in` expressions."""
        return self.node_contents_visit(node)

    def visit_Call(self, node):
        """Checks calls with '*args' and '**kwargs'.

        Note: The following happens only if '*args' or '**kwargs' is used.

        Transfroms 'foo(<all the possible ways of args>)' into
        _apply_(foo, <all the possible ways for args>)

        The thing is that '_apply_' has only '*args', '**kwargs', so it gets
        Python to collapse all the myriad ways to call functions
        into one manageable from.

        From there, '_apply_()' wraps args and kws in guarded accessors,
        then calls the function, returning the value.
        """

        if isinstance(node.func, ast.Name):
            if node.func.id == 'exec':
                self.error(node, 'Exec calls are not allowed.')
            elif node.func.id == 'eval':
                self.error(node, 'Eval calls are not allowed.')

        needs_wrap = False

        # In python2.7 till python3.4 '*args', '**kwargs' have dedicated
        # attributes on the ast.Call node.
        # In python 3.5 and greater this has changed due to the fact that
        # multiple '*args' and '**kwargs' are possible.
        # '*args' can be detected by 'ast.Starred' nodes.
        # '**kwargs' can be deteced by 'keyword' nodes with 'arg=None'.

        if IS_PY35_OR_GREATER:
            for pos_arg in node.args:
                if isinstance(pos_arg, ast.Starred):
                    needs_wrap = True

            for keyword_arg in node.keywords:
                if keyword_arg.arg is None:
                    needs_wrap = True
        else:
            if (node.starargs is not None) or (node.kwargs is not None):
                needs_wrap = True

        node = self.node_contents_visit(node)

        if not needs_wrap:
            return node

        node.args.insert(0, node.func)
        node.func = ast.Name('_apply_', ast.Load())
        copy_locations(node.func, node.args[0])
        return node

    def visit_keyword(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_IfExp(self, node):
        """Allow `if` expressions without restrictions."""
        return self.node_contents_visit(node)

    def visit_Attribute(self, node):
        """Checks and mutates attribute access/assignment.

        'a.b' becomes '_getattr_(a, "b")'
        'a.b = c' becomes '_write_(a).b = c'
        'del a.b' becomes 'del _write_(a).b'

        The _write_ function should return a security proxy.
        """
        if node.attr.startswith('_') and node.attr != '_':
            self.error(
                node,
                '"{name}" is an invalid attribute name because it starts '
                'with "_".'.format(name=node.attr))

        if node.attr.endswith('__roles__'):
            self.error(
                node,
                '"{name}" is an invalid attribute name because it ends '
                'with "__roles__".'.format(name=node.attr))

        if isinstance(node.ctx, ast.Load):
            node = self.node_contents_visit(node)
            new_node = ast.Call(
                func=ast.Name('_getattr_', ast.Load()),
                args=[node.value, ast.Str(node.attr)],
                keywords=[])

            copy_locations(new_node, node)
            return new_node

        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            node = self.node_contents_visit(node)
            new_value = ast.Call(
                func=ast.Name('_write_', ast.Load()),
                args=[node.value],
                keywords=[])

            copy_locations(new_value, node.value)
            node.value = new_value
            return node

        else:
            return self.node_contents_visit(node)

    # Subscripting

    def visit_Subscript(self, node):
        """Transforms all kinds of subscripts.

        'foo[bar]' becomes '_getitem_(foo, bar)'
        'foo[:ab]' becomes '_getitem_(foo, slice(None, ab, None))'
        'foo[ab:]' becomes '_getitem_(foo, slice(ab, None, None))'
        'foo[a:b]' becomes '_getitem_(foo, slice(a, b, None))'
        'foo[a:b:c]' becomes '_getitem_(foo, slice(a, b, c))'
        'foo[a, b:c] becomes '_getitem_(foo, (a, slice(b, c, None)))'
        'foo[a] = c' becomes '_write(foo)[a] = c'
        'del foo[a]' becomes 'del _write_(foo)[a]'

        The _write_ function should return a security proxy.
        """
        node = self.node_contents_visit(node)

        # 'AugStore' and 'AugLoad' are defined in 'Python.asdl' as possible
        # 'expr_context'. However, according to Python/ast.c
        # they are NOT used by the implementation => No need to worry here.
        # Instead ast.c creates 'AugAssign' nodes, which can be visited.

        if isinstance(node.ctx, ast.Load):
            new_node = ast.Call(
                func=ast.Name('_getitem_', ast.Load()),
                args=[node.value, self.transform_slice(node.slice)],
                keywords=[])

            copy_locations(new_node, node)
            return new_node

        elif isinstance(node.ctx, (ast.Del, ast.Store)):
            new_value = ast.Call(
                func=ast.Name('_write_', ast.Load()),
                args=[node.value],
                keywords=[])

            copy_locations(new_value, node)
            node.value = new_value
            return node

        else:
            return node

    def visit_Index(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_Slice(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_ExtSlice(self, node):
        """

        """
        return self.node_contents_visit(node)

    # Comprehensions

    def visit_ListComp(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_SetComp(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_GeneratorExp(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_DictComp(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_comprehension(self, node):
        """

        """
        return self.guard_iter(node)

    # Statements

    def visit_Assign(self, node):
        """

        """

        node = self.node_contents_visit(node)

        if not any(isinstance(t, ast.Tuple) for t in node.targets):
            return node

        # Handle sequence unpacking.
        # For briefness this example omits cleanup of the temporary variables.
        # Check 'transform_tuple_assign' how its done.
        #
        # - Single target (with nested support)
        # (a, (b, (c, d))) = <exp>
        # is converted to
        # (a, t1) = _getiter_(<exp>)
        # (b, t2) = _getiter_(t1)
        # (c, d) = _getiter_(t2)
        #
        # - Multi targets
        # (a, b) = (c, d) = <exp>
        # is converted to
        # (c, d) = _getiter_(<exp>)
        # (a, b) = _getiter_(<exp>)
        # Why is this valid ? The original bytecode for this multi targets
        # behaves the same way.

        # ast.NodeTransformer works with list results.
        # He injects it at the right place of the node's parent statements.
        new_nodes = []

        # python fills the right most target first.
        for target in reversed(node.targets):
            if isinstance(target, ast.Tuple):
                wrapper = ast.Assign(
                    targets=[target],
                    value=self.protect_unpack_sequence(target, node.value))
                new_nodes.append(wrapper)
            else:
                new_node = ast.Assign(targets=[target], value=node.value)
                new_nodes.append(new_node)

        for new_node in new_nodes:
            copy_locations(new_node, node)

        return new_nodes

    def visit_AugAssign(self, node):
        """Forbid certain kinds of AugAssign

        According to the language reference (and ast.c) the following nodes
        are are possible:
        Name, Attribute, Subscript

        Note that although augmented assignment of attributes and
        subscripts is disallowed, augmented assignment of names (such
        as 'n += 1') is allowed.
        'n += 1' becomes 'n = _inplacevar_("+=", n, 1)'
        """

        node = self.node_contents_visit(node)

        if isinstance(node.target, ast.Attribute):
            self.error(
                node,
                "Augmented assignment of attributes is not allowed.")
            return node

        elif isinstance(node.target, ast.Subscript):
            self.error(
                node,
                "Augmented assignment of object items "
                "and slices is not allowed.")
            return node

        elif isinstance(node.target, ast.Name):
            new_node = ast.Assign(
                targets=[node.target],
                value=ast.Call(
                    func=ast.Name('_inplacevar_', ast.Load()),
                    args=[
                        ast.Str(IOPERATOR_TO_STR[type(node.op)]),
                        ast.Name(node.target.id, ast.Load()),
                        node.value
                    ],
                    keywords=[]))

            copy_locations(new_node, node)
            return new_node

        return node

    def visit_Print(self, node):
        """Checks and mutates a print statement.

        Adds a target to all print statements.  'print foo' becomes
        'print >> _print, foo', where _print is the default print
        target defined for this scope.

        Alternatively, if the untrusted code provides its own target,
        we have to check the 'write' method of the target.
        'print >> ob, foo' becomes
        'print >> (_getattr_(ob, 'write') and ob), foo'.
        Otherwise, it would be possible to call the write method of
        templates and scripts; 'write' happens to be the name of the
        method that changes them.
        """

        self.print_info.print_used = True
        self.warn(node,
                  "Print statement is deprecated and "
                  "not avaliable anymore in Python 3.")

        node = self.node_contents_visit(node)
        if node.dest is None:
            node.dest = ast.Name('_print', ast.Load())
        else:
            # Pre-validate access to the 'write' attribute.
            node.dest = self.gen_attr_check(node.dest, 'write')

        copy_locations(node.dest, node)
        return node

    def visit_Raise(self, node):
        """Allow `raise` statements without restrictions."""
        return self.node_contents_visit(node)

    def visit_Assert(self, node):
        """Allow assert statements without restrictions."""
        return self.node_contents_visit(node)

    def visit_Delete(self, node):
        """Allow `del` statements without restrictions."""
        return self.node_contents_visit(node)

    def visit_Pass(self, node):
        """Allow `pass` statements without restrictions."""
        return self.node_contents_visit(node)

    # Imports

    def visit_Import(self, node):
        """Allow `import` statements with restrictions.
        See check_import_names."""
        return self.check_import_names(node)

    def visit_ImportFrom(self, node):
        """Allow `import from` statements with restrictions.
        See check_import_names."""
        return self.check_import_names(node)

    def visit_alias(self, node):
        """Allow `as` statements in import and import from statements."""
        return self.node_contents_visit(node)

    def visit_Exec(self, node):
        """Deny the usage of the exec statement.

        Exists only in Python 2.
        """
        self.not_allowed(node)

    # Control flow

    def visit_If(self, node):
        """Allow `if` statements without restrictions."""
        return self.node_contents_visit(node)

    def visit_For(self, node):
        """Allow `for` statements with some restrictions."""
        return self.guard_iter(node)

    def visit_While(self, node):
        """Allow `while` statements."""
        return self.node_contents_visit(node)

    def visit_Break(self, node):
        """Allow `break` statements without restrictions."""
        return self.node_contents_visit(node)

    def visit_Continue(self, node):
        """Allow `continue` statements without restrictions."""
        return self.node_contents_visit(node)

    def visit_Try(self, node):
        """Allow `try` without restrictions.

        This is Python 3 only, Python 2 uses TryExcept.
        """
        return self.node_contents_visit(node)

    def visit_TryFinally(self, node):
        """Allow `try ... finally` without restrictions."""
        return self.node_contents_visit(node)

    def visit_TryExcept(self, node):
        """Allow `try ... except` without restrictions."""
        return self.node_contents_visit(node)

    def visit_ExceptHandler(self, node):
        """Protect tuple unpacking on exception handlers.

        try:
            .....
        except Exception as (a, b):
            ....

        becomes

        try:
            .....
        except Exception as tmp:
            try:
                (a, b) = _getiter_(tmp)
            finally:
                del tmp
        """
        node = self.node_contents_visit(node)

        if IS_PY3:
            self.check_name(node, node.name)
            return node

        if not isinstance(node.name, ast.Tuple):
            return node

        tmp_target, unpack = self.gen_unpack_wrapper(node, node.name)

        # Replace the tuple with the temporary variable.
        node.name = tmp_target

        # Insert the unpack code within the body of the except clause.
        node.body.insert(0, unpack)

        return node

    def visit_With(self, node):
        """Protect tuple unpacking on with statements."""
        node = self.node_contents_visit(node)

        if IS_PY2:
            items = [node]
        else:
            items = node.items

        for item in reversed(items):
            if isinstance(item.optional_vars, ast.Tuple):
                tmp_target, unpack = self.gen_unpack_wrapper(
                    node,
                    item.optional_vars)

                item.optional_vars = tmp_target
                node.body.insert(0, unpack)

        return node

    def visit_withitem(self, node):
        """Allow `with` statements (context managers) without restrictions."""
        return self.node_contents_visit(node)

    # Function and class definitions

    def visit_FunctionDef(self, node):
        """Allow function definitions (`def`) with some restrictions."""
        self.check_name(node, node.name)
        self.check_function_argument_names(node)

        with self.print_info.new_print_scope():
            node = self.node_contents_visit(node)
            self.inject_print_collector(node)

        if IS_PY3:
            return node

        # Protect 'tuple parameter unpacking' with '_getiter_'.

        unpacks = []
        for index, arg in enumerate(list(node.args.args)):
            if isinstance(arg, ast.Tuple):
                tmp_target, unpack = self.gen_unpack_wrapper(
                    node, arg, 'param')

                # Replace the tuple with a single (temporary) parameter.
                node.args.args[index] = tmp_target
                unpacks.append(unpack)

        # Add the unpacks at the front of the body.
        # Keep the order, so that tuple one is unpacked first.
        node.body[0:0] = unpacks
        return node

    def visit_Lambda(self, node):
        """Allow lambda with some restrictions."""
        self.check_function_argument_names(node)

        node = self.node_contents_visit(node)

        if IS_PY3:
            return node

        # Check for tuple parameters which need _getiter_ protection
        if not any(isinstance(arg, ast.Tuple) for arg in node.args.args):
            return node

        # Wrap this lambda function with another. Via this wrapping it is
        # possible to protect the 'tuple arguments' with _getiter_
        outer_params = []
        inner_args = []

        for arg in node.args.args:
            if isinstance(arg, ast.Tuple):
                tmp_name = self.gen_tmp_name()
                converter = self.protect_unpack_sequence(
                    arg,
                    ast.Name(tmp_name, ast.Load()))

                outer_params.append(ast.Name(tmp_name, ast.Param()))
                inner_args.append(converter)

            else:
                outer_params.append(arg)
                inner_args.append(ast.Name(arg.id, ast.Load()))

        body = ast.Call(func=node, args=inner_args, keywords=[])
        new_node = self.gen_lambda(outer_params, body)

        if node.args.vararg:
            new_node.args.vararg = node.args.vararg
            body.starargs = ast.Name(node.args.vararg, ast.Load())

        if node.args.kwarg:
            new_node.args.kwarg = node.args.kwarg
            body.kwargs = ast.Name(node.args.kwarg, ast.Load())

        copy_locations(new_node, node)
        return new_node

    def visit_arguments(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_arg(self, node):
        """

        """
        return self.node_contents_visit(node)

    def visit_Return(self, node):
        """Allow `return` statements without restrictions."""
        return self.node_contents_visit(node)

    def visit_Yield(self, node):
        """Deny `yield` unconditionally."""
        self.not_allowed(node)

    def visit_YieldFrom(self, node):
        """Deny `yield from` unconditionally."""
        self.not_allowed(node)

    def visit_Global(self, node):
        """Allow `global` statements without restrictions."""
        return self.node_contents_visit(node)

    def visit_Nonlocal(self, node):
        """Deny `nonlocal` statements.

        This statement was introduced in Python 3.
        """
        # TODO: Review if we want to allow it later
        self.not_allowed(node)

    def visit_ClassDef(self, node):
        """Check the name of a class definition."""
        self.check_name(node, node.name)
        node = self.node_contents_visit(node)
        if IS_PY2:
            new_class_node = node
        else:
            if any(keyword.arg == 'metaclass' for keyword in node.keywords):
                self.error(
                    node, 'The keyword argument "metaclass" is not allowed.')
            CLASS_DEF = textwrap.dedent('''\
                class {0.name}(metaclass=__metaclass__):
                    pass
            '''.format(node))
            new_class_node = ast.parse(CLASS_DEF).body[0]
            new_class_node.body = node.body
            new_class_node.bases = node.bases
            new_class_node.decorator_list = node.decorator_list
        return new_class_node

    def visit_Module(self, node):
        """Add the print_collector (only if print is used) at the top."""
        node = self.node_contents_visit(node)

        # Inject the print collector after 'from __future__ import ....'
        position = 0
        for position, child in enumerate(node.body):
            if not isinstance(child, ast.ImportFrom):
                break

            if not child.module == '__future__':
                break

        self.inject_print_collector(node, position)
        return node

    def visit_Param(self, node):
        """Allow parameters without restrictions."""
        return self.node_contents_visit(node)

    # Async und await

    def visit_AsyncFunctionDef(self, node):
        """Deny async functions."""
        self.not_allowed(node)

    def visit_Await(self, node):
        """Deny async functionality."""
        self.not_allowed(node)

    def visit_AsyncFor(self, node):
        """Deny async functionality."""
        self.not_allowed(node)

    def visit_AsyncWith(self, node):
        """Deny async functionality."""
        self.not_allowed(node)