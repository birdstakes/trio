#! /usr/bin/env python3
"""
Code generation script for class methods
to be exported as public API
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from textwrap import indent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing_extensions import TypeGuard

import astor
import attr

PREFIX = "_generated"

HEADER = """# ***********************************************************
# ******* WARNING: AUTOGENERATED! ALL EDITS WILL BE LOST ******
# *************************************************************
# Don't lint this file, generation will not format this too nicely.
# isort: skip_file
# fmt: off
from __future__ import annotations

from ._ki import LOCALS_KEY_KI_PROTECTION_ENABLED
from ._run import GLOBAL_RUN_CONTEXT
"""

FOOTER = """# fmt: on
"""

TEMPLATE = """locals()[LOCALS_KEY_KI_PROTECTION_ENABLED] = True
try:
    return{}GLOBAL_RUN_CONTEXT.{}.{}
except AttributeError:
    raise RuntimeError("must be called from async context")
"""


@attr.define
class File:
    path: Path
    modname: str
    platform: str = attr.field(default="", kw_only=True)
    imports: str = attr.field(default="", kw_only=True)


def is_function(node: ast.AST) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Check if the AST node is either a function
    or an async function
    """
    if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
        return True
    return False


def is_public(node: ast.AST) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Check if the AST node has a _public decorator"""
    if is_function(node):
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name) and decorator.id == "_public":
                return True
    return False


def get_public_methods(
    tree: ast.AST,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return a list of methods marked as public.
    The function walks the given tree and extracts
    all objects that are functions which are marked
    public.
    """
    for node in ast.walk(tree):
        if is_public(node):
            yield node


def create_passthrough_args(funcdef: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Given a function definition, create a string that represents taking all
    the arguments from the function, and passing them through to another
    invocation of the same function.

    Example input: ast.parse("def f(a, *, b): ...")
    Example output: "(a, b=b)"
    """
    call_args = []
    for arg in funcdef.args.args:
        call_args.append(arg.arg)
    if funcdef.args.vararg:
        call_args.append("*" + funcdef.args.vararg.arg)
    for arg in funcdef.args.kwonlyargs:
        call_args.append(arg.arg + "=" + arg.arg)
    if funcdef.args.kwarg:
        call_args.append("**" + funcdef.args.kwarg.arg)
    return "({})".format(", ".join(call_args))


def gen_public_wrappers_source(file: File) -> str:
    """Scan the given .py file for @_public decorators, and generate wrapper
    functions.

    """
    header = [HEADER]

    if file.imports:
        header.append(file.imports)
    if file.platform:
        # Simple checks to avoid repeating imports. If this messes up, type checkers/tests will
        # just give errors.
        if "TYPE_CHECKING" not in file.imports:
            header.append("from typing import TYPE_CHECKING\n")
        if "import sys" not in file.imports:  # pragma: no cover
            header.append("import sys\n")
        header.append(
            f'\nassert not TYPE_CHECKING or sys.platform=="{file.platform}"\n'
        )

    generated = ["".join(header)]

    source = astor.code_to_ast.parse_file(file.path)
    for method in get_public_methods(source):
        # Remove self from arguments
        assert method.args.args[0].arg == "self"
        del method.args.args[0]

        for dec in method.decorator_list:  # pragma: no cover
            if isinstance(dec, ast.Name) and dec.id == "contextmanager":
                is_cm = True
                break
        else:
            is_cm = False

        # Remove decorators
        method.decorator_list = []

        # Create pass through arguments
        new_args = create_passthrough_args(method)

        # Remove method body without the docstring
        if ast.get_docstring(method) is None:
            del method.body[:]
        else:
            # The first entry is always the docstring
            del method.body[1:]

        # Create the function definition including the body
        func = astor.to_source(method, indent_with=" " * 4)

        if is_cm:  # pragma: no cover
            func = func.replace("->Iterator", "->ContextManager")

        # TODO: hacky workaround until we run mypy without `-m`, which breaks imports
        # enough that it cannot figure out the type of _NO_SEND
        if file.path.stem == "_run" and func.startswith(
            "def reschedule"
        ):  # pragma: no cover
            func = func.replace("None:\n", "None:  # type: ignore[has-type]\n")

        # Create export function body
        template = TEMPLATE.format(
            " await " if isinstance(method, ast.AsyncFunctionDef) else " ",
            file.modname,
            method.name + new_args,
        )

        # Assemble function definition arguments and body
        snippet = func + indent(template, " " * 4)

        # Append the snippet to the corresponding module
        generated.append(snippet)
    generated.append(FOOTER)
    return "\n\n".join(generated)


def matches_disk_files(new_files: dict[str, str]) -> bool:
    for new_path, new_source in new_files.items():
        if not os.path.exists(new_path):
            return False
        with open(new_path, encoding="utf-8") as old_file:
            old_source = old_file.read()
        if old_source != new_source:
            return False
    return True


def process(files: Iterable[File], *, do_test: bool) -> None:
    new_files = {}
    for file in files:
        print("Scanning:", file.path)
        new_source = gen_public_wrappers_source(file)
        dirname, basename = os.path.split(file.path)
        new_path = os.path.join(dirname, PREFIX + basename)
        new_files[new_path] = new_source
    if do_test:
        if not matches_disk_files(new_files):
            print("Generated sources are outdated. Please regenerate.")
            sys.exit(1)
        else:
            print("Generated sources are up to date.")
    else:
        for new_path, new_source in new_files.items():
            with open(new_path, "w", encoding="utf-8") as f:
                f.write(new_source)
        print("Regenerated sources successfully.")


# This is in fact run in CI, but only in the formatting check job, which
# doesn't collect coverage.
def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Generate python code for public api wrappers"
    )
    parser.add_argument(
        "--test", "-t", action="store_true", help="test if code is still up to date"
    )
    parsed_args = parser.parse_args()

    source_root = Path.cwd()
    # Double-check we found the right directory
    assert (source_root / "LICENSE").exists()
    core = source_root / "trio/_core"
    to_wrap = [
        File(core / "_run.py", "runner", imports=IMPORTS_RUN),
        File(
            core / "_instrumentation.py",
            "runner.instruments",
            imports=IMPORTS_INSTRUMENT,
        ),
        File(core / "_io_windows.py", "runner.io_manager", platform="win32"),
        File(
            core / "_io_epoll.py",
            "runner.io_manager",
            platform="linux",
            imports=IMPORTS_EPOLL,
        ),
        File(
            core / "_io_kqueue.py",
            "runner.io_manager",
            platform="darwin",
            imports=IMPORTS_KQUEUE,
        ),
    ]

    process(to_wrap, do_test=parsed_args.test)


IMPORTS_RUN = """\
from collections.abc import Awaitable, Callable
from typing import Any

from outcome import Outcome
import contextvars

from ._run import _NO_SEND, RunStatistics, Task
from ._entry_queue import TrioToken
from .._abc import Clock
"""
IMPORTS_INSTRUMENT = """\
from ._instrumentation import Instrument
"""

IMPORTS_EPOLL = """\
from socket import socket
"""

IMPORTS_KQUEUE = """\
from typing import Callable, ContextManager, TYPE_CHECKING

if TYPE_CHECKING:
    import select
    from socket import socket

    from ._traps import Abort, RaiseCancelT

    from .. import _core

"""


if __name__ == "__main__":  # pragma: no cover
    main()
