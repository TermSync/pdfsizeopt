import re
import getopt

from lib.util.constants import FLAGS_HELP
from lib.util.util import parse_bool_flag, parse_uint_flag


class Flags(object):
  """Class for parsing the command-line of pdfsizeopt."""

  BOOL_FLAG_WITH_DEFAULT_RE = re.compile(r'(--\w[-\w]*)=YES_NO;(?: default: (yes|no)|(.|\n)*)\Z')

  FLAG_PREFIX_RE = re.compile(r'--(\w[-\w]*=?)')

  def __init__(self):
    _BOOL_FLAG_WITH_DEFAULT_RE = self.BOOL_FLAG_WITH_DEFAULT_RE
    _FLAG_PREFIX_RE = self.FLAG_PREFIX_RE
    self.getopt_flag_names = getopt_flag_names = []
    self.bool_flag_names = bool_flag_names = set()
    for line in FLAGS_HELP.split('\n'):
      match = _BOOL_FLAG_WITH_DEFAULT_RE.match(line)
      if match:
        assert match.group(2), 'Invalid bool flag syntax: ' + line
        flag_name = match.group(1)[2:].replace('-', '_')
        setattr(self, flag_name, parse_bool_flag(match.group(1), match.group(2)))
        bool_flag_names.add(flag_name)
      match = _FLAG_PREFIX_RE.match(line)
      if match:
        getopt_flag_names.append(match.group(1))  # Multiple times OK.

    # TODO(pts): Add =auto for use_pngout, use_jbig2, use_multivalent.
    self.use_pngout = self.use_jbig2 = self.use_sam2p_pr = None
    self.mode = 'optimize'
    self.img_cmds = []
    self.args = []
    self.verbosity = 190
    self.tmp_dir = None

  def parse(self, argv):
    def long_has_args(opt, longopts):
      if opt in longopts:
        return False, opt
      elif opt + '=' in longopts:
        return True, opt
      else:
        raise getopt.GetoptError('option --%s not recognized' % opt, opt)

    # Disable prefix matching, e.g. disable `--use-pngo=no'.
    # TODO(pts): Undo this and make it thread-safe.
    getopt.long_has_args = long_has_args

    f = self
    if len(argv) < 2:
      f.mode = 'helpshort'
    # getopt.getopt wouldn't allow option and non-option arguments to be
    # intermixed.
    opts, f.args[:] = getopt.gnu_getopt(argv[1:], '+', self.getopt_flag_names)

    for key, value in opts:
      assert key.startswith('--')
      flag_name = key[2:].replace('-', '_')
      if flag_name in ('stats', 'help', 'helpshort', 'version', 'optimize'):
        f.mode = flag_name
      elif flag_name == 'use_image_optimizer':
        value = value.strip()
        if not value:
          raise getopt.GetoptError('Empty image optimizer command.')
        if len(value.split()) < 2:
          f.img_cmds.extend(filter(None, value.split(',')))
        else:
          # Special value 'none' and 'none' are also OK.
          f.img_cmds.append(value)
      elif flag_name == 'do_double_check_missing_glyphs':  # Legacy flag name.
        f.do_double_check_type1c_output = parse_bool_flag(key, value)
      elif flag_name == 'v':
        f.verbosity = parse_uint_flag(key, value)
      elif flag_name == 'quiet':
        f.verbosity = 20
      elif flag_name == 'tmp_dir':
        setattr(f, flag_name, value)
      elif flag_name in f.bool_flag_names:
        setattr(f, flag_name, parse_bool_flag(key, value))
      else:
        assert False, 'unknown flag %s' % key  # Can't happen, getopt output.