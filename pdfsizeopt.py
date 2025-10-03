import os.path
import sys

from lib.pdfsizeopt import main

if sys.version_info[:2] < (3, 0):
  sys.stderr.write('fatal: Python version >3.0 needed for: %s\n' % __file__)
  sys.exit(1)

script_dir = os.path.dirname(__file__)
try:
  __file__ = os.path.join(script_dir, os.readlink(__file__))
  script_dir = os.path.dirname(__file__)
except (OSError, AttributeError, NotImplementedError):
  pass
if os.path.isfile(os.path.join(
    script_dir, 'lib', 'pdfsizeopt', 'main.py')):
  sys.path[0] = os.path.join(script_dir, 'lib')

sys.exit(main.main(sys.argv, script_dir=script_dir))
