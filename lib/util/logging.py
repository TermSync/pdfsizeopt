import sys

class Logger:
  def __init__(self, verbosity=190):
    self.verbosity = verbosity

  def log_fatal(self, msg, exit_code=2):
    sys.stderr.write('fatal: %s\n' % (msg,))
    sys.stderr.flush()  # Not needed.
    sys.exit(exit_code)


  def log_error(self, msg):
    if self.verbosity >= 20:
      sys.stderr.write('error: %s\n' % (msg,))
      sys.stderr.flush()  # Not needed.


  def log_warning(self, msg):
    if self.verbosity >= 30:
      sys.stderr.write('warning: %s\n' % (msg,))
      sys.stderr.flush()  # Not needed.


  def log_info(self, msg, is_proportional=False):
    if self.verbosity >= (40, 50)[bool(is_proportional)]:
      sys.stderr.write('info: %s\n' % (msg,))
      sys.stderr.flush()  # Not needed.


  def log_proportional_info(self, msg):
    if self.verbosity >= 50:
      sys.stderr.write('info: %s\n' % (msg,))
      sys.stderr.flush()  # Not needed.

  def need_tool_log_output(self):
    return self.verbosity >= 60
