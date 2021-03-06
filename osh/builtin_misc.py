#!/usr/bin/env python2
# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
builtin_misc.py - Misc builtins.
"""
from __future__ import print_function

import sys
import termios  # for read -n

from _devbuild.gen.runtime_asdl import (
    value_e, scope_e, span_e, cmd_value_t, cmd_value__Argv
)
from asdl import runtime
from core import state
from core import ui
from core.util import log
from frontend import args
from frontend import arg_def
from mycpp import mylib
from pylib import os_path

import libc
import posix_ as posix

if mylib.PYTHON:
  # Hack because we don't want libcmark.so dependency for build/dev.sh minimal
  try:
    from _devbuild.gen import help_index  # generated file
  except ImportError:
    help_index = None

from typing import Any, Optional, IO, TYPE_CHECKING
if TYPE_CHECKING:
  from _devbuild.gen.runtime_asdl import value__Str
  from core.pyutil import _FileResourceLoader
  from core.state import Mem, DirStack
  from core.ui import ErrorFormatter
  from osh.cmd_eval import CommandEvaluator
  from osh.split import SplitContext

_ = log

#
# Abstract base class
#

class _Builtin(object):
  """All builtins except 'command' obey this interface.

  Assignment builtins use cmd_value__Assign; others use cmd_value__Argv.
  """
  def Run(self, cmd_val):
    # type: (cmd_value_t) -> int
    raise NotImplementedError()

#
# Implementation of builtins.
#


if mylib.PYTHON:
  TIMES_SPEC = arg_def.Register('times')

class Times(_Builtin):
  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    utime, stime, cutime, cstime, elapsed = posix.times()
    print("%dm%1.3fs %dm%1.3fs" % (utime / 60, utime % 60, stime / 60, stime % 60))
    print("%dm%1.3fs %dm%1.3fs" % (cutime / 60, cutime % 60, cstime / 60, cstime % 60))

    return 0


# The Read builtin splits using IFS.
#
# Summary:
# - Split with IFS, except \ can escape them!  This is different than the
#   algorithm for splitting words (at least the way I've represented it.)

# Bash manual:
# - If there are more words than names, the remaining words and their
#   intervening delimiters are assigned to the last name.
# - If there are fewer words read from the input stream than names, the
#   remaining names are assigned empty values.
# - The characters in the value of the IFS variable are used to split the line
#   into words using the same rules the shell uses for expansion (described
# above in Word Splitting).
# - The backslash character '\' may be used to remove any special meaning for
#   the next character read and for line continuation.

def _AppendParts(s, spans, max_results, join_next, parts):
  """
  Args:
    s: The original string
    spans: List of (span, end_index)
    max_results: the maximum number of parts we want
    join_next: Whether to join the next span to the previous part.  This
    happens in two cases:
      - when we have '\ '
      - and when we have more spans # than max_results.
  """
  start_index = 0
  # If the last span was black, and we get a backslash, set join_next to merge
  # two black spans.
  last_span_was_black = False

  for span_type, end_index in spans:
    if span_type == span_e.Black:
      if join_next and parts:
        parts[-1] += s[start_index:end_index]
        join_next = False
      else:
        parts.append(s[start_index:end_index])
      last_span_was_black = True

    elif span_type == span_e.Delim:
      if join_next:
        parts[-1] += s[start_index:end_index]
        join_next = False
      last_span_was_black = False

    elif span_type == span_e.Backslash:
      if last_span_was_black:
        join_next = True
      last_span_was_black = False

    if max_results and len(parts) >= max_results:
      join_next = True

    start_index = end_index

  done = True
  if spans:
    #log('%s %s', s, spans)
    #log('%s', spans[-1])
    last_span_type, _ = spans[-1]
    if last_span_type == span_e.Backslash:
      done = False

  #log('PARTS %s', parts)
  return done, join_next


if mylib.PYTHON:
  READ_SPEC = arg_def.Register('read')
  READ_SPEC.ShortFlag('-r')
  READ_SPEC.ShortFlag('-n', args.Int)
  READ_SPEC.ShortFlag('-a', args.Str)  # name of array to read into
  READ_SPEC.ShortFlag('-d', args.Str)


# sys.stdin.readline() in Python has buffering!  TODO: Rewrite this tight loop
# in C?  Less garbage probably.
# NOTE that dash, mksh, and zsh all read a single byte at a time.  It appears
# to be required by POSIX?  Could try libc getline and make this an option.
def ReadLineFromStdin(delim_char):
  # type: (Optional[str]) -> str
  """Read a line, or read up until delim_char if set."""
  chars = []
  while True:
    c = posix.read(0, 1)
    if not c:
      break

    if c == delim_char:
      break

    chars.append(c)

    if c == '\n':
      break
  return ''.join(chars)


class Read(object):
  def __init__(self, splitter, mem):
    # type: (SplitContext, Mem) -> None
    self.splitter = splitter
    self.mem = mem

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    arg, i = READ_SPEC.ParseCmdVal(cmd_val)
    names = cmd_val.argv[i:]

    if arg.n is not None:  # read a certain number of bytes
      stdin = sys.stdin.fileno()
      try:
        name = names[0]
      except IndexError:
        name = 'REPLY'  # default variable name
      s = ""
      if sys.stdin.isatty():  # set stdin to read in unbuffered mode
        orig_attrs = termios.tcgetattr(stdin)
        attrs = termios.tcgetattr(stdin)
        # disable canonical (buffered) mode
        # see `man termios` for an extended discussion
        attrs[3] &= ~termios.ICANON
        try:
          termios.tcsetattr(stdin, termios.TCSANOW, attrs)
          # posix.read always returns a single character in unbuffered mode
          while arg.n > 0:
            s += posix.read(stdin, 1)
            arg.n -= 1
        finally:
          termios.tcsetattr(stdin, termios.TCSANOW, orig_attrs)
      else:
        s_len = 0
        while arg.n > 0:
          buf = posix.read(stdin, arg.n)
          # EOF
          if buf == '':
            break
          arg.n -= len(buf)
          s += buf

      state.SetLocalString(self.mem, name, s)
      # NOTE: Even if we don't get n bytes back, there is no error?
      return 0

    if not names:
      names.append('REPLY')

    # leftover words assigned to the last name
    if arg.a:
      max_results = 0  # no max
    else:
      max_results = len(names)

    if arg.d is not None:
      if len(arg.d):
        delim_char = arg.d[0]
      else:
        delim_char = '\0'  # -d '' delimits by NUL
    else:
      delim_char = None  # read a line

    # We have to read more than one line if there is a line continuation (and
    # it's not -r).
    parts = []
    join_next = False
    while True:
      line = ReadLineFromStdin(delim_char)
      #log('LINE %r', line)
      if not line:  # EOF
        status = 1
        break

      if line.endswith('\n'):  # strip trailing newline
        line = line[:-1]
        status = 0
      else:
        # odd bash behavior: fail even if we can set variables.
        status = 1

      spans = self.splitter.SplitForRead(line, not arg.r)
      done, join_next = _AppendParts(line, spans, max_results, join_next, parts)

      #log('PARTS %s continued %s', parts, continued)
      if done:
        break

    if arg.a:
      state.SetArrayDynamic(self.mem, arg.a, parts)
    else:
      for i in xrange(max_results):
        try:
          s = parts[i]
        except IndexError:
          s = ''  # if there are too many variables
        #log('read: %s = %s', names[i], s)
        state.SetStringDynamic(self.mem, names[i], s)

    return status


if mylib.PYTHON:
  CD_SPEC = arg_def.Register('cd')
  CD_SPEC.ShortFlag('-L')
  CD_SPEC.ShortFlag('-P')


class Cd(object):
  def __init__(self, mem, dir_stack, cmd_ev, errfmt):
    # type: (Mem, DirStack, CommandEvaluator, ErrorFormatter) -> None
    self.mem = mem
    self.dir_stack = dir_stack
    self.cmd_ev = cmd_ev  # To run blocks
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    arg, i = CD_SPEC.ParseCmdVal(cmd_val)
    try:
      dest_dir = cmd_val.argv[i]
    except IndexError:
      val = self.mem.GetVar('HOME')
      if val.tag == value_e.Undef:
        self.errfmt.Print("$HOME isn't defined")
        return 1
      elif val.tag == value_e.Str:
        dest_dir = val.s
      elif val.tag == value_e.MaybeStrArray:
        # User would have to unset $HOME to get rid of exported flag
        self.errfmt.Print("$HOME shouldn't be an array")
        return 1

    if dest_dir == '-':
      old = self.mem.GetVar('OLDPWD', scope_e.GlobalOnly)
      if old.tag == value_e.Undef:
        self.errfmt.Print('$OLDPWD not set')
        return 1
      elif old.tag == value_e.Str:
        dest_dir = old.s
        print(dest_dir)  # Shells print the directory
      elif old.tag == value_e.MaybeStrArray:
        # TODO: Prevent the user from setting OLDPWD to array (or maybe they
        # can't even set it at all.)
        raise AssertionError('Invalid $OLDPWD')

    pwd = self.mem.GetVar('PWD')
    assert pwd.tag == value_e.Str, pwd  # TODO: Need a general scheme to avoid

    # Calculate new directory, chdir() to it, then set PWD to it.  NOTE: We can't
    # call posix.getcwd() because it can raise OSError if the directory was
    # removed (ENOENT.)
    abspath = os_path.join(pwd.s, dest_dir)  # make it absolute, for cd ..
    if arg.P:
      # -P means resolve symbolic links, then process '..'
      real_dest_dir = libc.realpath(abspath)
    else:
      # -L means process '..' first.  This just does string manipulation.  (But
      # realpath afterward isn't correct?)
      real_dest_dir = os_path.normpath(abspath)

    try:
      posix.chdir(real_dest_dir)
    except OSError as e:
      self.errfmt.Print("cd %r: %s", real_dest_dir, posix.strerror(e.errno),
                        span_id=cmd_val.arg_spids[i])
      return 1

    state.ExportGlobalString(self.mem, 'PWD', real_dest_dir)

    # WEIRD: We need a copy that is NOT PWD, because the user could mutate PWD.
    # Other shells use global variables.
    self.mem.SetPwd(real_dest_dir)

    if cmd_val.block:
      self.dir_stack.Push(real_dest_dir)
      try:
        unused = self.cmd_ev.EvalBlock(cmd_val.block)
      finally:  # TODO: Change this to a context manager.
        # note: it might be more consistent to use an exception here.
        if not _PopDirStack(self.mem, self.dir_stack, self.errfmt):
          return 1

    else:  # No block
      state.ExportGlobalString(self.mem, 'OLDPWD', pwd.s)
      self.dir_stack.Reset()  # for pushd/popd/dirs

    return 0


WITH_LINE_NUMBERS = 1
WITHOUT_LINE_NUMBERS = 2
SINGLE_LINE = 3

def _PrintDirStack(dir_stack, style, home_dir):
  # type: (DirStack, int, value__Str) -> None
  """Helper for 'dirs'."""

  if style == WITH_LINE_NUMBERS:
    for i, entry in enumerate(dir_stack.Iter()):
      print('%2d  %s' % (i, ui.PrettyDir(entry, home_dir)))

  elif style == WITHOUT_LINE_NUMBERS:
    for entry in dir_stack.Iter():
      print(ui.PrettyDir(entry, home_dir))

  elif style == SINGLE_LINE:
    s = ' '.join(ui.PrettyDir(entry, home_dir) for entry in dir_stack.Iter())
    print(s)


class Pushd(object):
  def __init__(self, mem, dir_stack, errfmt):
    # type: (Mem, DirStack, ErrorFormatter) -> None
    self.mem = mem
    self.dir_stack = dir_stack
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    num_args = len(cmd_val.argv) - 1
    if num_args == 0:
      # TODO: It's suppose to try another dir before doing this?
      self.errfmt.Print('pushd: no other directory')
      return 1
    elif num_args > 1:
      raise args.UsageError('got too many arguments')

    # TODO: 'cd' uses normpath?  Is that inconsistent?
    dest_dir = os_path.abspath(cmd_val.argv[1])
    try:
      posix.chdir(dest_dir)
    except OSError as e:
      self.errfmt.Print("pushd: %r: %s", dest_dir, posix.strerror(e.errno),
                        span_id=cmd_val.arg_spids[1])
      return 1

    self.dir_stack.Push(dest_dir)
    _PrintDirStack(self.dir_stack, SINGLE_LINE, self.mem.GetVar('HOME'))
    state.ExportGlobalString(self.mem, 'PWD', dest_dir)
    self.mem.SetPwd(dest_dir)
    return 0


def _PopDirStack(mem, dir_stack, errfmt):
  # type: (Mem, DirStack, ErrorFormatter) -> bool
  """Helper for popd and cd { ... }."""
  dest_dir = dir_stack.Pop()
  if dest_dir is None:
    errfmt.Print('popd: directory stack is empty')
    return False

  try:
    posix.chdir(dest_dir)
  except OSError as e:
    # Happens if a directory is deleted in pushing and popping
    errfmt.Print("popd: %r: %s", dest_dir, posix.strerror(e.errno))
    return False

  state.SetGlobalString(mem, 'PWD', dest_dir)
  mem.SetPwd(dest_dir)
  return True


class Popd(object):
  def __init__(self, mem, dir_stack, errfmt):
    # type: (Mem, DirStack, ErrorFormatter) -> None
    self.mem = mem
    self.dir_stack = dir_stack
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    if len(cmd_val.arg_spids) > 1:
      raise args.UsageError('got extra argument', span_id=cmd_val.arg_spids[1])

    if not _PopDirStack(self.mem, self.dir_stack, self.errfmt):
      return 1  # error

    _PrintDirStack(self.dir_stack, SINGLE_LINE, self.mem.GetVar('HOME'))
    return 0


if mylib.PYTHON:
  DIRS_SPEC = arg_def.Register('dirs')
  DIRS_SPEC.ShortFlag('-c')
  DIRS_SPEC.ShortFlag('-l')
  DIRS_SPEC.ShortFlag('-p')
  DIRS_SPEC.ShortFlag('-v')


class Dirs(object):
  def __init__(self, mem, dir_stack, errfmt):
    # type: (Mem, DirStack, ErrorFormatter) -> None
    self.mem = mem
    self.dir_stack = dir_stack
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    home_dir = self.mem.GetVar('HOME')

    arg, i = DIRS_SPEC.ParseCmdVal(cmd_val)
    style = SINGLE_LINE

    # Following bash order of flag priority
    if arg.l:
      home_dir = None  # disable pretty ~
    if arg.c:
      self.dir_stack.Reset()
      return 0
    elif arg.v:
      style = WITH_LINE_NUMBERS
    elif arg.p:
      style = WITHOUT_LINE_NUMBERS

    _PrintDirStack(self.dir_stack, style, home_dir)
    return 0


if mylib.PYTHON:
  PWD_SPEC = arg_def.Register('pwd')
  PWD_SPEC.ShortFlag('-L')
  PWD_SPEC.ShortFlag('-P')


class Pwd(object):
  """
  NOTE: pwd doesn't just call getcwd(), which returns a "physical" dir (not a
  symlink).
  """
  def __init__(self, mem, errfmt):
    # type: (Mem, ErrorFormatter) -> None
    self.mem = mem
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    arg, _ = PWD_SPEC.ParseCmdVal(cmd_val)

    # NOTE: 'pwd' will succeed even if the directory has disappeared.  Other
    # shells behave that way too.
    pwd = self.mem.pwd

    # '-L' is the default behavior; no need to check it
    # TODO: ensure that if multiple flags are provided, the *last* one overrides
    # the others
    if arg.P:
      pwd = libc.realpath(pwd)
    print(pwd)
    return 0


if mylib.PYTHON:
  HELP_SPEC = arg_def.Register('help')

# Use Oil flags?  -index?
  HELP_SPEC.ShortFlag('-i')  # show index
# Note: bash has help -d -m -s, which change the formatting

# TODO: Need $VERSION inside all pages?

class Help(object):

  def __init__(self, loader, errfmt):
    # type: (_FileResourceLoader, ErrorFormatter) -> None
    self.loader = loader
    self.errfmt = errfmt

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    try:
      topic = cmd_val.argv[1]
      blame_spid = cmd_val.arg_spids[1]
    except IndexError:
      topic = 'help'
      blame_spid = runtime.NO_SPID

    # TODO: Should be -i for index?  Or -l?
    if topic == 'index':
      groups = cmd_val.argv[2:]
      if len(groups) == 0:
        # Print the whole index
        groups = help_index.GROUPS

      for group in groups:
        try:
          f = self.loader.open('_devbuild/help/_%s' % group)
        except IOError:
          self.errfmt.Print('Invalid help index group: %r', group)
          return 1
        print(f.read())
        f.close()
      return 0

    try:
      f = self.loader.open('_devbuild/help/%s' % topic)
    except IOError:
      # Notes:
      # 1. bash suggests:
      # man -k zzz
      # info zzz
      # help help
      # We should do something smarter.

      # 2. This also happens on 'build/dev.sh minimal', which isn't quite
      # accurate.  We don't have an exact list of help topics!

      # 3. This is mostly an interactive command.  Is it obnoxious to
      # quote the line of code?
      self.errfmt.Print('no help topics match %r', topic,
                        span_id=blame_spid)
      return 1

    print(f.read())
    f.close()
    return 0


if mylib.PYTHON:
  HISTORY_SPEC = arg_def.Register('history')
  HISTORY_SPEC.ShortFlag('-c')
  HISTORY_SPEC.ShortFlag('-d', args.Int)


class History(object):
  """Show interactive command history."""

  def __init__(self, readline_mod, f=sys.stdout):
    # type: (Any, IO[bytes]) -> None
    self.readline_mod = readline_mod
    self.f = f

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    # NOTE: This builtin doesn't do anything in non-interactive mode in bash?
    # It silently exits zero.
    # zsh -c 'history' produces an error.
    readline_mod = self.readline_mod
    if not readline_mod:
      raise args.UsageError("OSH wasn't compiled with the readline module.")

    arg, arg_index = HISTORY_SPEC.ParseCmdVal(cmd_val)

    # Clear all history
    if arg.c:
      readline_mod.clear_history()
      return 0

    # Delete history entry by id number
    if arg.d:
      cmd_index = arg.d - 1

      try:
        readline_mod.remove_history_item(cmd_index)
      except ValueError:
        raise args.UsageError("couldn't find item %d" % arg.d)

      return 0

    # Returns 0 items in non-interactive mode?
    num_items = readline_mod.get_current_history_length()
    #log('len = %d', num_items)

    rest = cmd_val.argv[arg_index:]
    if len(rest) == 0:
      start_index = 1
    elif len(rest) == 1:
      arg0 = rest[0]
      try:
        num_to_show = int(arg0)
      except ValueError:
        raise args.UsageError('Invalid argument %r' % arg0)
      start_index = max(1, num_items + 1 - num_to_show)
    else:
      raise args.UsageError('Too many arguments')

    # TODO:
    # - Exclude lines that don't parse from the history!  bash and zsh don't do
    # that.
    # - Consolidate multiline commands.

    for i in xrange(start_index, num_items+1):  # 1-based index
      item = readline_mod.get_history_item(i)
      self.f.write('%5d  %s\n' % (i, item))
    return 0
