import os
import re
import sys
import zlib
import struct
import getopt

from lib.util.constants import DEFAULT_VERBOSITY, FLAGS_HELP
from lib.util.logging import Logger


logger = Logger(DEFAULT_VERBOSITY)


def format_percent(num, den):
  if den == 0:
    return '?%'
  return '%d%%' % int((num * 100 + (den / 2)) // den)


def permissive_zlib_decompress(data):
  """Decompress (inflate) an RFC 1950 deflated string, maybe w/o checksum.

  Args:
    data: String containing RFC 1950 deflated data, the 4-byte ADLER32 checksum
      being possibly truncated (to 0, 1, 2, 3 or 4 bytes), and the end-of-stream
      (Z_FINISH) may not be explicitly indicated.
  Returns:
    String containing the uncompressed data.
  Raises:
    zlib.error:
  """
  try:
    return zlib.decompress(data)
  except zlib.error:
    cmf, flg = data[0], data[1]
    wbits, cm = 8 + (cmf >> 4), cmf & 15
    if (cmf << 8 | flg) % 31:  # `flg & 31' is set like this.
      raise zlib.error('Bad zlib flag checksum.')
    if cm != 8:
      raise zlib.error('Unknown zlib compression method: %d' % cm)
    if not (8 <= wbits <= 15):
      raise zlib.error('Bad zlib wbits: %d' % wbits)
    if flg & 32:
      raise zlib.error('Unexpected zlib preset diectionary.')
    # This won't work data = zlib.decompress(buffer(data, 2), -wbits)
    # It may raise: zlib.error: Error -5 while decompressing data: incomplete or truncated stream
    zd = zlib.decompressobj(-wbits)
    data = zd.decompress(data[2:])
    data += zd.flush()
    if len(zd.unused_data) >= 4:  # Full Adler-32 checksum.
      # Python 2.4 or 2.5 zlib.adler32(...) may return signed or unsigned.
      adler32_data = struct.pack('>L', zlib.adler32(data) & 0xffffffff)
      if adler32_data != zd.unused_data[:4]:
        raise zlib.error('Bad zlib data Adler-32 checksum.')
    return data

def verify_gs(gs_cmd, is_verbose):
  q = '\''
  if sys.platform.startswith('win'):
    q = ''
  if is_verbose:
    logger.log_info('verifying Ghostscript: %s' % gs_cmd)
  gs_cmd2 = gs_cmd + ' -dNODISPLAY -c %s/GSOK === quit%s' % (q, q)
  # It would also work without RedirectOutput, because Ghostscript writes
  # the interesting message to stdout.
  f = os.popen(RedirectOutput(gs_cmd2, mode=True), 'r')
  data = f.read()
  if is_verbose:
    logger.log_info('output from Ghostscript: %r' % data)
  if f.close():
    if is_verbose:
      logger.log_info('Ghostscript failed')
    return False
  lines = data.rstrip('\n').split('\n')
  if not lines or lines[-1] != '/GSOK':
    if is_verbose:
      logger.log_info('missing /GSOK from Ghostscript')
    return False
  lines.pop()
  if not lines or ' Ghostscript ' not in lines[0]:
    if is_verbose:
      logger.log_info('missing Ghostscript version info')
    return False
  lines = [line for line in lines if not line.startswith('Copyright ') and
           'NO WARRANTY' not in line]
  data = '; '.join(lines)
  # Example: data == 'GPL Ghostscript 9.02 (2011-03-30)'.
  if is_verbose:
    logger.log_info('Ghostscript version info: %r' % (data,))
  return data


def find_exe_on_path(prog):
  """Return pathname to the executable prog, or None if not found."""
  exe_ext = ''
  if sys.platform.startswith('win') and '.' not in os.path.basename(prog):
    exe_ext = '.exe'
  return find_on_path(prog + exe_ext)


def get_gs_command(tmp_prefix, is_verbose=False, _cache=[]):
  """Return shell command-line prefix for running Ghostscript (gs)."""
  if _cache:
    return _cache[0]
  prefix = ''
  if sys.platform.startswith('win') and os.getcwd().startswith('\\\\'):
    # Ghostscript doesn't with on Windows 8.1 if current directory is a UNC
    # path. (It seems to work on wine-1.6.2.) See
    # https://github.com/pts/pdfsizeopt/issues/24 for details.
    #
    # Please note that os.getcwd() on wine-1.6.2 returns r'UNC\...\...\...',
    # so this doesn't match. But it would work if it was changed to
    # r'\\...\...\...' in future Wine versions.
    #
    # We work it around by doing a chdir to C:\ before running Ghostscript,
    # and converting relative paths to absolute paths in the Ghostscript
    # command-line (done in ShellQuoteFileName(..., is_gs=True)).
    prefix = 'c:&cd \\&'
  elif not sys.platform.startswith('win'):
    # Make Ghostscript use the same temporary directory as pdfsizeopt.
    # This fixes the startup problem on Docker.
    #
    # Without this we get the error:
    # GPL Ghostscript 9.05: **** Could not open temporary file /foo/gs_sYvSR7
    tmp_dir = os.path.dirname(tmp_prefix) or '.'
    prefix = 'TMPDIR=%s TEMP=%s ' % ((ShellQuote(tmp_dir),) * 2)
  data = None
  gs_cmd = os.getenv('PDFSIZEOPT_GS', None)
  if gs_cmd is None:
    if sys.platform.startswith('win'):  # Windows: win32 or win64
      gs_cmd = find_on_path(os.path.join('pdfsizeopt_gswin', 'gswin32c.exe'))
      if gs_cmd is not None:
        # wine-1.2 works with or without quoting here, but Windows XP
        # requires quoting if the path to gs_cmd contains whitespace.
        gs_cmd = prefix + ShellQuote(gs_cmd)
        data = verify_gs(gs_cmd, is_verbose=is_verbose)
      if not data:
        gs_cmd = prefix + 'gswin32c'
        data = verify_gs(gs_cmd, is_verbose=is_verbose)
      if not data:
        # if os.getenv('PROCESSOR_ARCHITECTURE', 'x86') != 'x86':
        if not os.getenv('PROGRAMFILES(X86)', ''):  # 32-bit Windows.
          envs = ('PROGRAMFILES',)
        else:
          envs = ('PROGRAMW6432', 'PROGRAMFILES(X86)', 'PROGRAMFILES')
        gs_cmd = None
        for env_name in envs:
          env_value = os.getenv(env_name, '')
          if env_value:
            d = os.path.join(env_value, 'gs')
            if os.path.isdir(d):
              for entry in os.listdir(d):
                if re.match(r'gs[89][.]\d\d\Z', entry):
                  fn = os.path.join(d, entry, 'bin', 'gswin32c.exe')
                  if os.path.isfile(fn):
                    gs_cmd = prefix + ShellQuote(fn)
                    data = verify_gs(gs_cmd, is_verbose=is_verbose)
                    if data:
                      break
                    logger.log_info('this Ghostscript does not work: %s' % gs_cmd)
                    data = gs_cmd = None
              if gs_cmd is not None:
                break
      if not data or gs_cmd is None:
        raise RuntimeError('Could not find a working Ghostscript.')
    else:
      gs_cmd = find_on_path(os.path.join('pdfsizeopt_gs', 'gs'))
      if gs_cmd is not None:
        gs_cmd = prefix + ShellQuote(gs_cmd)
        data = verify_gs(gs_cmd, is_verbose=is_verbose)
      if not data and sys.platform.startswith('darwin'):
        # http://pages.uoregon.edu/koch/ and MacTeX have it.
        gs_cmd = find_on_path('gs-noX11')
        if gs_cmd is not None:
          gs_cmd = prefix + ShellQuote(gs_cmd)
          data = verify_gs(gs_cmd, is_verbose=is_verbose)
      if not data:
        gs_cmd, data = prefix + 'gs', None
  if data is None:
    data = verify_gs(gs_cmd, is_verbose=is_verbose)
  if not data:
    raise RuntimeError('Ghostscript does not seem to work: %s' % gs_cmd)
  if os.sep not in gs_cmd[len(prefix):]:
    gs_cmd2 = gs_cmd
    if gs_cmd2.startswith('"'):  # Approximately correct on Windows and Unix.
      i = gs_cmd2.find('"', 1)
      if i > 0:
        gs_cmd2 = gs_cmd[1 : i]
    gs_cmd_print = find_exe_on_path(gs_cmd2)
    if not gs_cmd_print:
      gs_cmd_print = gs_cmd
  else:
    gs_cmd_print = gs_cmd
  logger.log_info('using Ghostscript %s: %s' % (gs_cmd_print, data))
  _cache.append(gs_cmd)
  return gs_cmd


def redirect_output_unix(cmd, mode=False):
  """Returns cmd with output redirected.

  Args:
    cmd: A single-line shell command.
    mode: If None, stdout and stderr are discarded.
      Otherwise, if true, stderr is redirected to stdout.
        (This is also called pipe mode.)
      Otherwise, stdout is redirected to stderr.
  Returns:
    A modified shell command-line with stdout and stderr redirected.
    The stdout of the command will be redirected to its stderr.
  """
  if mode is None:
    suffix = '>/dev/null 2>&1'
  else:
    suffix = ('>&2', ' 2>&1')[bool(mode)]
  return 'exec%s;%s' % (suffix, cmd + '')


WINDOWS_COMMAND_QUOTED_OR_SEP_RE = re.compile(r'"[^"]+"|>[^&\n]*(?:&&?|\r\n|\n|\Z)|&&?|\r\n|\n')
"""Matches a double-quoted string literal or a command separator
(& or && or newline) or an stdout-redirect until the separator
on a Windows cmd.exe command-line.

We assume that the command doesn't contain 2>...

TODO(pts): Handle \" and \\ better in quoted strings.
"""


def redirect_output_windows(cmd, mode=False):
  """See the docstring of RedirectOutputUnix."""
  # These command suffixes indeed work on Windows.
  # https://serverfault.com/a/132964/27885
  if mode is None:
    suffix, suffixb = '>nul 2>&1', ' 2>nul '
  else:
    mode = bool(mode)
    suffix = ('>&2', ' 2>&1')[mode]
    # If mode is true and '>...' is in cmd, then prepend
    # ' 2>&1' in front of '>...'.
    ics = ('">', '"')[mode]
  cmd = cmd.strip()
  if (r'\\' in cmd or r'\"' in cmd or ' 2>' in cmd or  # Too complicated.
      ('&' not in cmd and '\n' not in cmd and
       (mode or '>' not in cmd))):  # Simple, appending will do.
    return cmd + suffix
  if mode is None:
    replf = (lambda match: suffix * (match.group()[0] not in '">') + suffixb * (match.group()[0] == '>') + match.group())
  else:
    replf = lambda match: suffix * (match.group()[0] not in ics) + match.group()
  return WINDOWS_COMMAND_QUOTED_OR_SEP_RE.sub(replf, cmd + '\n').rstrip('\n')


RedirectOutput = (redirect_output_unix, redirect_output_windows)[sys.platform.startswith('win')]

UNIX_SHELL_NEED_QUOTE_RE = re.compile(r'[^-_.+,:/a-zA-Z0-9]')


def shell_quote_unix(string):
  """Quotes (escapes) a command-line argument for Unix (Bourne shell).

  Args:
    string: Command-line argument to be quoted.
  Returns:
    Quoted command-line argument. Can be concatenated with any string.
  """
  string = str(string)
  if string and not UNIX_SHELL_NEED_QUOTE_RE.search(string):
    return string
  return "'%s'" % string.replace("'", "'\\''")


WINDOWS_SHELL_NEED_QUOTE_RE = re.compile(r'[ \t%"<>&|]')

WINDOWS_SHELL_QUOTE_RE = re.compile(r'(\\+)("|\Z)|(")')


def shell_quote_windows(string):
  """Quotes (escapes) a command-line argument for Windows.

  Does the quoting according to inverse of the rules defined in

  * https://stackoverflow.com/a/4094897/97248
  * https://msdn.microsoft.com/en-us/library/a1y7w461.aspx

  Args:
    string: Command-line argument to be quoted.
  Returns:
    Quoted command-line argument. If the result ends with backslash, please
    don't continue it with a string starting with double-quote.
  """
  # The following characters need escaping:
  # space, tab, %, ", <, >, &, |.
  # If the input contains none of these, the output is same as the
  # input. Otherwise, the output looks like "...", and within the "s:
  #
  # * \s not followed by a " are kept intact
  # * \s followed by a " are doubled and the " is escaped as \"
  # * " (not preceded by a \) is escaped as \"
  # * anything else is kept intact
  string = str(string)
  if string and not WINDOWS_SHELL_NEED_QUOTE_RE.search(string):
    return string
  return '"%s"' % WINDOWS_SHELL_QUOTE_RE.sub(
      lambda match: r'\\' * len(match.group(1) or '') + r'\"' * len(match.group(2) or match.group(3) or ''), string)


ShellQuote = (shell_quote_unix, shell_quote_windows)[sys.platform.startswith('win')]


def shell_quote_file_name(string, is_gs=False):
  # TODO(pts): Make it work on non-Unix systems.
  if string.startswith('-') and len(string) > 1:
    string = '.%s%s' % (os.sep, string)
  if sys.platform.startswith('win'):
    # This hack here is making sure that Ghostscript running on UNC path as
    # os.getcwd() will get absolute filenames. That's because we are doing the
    # 'c:&cd \\&' hack to run Ghotscript with 'c:\\' as os.getcwd(), and thus
    # it wouldn't find files with a relative filename.
    #
    # os.path.isabs and os.path.join don't work correctly, we implement our
    # own.
    if (is_gs and
        get_gs_command()[1 : 8] == ':&cd \\&' and
        not string.startswith('\\\\') and not string[1 : 3] == ':\\'):
      cwd = os.getcwd()
      if cwd.startswith('UNC\\'):  # wine-1.6.3.
        cwd = '\\' + cwd[3:]
      assert cwd.startswith('\\\\') or cwd[1 : 3] == ':\\', cwd
      if string.startswith('\\'):
        if cwd.startswith('\\\\'):
          raise NotImplementedError(
              'Unable to join pathnames: %s + %s' % (cwd, string))
        string = cwd[:2] + string
      else:
        string = os.path.join(cwd, string)
  return ShellQuote(string)


def format_percent_two_digits(num, den):
  if den == 0:
    return '?%'
  v = (num * 10000 + (den / 2)) // den
  return '%d.%02d%%' % divmod(v, 100)


def ensure_removed(file_name):
  try:
    os.remove(file_name)
  except OSError:
    assert not os.path.exists(file_name)


def rename(fromfn, tofn):
  """Like os.rename, but works on Windows if the destination exists."""
  try:
    os.rename(fromfn, tofn)
    return
  except OSError:
    # On Windows: WindowsError:
    # [Error 183] Cannot create a file when that file already exists.
    # sys.platform.startswith('win') and e[0] == 183):
    if os.path.exists(fromfn) and os.path.exists(tofn):
      try:
        try:
          os.remove(tofn)
        except OSError:
          pass
        os.rename(fromfn, tofn)
        return
      except OSError as e:
        pass
        logger.log_fatal('unable to rename from %r to %r: %s' % (fromfn, tofn, e), 4)


def find_on_path(file_name):
  """Find file_name on $PATH, and return the full pathname or None."""
  path = os.getenv('PATH', None)
  is_win = sys.platform.startswith('win')
  if path is None and not is_win:
    path = '/bin:/usr/bin'
  # TODO(pts): On Windows, do we want to append .exe to file_name?
  for item in path.split(os.pathsep):
    if (is_win and item.startswith('"') and item.endswith('"') and
        len(item) >= 2):
      # TODO(pts): Allow ';' within item.
      item = item[1 : -1].replace('""', '')
    if not item:
      item = '.'
    path_name = os.path.join(item, file_name)
    if os.path.exists(path_name):
      return path_name
  return None


NONWORD_RE = re.compile(r'\W+')


def get_cmd_name(cmd_pattern):
  cmd_name = cmd_pattern.split()  # TODO(pts): Windows unquote "...".
  if not cmd_name:
    return None
  if cmd_pattern.startswith('sam2p -j:quiet -pdf:2 -c zip:1:9 '):
    return 'sam2p_np'
  if cmd_pattern.startswith('sam2p -j:quiet -c zip:15:9 '):
    return 'sam2p_pr'
  cmd_name = os.path.basename(cmd_name[0].lower())
  cmd_name = NONWORD_RE.sub('_', cmd_name)
  return cmd_name


# Allowed in /Encoding of /TypeFont, pdf_reference_1-7.pdf page 414.
# gs -dNODISPLAY -q -P- -c '/MacRomanEncoding .findencoding === quit'
PDF_FONT_ENCODINGS = {
   'WinAnsiEncoding': '/.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /space /exclam /quotedbl /numbersign /dollar /percent /ampersand /quotesingle /parenleft /parenright /asterisk /plus /comma /hyphen /period /slash /zero /one /two /three /four /five /six /seven /eight /nine /colon /semicolon /less /equal /greater /question /at /A /B /C /D /E /F /G /H /I /J /K /L /M /N /O /P /Q /R /S /T /U /V /W /X /Y /Z /bracketleft /backslash /bracketright /asciicircum /underscore /grave /a /b /c /d /e /f /g /h /i /j /k /l /m /n /o /p /q /r /s /t /u /v /w /x /y /z /braceleft /bar /braceright /asciitilde /bullet /Euro /bullet /quotesinglbase /florin /quotedblbase /ellipsis /dagger /daggerdbl /circumflex /perthousand /Scaron /guilsinglleft /OE /bullet /Zcaron /bullet /bullet /quoteleft /quoteright /quotedblleft /quotedblright /bullet /endash /emdash /tilde /trademark /scaron /guilsinglright /oe /bullet /zcaron /Ydieresis /space /exclamdown /cent /sterling /currency /yen /brokenbar /section /dieresis /copyright /ordfeminine /guillemotleft /logicalnot /hyphen /registered /macron /degree /plusminus /twosuperior /threesuperior /acute /mu /paragraph /periodcentered /cedilla /onesuperior /ordmasculine /guillemotright /onequarter /onehalf /threequarters /questiondown /Agrave /Aacute /Acircumflex /Atilde /Adieresis /Aring /AE /Ccedilla /Egrave /Eacute /Ecircumflex /Edieresis /Igrave /Iacute /Icircumflex /Idieresis /Eth /Ntilde /Ograve /Oacute /Ocircumflex /Otilde /Odieresis /multiply /Oslash /Ugrave /Uacute /Ucircumflex /Udieresis /Yacute /Thorn /germandbls /agrave /aacute /acircumflex /atilde /adieresis /aring /ae /ccedilla /egrave /eacute /ecircumflex /edieresis /igrave /iacute /icircumflex /idieresis /eth /ntilde /ograve /oacute /ocircumflex /otilde /odieresis /divide /oslash /ugrave /uacute /ucircumflex /udieresis /yacute /thorn /ydieresis'.split(' '),
   'MacRomanEncoding': '/.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /space /exclam /quotedbl /numbersign /dollar /percent /ampersand /quotesingle /parenleft /parenright /asterisk /plus /comma /hyphen /period /slash /zero /one /two /three /four /five /six /seven /eight /nine /colon /semicolon /less /equal /greater /question /at /A /B /C /D /E /F /G /H /I /J /K /L /M /N /O /P /Q /R /S /T /U /V /W /X /Y /Z /bracketleft /backslash /bracketright /asciicircum /underscore /grave /a /b /c /d /e /f /g /h /i /j /k /l /m /n /o /p /q /r /s /t /u /v /w /x /y /z /braceleft /bar /braceright /asciitilde /.notdef /Adieresis /Aring /Ccedilla /Eacute /Ntilde /Odieresis /Udieresis /aacute /agrave /acircumflex /adieresis /atilde /aring /ccedilla /eacute /egrave /ecircumflex /edieresis /iacute /igrave /icircumflex /idieresis /ntilde /oacute /ograve /ocircumflex /odieresis /otilde /uacute /ugrave /ucircumflex /udieresis /dagger /degree /cent /sterling /section /bullet /paragraph /germandbls /registered /copyright /trademark /acute /dieresis /.notdef /AE /Oslash /.notdef /plusminus /.notdef /.notdef /yen /mu /.notdef /.notdef /.notdef /.notdef /.notdef /ordfeminine /ordmasculine /.notdef /ae /oslash /questiondown /exclamdown /logicalnot /.notdef /florin /.notdef /.notdef /guillemotleft /guillemotright /ellipsis /space /Agrave /Atilde /Otilde /OE /oe /endash /emdash /quotedblleft /quotedblright /quoteleft /quoteright /divide /.notdef /ydieresis /Ydieresis /fraction /currency /guilsinglleft /guilsinglright /fi /fl /daggerdbl /periodcentered /quotesinglbase /quotedblbase /perthousand /Acircumflex /Ecircumflex /Aacute /Edieresis /Egrave /Iacute /Icircumflex /Idieresis /Igrave /Oacute /Ocircumflex /.notdef /Ograve /Uacute /Ucircumflex /Ugrave /dotlessi /circumflex /tilde /macron /breve /dotaccent /ring /cedilla /hungarumlaut /ogonek /caron'.split(' '),
   'MacExpertEncoding': '/.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /space /exclamsmall /Hungarumlautsmall /centoldstyle /dollaroldstyle /dollarsuperior /ampersandsmall /Acutesmall /parenleftsuperior /parenrightsuperior /twodotenleader /onedotenleader /comma /hyphen /period /fraction /zerooldstyle /oneoldstyle /twooldstyle /threeoldstyle /fouroldstyle /fiveoldstyle /sixoldstyle /sevenoldstyle /eightoldstyle /nineoldstyle /colon /semicolon /.notdef /threequartersemdash /.notdef /questionsmall /.notdef /.notdef /.notdef /.notdef /Ethsmall /.notdef /.notdef /onequarter /onehalf /threequarters /oneeighth /threeeighths /fiveeighths /seveneighths /onethird /twothirds /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /ff /fi /fl /ffi /ffl /parenleftinferior /.notdef /parenrightinferior /Circumflexsmall /hypheninferior /Gravesmall /Asmall /Bsmall /Csmall /Dsmall /Esmall /Fsmall /Gsmall /Hsmall /Ismall /Jsmall /Ksmall /Lsmall /Msmall /Nsmall /Osmall /Psmall /Qsmall /Rsmall /Ssmall /Tsmall /Usmall /Vsmall /Wsmall /Xsmall /Ysmall /Zsmall /colonmonetary /onefitted /rupiah /Tildesmall /.notdef /.notdef /asuperior /centsuperior /.notdef /.notdef /.notdef /.notdef /Aacutesmall /Agravesmall /Acircumflexsmall /Adieresissmall /Atildesmall /Aringsmall /Ccedillasmall /Eacutesmall /Egravesmall /Ecircumflexsmall /Edieresissmall /Iacutesmall /Igravesmall /Icircumflexsmall /Idieresissmall /Ntildesmall /Oacutesmall /Ogravesmall /Ocircumflexsmall /Odieresissmall /Otildesmall /Uacutesmall /Ugravesmall /Ucircumflexsmall /Udieresissmall /.notdef /eightsuperior /fourinferior /threeinferior /sixinferior /eightinferior /seveninferior /Scaronsmall /.notdef /centinferior /twoinferior /.notdef /Dieresissmall /.notdef /Caronsmall /osuperior /fiveinferior /.notdef /commainferior /periodinferior /Yacutesmall /.notdef /dollarinferior /.notdef /.notdef /Thornsmall /.notdef /nineinferior /zeroinferior /Zcaronsmall /AEsmall /Oslashsmall /questiondownsmall /oneinferior /Lslashsmall /.notdef /.notdef /.notdef /.notdef /.notdef /.notdef /Cedillasmall /.notdef /.notdef /.notdef /.notdef /.notdef /OEsmall /figuredash /hyphensuperior /.notdef /.notdef /.notdef /.notdef /exclamdownsmall /.notdef /Ydieresissmall /.notdef /onesuperior /twosuperior /threesuperior /foursuperior /fivesuperior /sixsuperior /sevensuperior /ninesuperior /zerosuperior /.notdef /esuperior /rsuperior /tsuperior /.notdef /.notdef /isuperior /ssuperior /dsuperior /.notdef /.notdef /.notdef /.notdef /.notdef /lsuperior /Ogoneksmall /Brevesmall /Macronsmall /bsuperior /nsuperior /msuperior /commasuperior /periodsuperior /Dotaccentsmall /Ringsmall /.notdef /.notdef /.notdef /.notdef'.split(' '),
}
NONE_ENCODING = [None]  * 256

BOOL_VALUES = {
    'on': True,
    'off': False,
    'yes': True,
    'no': False,
    '1': True,
    '0': False,
    'true': True,
    'false': False,
}


def parse_bool_flag(key, flag_value):
  flag_value_lower = flag_value.lower()
  if flag_value_lower not in BOOL_VALUES:
    raise getopt.GetoptError('flag %s=%s needs a bool value' % (key, flag_value))
  return BOOL_VALUES[flag_value_lower]


def parse_uint_flag(key, flag_value):
  try:
    value = int(flag_value)
    if value < 0:
      raise ValueError
  except ValueError:
    raise getopt.GetoptError('flag %s=%s needs a nonnegative integer value' % (key, flag_value))
  return value


def get_dir(file_name):
  readlink = getattr(os, 'readlink', None)  # Not available on Windows.
  if readlink:
    while 1:
      try:
        target_name = readlink(file_name)
      except OSError:  # Happens on Linux if file_name is not a symlink.
        break
      if target_name == file_name:  # This doesn't happen on Linux.
        break
      file_name = target_name
  return os.path.dirname(file_name)


def get_version_spec(zip_file):
  if zip_file and os.path.isfile(zip_file):
    main_file = zip_file
    zip_msg = ' ZIP'
  else:
    main_file = __file__
    zip_msg = ''
  if main_file.endswith('.pyc'):
    main_file = main_file[:-4] + '.py'
  try:
    size = os.stat(main_file).st_size
  except OSError:
    # We'll get this if main_file is within a .zip file (on $PYTHONPATH).
    # Since the built-in linecache.py doesn't attempt to read such files,
    # we don't do that either, and keep size = None for simplicity.
    size = None
  rev = None  # TODO(pts): Do we want to display a git commit id?
  return 'pdfsizeopt%s r%s size=%s' % (zip_msg, rev or 'UNKNOWN', size)


def get_used_script_dir(script_dir, zip_file):
  # script_file = sys.modules['__main__'].__file__
  if script_dir:
    return script_dir
  elif zip_file and os.path.isfile(zip_file):
    return os.path.dirname(os.path.abspath(zip_file))
  else:
    return os.path.dirname(os.path.abspath(__file__))


def get_libexec_dir(used_script_dir):
  if sys.platform.startswith('win'):
    xdir = os.path.join(used_script_dir, 'pdfsizeopt_win32exec')
    if os.path.isdir(xdir):
      return xdir
  xdir = os.path.join(used_script_dir, 'pdfsizeopt_libexec')
  if os.path.isdir(xdir):
    return xdir
  return None


def prepend_to_path(extrapath_dir):
  logger.log_info('prepending to PATH: %s' % extrapath_dir)
  # When adding to the PATH, we mustn't call ShellQuote on extrapath_dir on
  # Unix systems. On Windows XP ... Windows 10, it works with or without
  # ShellQuote (i.e. "s round the directory name), but on wine-1.6.2, directory
  # names in PATH don't work with "s. So we are not calling ShellQuote here.
  os.environ['PATH'] = '%s%s%s' % (extrapath_dir, os.pathsep, os.getenv('PATH', ''))


def setup_tmp_prefix(output_file_name, tmp_dir):
  if not tmp_dir:
    if sys.platform.startswith('win'):
      tmp_dir = os.getenv('TEMP', '')
    else:
      tmp_dir = os.getenv('TMPDIR', '')
    if not (tmp_dir and os.path.isdir(tmp_dir)):
      if output_file_name:
        tmp_dir = os.path.dirname(output_file_name)
      else:
        tmp_dir = '..'
  tmp_basename = 'psotmp.%d.' % os.getpid()
  if tmp_dir == '.':
    tmp_prefix = tmp_basename
  else:
    tmp_prefix = os.path.join(tmp_dir, tmp_basename)
  if tmp_prefix.startswith('/') and sys.platform.startswith('win'):
    # pngout doesn't work otherwise, because it treats '/' as flag.
    tmp_prefix = tmp_prefix.replace('/', os.sep)
  return tmp_prefix


def display_help(mode, argv0):
  logger.log_info('usage for statistics computation: %s --stats <input.pdf>' % argv0)
  logger.log_info('usage for size optimization: %s [<flag>...] <input.pdf> [<output.pdf>]' % argv0)
  if mode == 'helpshort':
    logger.log_info('specify --help to get help on each flag')
  else:
    logger.log_info('flags:\n\n%s' % FLAGS_HELP.strip())