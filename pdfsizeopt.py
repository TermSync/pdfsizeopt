#!/usr/bin/python3.10

""":" # pdfsizeopt: PDF file size optimizer

P="$(readlink "$0" 2>/dev/null)"
test "$P" && test "${P#/}" = "$P" && P="${0%/*}/$P"
test "$P" || P="$0"
Q="${P%/*}"/pdfsizeopt_libexec/python
test -f "$Q" && exec "$Q" -E -- "$P" ${1+"$@"}
type -p python3.13 >/dev/null 2>&1 && exec python3.13 -- "$P" ${1+"$@"}
type -p python3.12 >/dev/null 2>&1 && exec python3.12 -- "$P" ${1+"$@"}
type -p python3.11 >/dev/null 2>&1 && exec python3.11 -- "$P" ${1+"$@"}
type -p python3.10 >/dev/null 2>&1 && exec python3.10 -- "$P" ${1+"$@"}
exec python -- "$P" ${1+"$@"}; exit 1

This is a Python 3.10+ script, it works with Python 3.10 or newer. It
doesn't work with Python 2.x. Feel free to replace the #! line with
`#! /usr/bin/python', `#! /usr/bin/env python' or whatever suits you best.
"""

import os
import os.path
import sys

if sys.version_info[:2] < (3, 10):
  sys.stderr.write('fatal: Python version 3.10+ needed for: %s\n' % __file__)
  sys.exit(1)

script_dir = os.path.dirname(__file__)
try:
  __file__ = os.path.join(script_dir, os.readlink(__file__))
  script_dir = os.path.dirname(__file__)
except (OSError, AttributeError, NotImplementedError):
  pass
if os.path.isfile(os.path.join(script_dir, 'lib', 'pdfsizeopt', 'main.py')):
  sys.path[0] = os.path.join(script_dir, 'lib')

import lib
from lib import main
sys.exit(main.main(sys.argv, script_dir=script_dir))
