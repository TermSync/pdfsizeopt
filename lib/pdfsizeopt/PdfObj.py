import re
import sys
import zlib

from lib.util import *


logger = Logger()


class PdfObj(object):
  """Contents of a PDF object (head and stream data).

  PdfObj provides convenience methods Set and Get for manipulating PDF objects
  of type dict and stream.

  Attributes:
    _head: stripped string between `obj' and (`stream' or `endobj')
    _cache: ParseDict(self._head) or None.
    stream: stripped string between `stream' and `endstream', or None
  """
  __slots__ = ['_head', 'stream', '_cache']

  PDF_WHITESPACE_CHARS = b'\0\t\n\r\f '
  """String containing all PDF whitespace characters."""

  PDF_STREAM_OR_ENDOBJ_RE = re.compile(br'(stream(?:[\x00\t\f ]*\r?\n|[\x00\t\f ])|endobj(?:\r\n|[\x00\t\n\r\f /%]|\Z))')
  """Matches stream or endobj in a PDF obj in .group(1).

  pdf_reference_1-7.pdf requires stream\r?\n, we are more permissive.
  Example: 2019-05-21-azure.pdf in https://github.com/pts/pdfsizeopt/issues/117
  """

  PDF_PREFIXED_STREAM_OR_ENDOBJ_RE = re.compile(br'[\x00\t\n\r\f \)>\]]' + PDF_STREAM_OR_ENDOBJ_RE.pattern)
  """Matches stream or endobj in a PDF obj, prefixed with 1 char."""

  REST_OF_R_RE = re.compile(
      r'(?:[\x00\t\n\r\f ]|%[^\r\n]*[\r\n])+([-+]?\d+)'
      r'(?:[\x00\t\n\r\f ]|%[^\r\n]*[\r\n])+R(?=[\x00\t\n\r\f /%<>\[\](])')
  """Matches the generation number and the 'R' (followed by a char)."""

  PDF_END_OF_REF_RE = re.compile(
      r'[\x00\t\n\r\f ]R(?=[\x00\t\n\r\f /%(<>\[\]]|\Z)')
  """Matches the whitespace, the 'R' and looks ahead 1 char."""

  PDF_REF_END_RE = re.compile(r'[\x00\t\n\r\f ]R\Z')
  """Matches a whitespace char and an R at the end of the string."""

  PDF_REF_AT_EOS_RE = re.compile(
      br'([-+]?\d+)'
      br'(?:[\x00\t\n\r\f ]|%[^\r\n]*[\r\n])+([-+]?\d+)'
      br'(?:[\x00\t\n\r\f ]|%[^\r\n]*[\r\n])+R\Z')
  """Matches an <x> <y> R at end-of-string."""

  PDF_REF_RE = re.compile(
      PDF_REF_AT_EOS_RE.pattern[:-2] + br'(?=[\x00\t\n\r\f /%(<>\[\]]|\Z)')
  """Matches an <x> <y> R."""

  PDF_NUMBER_OR_REF_RE = re.compile(
      br'([-+]?\d+)(?=[\x00\t\n\r\f /%(<>\[\]])(?:'
      br'(?:[\x00\t\n\r\f ]|%[^\r\n]*[\r\n])+([-+]?\d+)'
      br'(?:[\x00\t\n\r\f ]|%[^\r\n]*[\r\n])+R'
      br'(?=[\x00\t\n\r\f /%(<>\[\]]|\Z))?')
  """Matches a number or an <x> <y> R."""

  LENGTH_OF_STREAM_RE = re.compile(br'/Length(?:[\x00\t\n\r\f ]|%[^\r\n]*[\r\n])+' + PDF_NUMBER_OR_REF_RE.pattern)
  """Matches `/Length <x>' or `/Length <x> <y> R'."""

  PDF_COMMENTS_OR_WHITESPACE_RE = re.compile(br'(?:[\x00\t\n\r\f ]+(?![\x00\t\n\r\f ])|%[^\r\n]*(?:[\r\n]|\Z))*')
  """Matches any number (0 is OK) of terminated comments and whitespace.

  Doesn't capture any regexp group.
  """

  PDF_COMMENT_OR_WHITESPACE_RE = re.compile(PDF_COMMENTS_OR_WHITESPACE_RE.pattern[:-1] + br'+')
  """Matches any number (>= 1) of terminated comments and whitespace."""

  PDF_JUST_OBJ_DEF_RE = re.compile(
      br'(\d+)[\x00\t\n\r\f ]+(\d+)[\x00\t\n\r\f ]+obj'
      br'(?=[\x00\t\n\r\f %/<\[({])')
  """Matches an `obj' definition without leading or trailing whitespace."""

  PDF_OBJ_DEF_RE = re.compile(br'[\x00\t\n\r\f ]*' + PDF_JUST_OBJ_DEF_RE.pattern + PDF_COMMENTS_OR_WHITESPACE_RE.pattern)
  """Matches an `obj' definition with maybe leading and trailing whitespace.

  Trailing whitespace and comments are ignored.

  Captures the object number and the generation number.
  """

  PDF_OBJ_DEF_OR_XREF_RE = re.compile(
      PDF_JUST_OBJ_DEF_RE.pattern + br'[\x00\t\n\r\f ]*|xref[\x00\t\n\r\f ]+|startxref[\x00\t\n\r\f ]+'
  )
  """Matches an `obj' definition, xref or startxref.

  It's important that leading whitespace is not ignored.

  Trailing whitespace (but not comments) is ignored.
  """

  PDF_HEXTOKENS_SAFE_HEX_ESCAPE_RE = re.compile(br'[^-+A-Za-z0-9_./#\[\]()<>{}\x00\t\n\r\f ]')
  """Matches a single name character which needs to be hex-escaped.

  This regexp should be matched against a PDF token sequence (rather than a
  binary string) which contains all strings <hex>-escaped. (Parts of comments
  will also be matched.) A safe token
  sequence is not enough, e.g. '(@)' is safe, but doesn't contain all
  strings <hex>-escaped.

  The character class of PDF_HEXTOKENS_SAFE_HEX_ESCAPE_RE is a subset of the
  character class of PDF_SAFE_KEEP_HEX_ESCAPED_RE, e.g. '/', '<', ' ',
  '#' and '(' are not members of PDF_HEXTOKENS_SAFE_HEX_ESCAPE_RE, but they are
  members of PDF_SAFE_KEEP_HEX_ESCAPED_RE,. """

  PDF_SAFE_KEEP_HEX_ESCAPED_RE = re.compile(br'[^-+A-Za-z0-9_.]')
  """Matches a single character to be kept escaped internally to pdfsizeopt."""

  PDF_STRING_UNSAFE_CHARS = b'<>(){}[]\\\v/\0\t\n\r\f%#'    # TODO: Double check validity [dzellm]
  """Contains all characters prohibited in a safe PDF string literal.

  * \v is considered unsafe because it's a Python whitespace (not PDF
    whitespace).
  * / is considered unsafe because of <</A(/Type/XObject)>>
  * % is considered unsafe because it's a PDF comment.
  * \r is considered unsafe because (\r) is equivalent to (\n).
  * { and } are considered unsafe because they are PostScript tokens, and they
    are not safe to have in PDF names.
  * \\ is considered unsafe because of (\)) .
  * # is considered unsafe so that we'd be able to do
    _EscapePdfNamesInHexTokensSafe processing before string processing.

  See also PDF_SAFE_KEEP_HEX_ESCAPED_RE for PDF name tokens.
  PDF_STRING_UNSAFE_CHAR_RE (e.g. not containing *) must be a subset of
  PDF_SAFE_KEEP_HEX_ESCAPED_RE (e.g. containing *).
  """

  PDF_STRING_UNSAFE_CHAR_RE = re.compile(b'[' + re.escape(PDF_STRING_UNSAFE_CHARS) + b']')
  """Matches a single character prohibited in a safe PDF string literal."""

  PDF_TOKENS_SAFE_STRING_RE = re.compile(
      br'([-+A-Za-z0-9_./#\[\] ]+|>>|<(?:<|[0-9a-f]*>)|\([^<>(){}[\]\\\v/\0\t\n\r\f%#]*\))+')
  """Matches a safe prefix of a PDF token sequence.

  This regexp accepts 'foo' and 'fooBar' and '<> <> R' as safe, and it also
  accepts unbalanced '[' and ']'. This is working as intended, because
  PdfObj.ParseTokensToSafe can return these.

  Please note that a safe token sequence is not necessarily ASCII, e.g. it
  can contain non-ASCII string literals, e.g. '(\200)'.

  If PDF_TOKENS_UNSAFE_CHARS_RE is found in a string, then
  PDF_TOKENS_SAFE_STRING_RE doesn't match all the way to the end. It's not
  true the other way round, e.g. PDF_TOKENS_SAFE_STRING_RE doesn't match
  'foo&bar' all the way to the end, but PDF_TOKENS_UNSAFE_CHARS_RE isn't found.
  """

  PDF_TOKENS_UNSAFE_CHARS_RE = re.compile(r'[{}\\\v\x00\t\n\r\f%\\]+')
  """Matches a single unsafe character in a PDF token sequence.

  * Space is not unsafe, we need it for `/Length 5'.
  * Non-space whitespace is considered unsafe because we normalize to space.
  * ( and ) are not unsafe, we need them for `(hi)'.
  * [ and ] are not unsafe, we need them for `[]'.
  * < and > are not unsafe, we need them for `<01>'.
  * # is not unsafe, we need it for `/Foo#2A'.
  * { and } are considered unsafe because they are PostScript tokens, and they
    are not safe to have in PDF names. They are hex-escaped in string
    literals.
  * \v is considered unsafe because it's a Python whitespace (not PDF
    whitespace).
  * % is considered unsafe because of <<%/Type/XObject\n>> .
  * \\ is considered unsafe because of (\)) .

  On single-character input, PDF_TOKENS_UNSAFE_CHARS_RE is a subset of
  PDF_STRING_UNSAFE_CHAR_RE.

  !!! Use this for additional checking (not in PdfObj.__init__) on the (test)
      output of PdfObj.__init__ and ParseTokensToSafe.
  """

  PDF_ANGLE_BRACKET_FOR_SIMPLE_RE = re.compile(br'<(?:<|[\x00\t\n\r\f 0-9a-fA-F]*>?)|>(?:>)?')
  """Matches angle bracket constructs.

  Useful for detecting PDF token sequence syntax errors in simple parsing."""

  PDF_HEX_STRING_LITERAL_OR_DICT_RE = re.compile(br'<(?:<|[\x00\t\n\r\f 0-9a-fA-F]*>?)')
  """Matches a << or a PDF hex <...> string literal, without maybe the trailing >."""

  PDF_HEX_STRING_LITERAL_RE = re.compile(r'<[\x00\t\n\r\f 0-9a-fA-F]*>?')
  """Matches a PDF hex <...> string literal, where the trailing > is optional,
  but then anchored to \Z."""

  PDF_UNSAFE_NAME_IN_SIMPLE_RE = re.compile(br'[!"$&\'*,:;=?@\\^`|~]')
  """Matches a simple character which should be hex-escaped in simple PDF
  token sequence parsing.

  Such characters are not matched by PDF_TOKENS_NONSIMPLE_CHAR_RE, are not
  whitespace, are not any of /[]<> , but they
  are matched by PDF_SAFE_KEEP_HEX_ESCAPED_RE.
  """

  PDF_TOKENS_NONSIMPLE_CHAR_RE = re.compile(
      br'[^-+A-Za-z0-9_.#/\[\]<>\x00\t\n\r\f ' +
      PDF_UNSAFE_NAME_IN_SIMPLE_RE.pattern[1 : -1] + br']')
  """Matches a non-simple character in a PDF obj, needs the
  PDF_TOKENS_INTERESTING_RE parser.

  In particular these PDF token constructs are considered nonsimple: non-hex
  string ('(' and ')'), comment ('%'), PostScript array ('{', '}'), a name
  with a hex escape (e.g. '/Typ#65'), a name with an unsafe character
  (e.g. '/Type*' or '/foo\\bar'), a vertical tab ('\v', because it's
  whitespace in Python, but not in PDF).

  Non-space PDF whitespace characters (e.g '\t') are considered simple, because
  parsing will normalize them to spaces.
  """

  # !!! Faster regexps by splitting. Do some benchmarks on huge PDFs.
  PDF_TOKENS_INTERESTING_RE = re.compile(
      PDF_COMMENT_OR_WHITESPACE_RE.pattern + br'(?=([^\x00\t\n\r\f ]|\Z))|'  # 1. Comment or whitespace.
      br'\(([^\\()\r]*)\)|'  # 2. Simple string: without parens or backslash.
      br'(\()|'  # 3. Beginning of a complicated string.
      br'(/[-+A-Za-z0-9_.]*[^<>(){}\[\]/\x00\t\n\r\f %\-+A-Za-z0-9_.][^<>(){}\[\]/\x00\t\n\r\f %]*)|' +  # 4. Name with explicit hex (#AB) escape or name which needs hex-escaping. !!! Reuse PDF_SAFE_KEEP_HEX_ESCAPED_RE.
      br'(/(?=[<>(){}\[\]/\x00\t\n\r\f %]|\Z))|' +  # 5. An empty name token.
      br'(#[0-9a-fA-F]{0,2})|' +  # 6. A hex-escape (usually in a name or a keyword).
      br'(' + PDF_HEX_STRING_LITERAL_OR_DICT_RE.pattern + br')|'  # 7. Hex string literal or stray <.
      br'([{}\\\v)]|>>?)|'  # 8. Invalid PDF tokens (except for >>). (At least invalid outside name tokens.)
      + PDF_STREAM_OR_ENDOBJ_RE.pattern[:-1] + br'|startxref[\x00\t\n\r\f ]|xref[\x00\t\n\r\f ])' )  # 9. stream or endobj or startxref or xref.

  """Matches interesting parts of a non-simple obj head."""

  PDF_EMPTY_NAME_TOKEN_RE = re.compile(b'/(?:[<>(){}\[\]/\0\t\n\r\f %]|\Z)')

  PDF_WHITESPACE_IN_SIMPLE_RE = re.compile(br'([^\x00\t\n\r\f ])[\x00\t\n\r\f ]+(?=([^\x00\t\n\r\f ]|\Z))')
  """Matches whitespace in a simple obj head."""

  PDF_NUMBER_AT_EOS_RE = re.compile(br'(?:([-])|[+]?)0*(\d*(?:[.]\d*)?)\Z')
  """Matches a single PDF numeric token (real or integer).

  Captures some parts of the number in groups.

  '42.' and '.5' are a valid floats in Python, PostScript and PDF. Also
  matches '.', which is not a valid number in Python, PostScript or PDF, but
  see FixAllBadNumbers why we accept it.
  !!! Don't accept it here.
  """

  PDF_KEYWORD_RE = re.compile('[a-z]+')
  """Matches a PDF keyword."""

  PDF_KEYWORD_OR_NUMBER_AT_EOS_RE = re.compile(b'[a-z]+\Z|[+-]?(?:[.]\d*|\d+(?:[.]\d*)?)\Z')
  """Matches a PDF keyword (e.g. true, false, null, obj) or number."""

  PDF_STARTXREF_EOF_RE = re.compile(br'[>\x00\t\n\r\f ]startxref[\x00\t\n\r\f ]+(\d+)(?:[\x00\t\n\r\f ]+%%EOF[\x00\t\n\r\f ]*)?')
  PDF_STARTXREF_EOF_AT_EOS_RE = re.compile(PDF_STARTXREF_EOF_RE.pattern + br'\Z')
  """Matches whitespace (or >), startxref, offset, then EOF at EOS."""

  PDF_VERSION_HEADER_RE = re.compile(br'%PDF-(1[.]\d)%?(\r?\n%[\x80-\xff]{1,4}\r?\n|[\x00\t\n\r\f ])')
  """Matches the header with the version at the beginning of the PDF."""

  PDF_TRAILER_RE = re.compile(
      br'(?s)trailer[\x00\t\n\r\f ]*(<<.*?>>)' +
      PDF_COMMENTS_OR_WHITESPACE_RE.pattern +
      br'(?:startxref|xref)[\x00\t\n\r\f ]')
  """Matches from 'trailer' to 'startxref' or 'xref'.

  TODO(pts): Match more generally, see multiple trailers for testing in:
  pdf.a9p4/5176.CFF.a9p4.pdf
  """

  PDF_PREFIXED_STARTXREF_RE = re.compile(
      br'>>' + PDF_COMMENTS_OR_WHITESPACE_RE.pattern +
      br'(startxref|xref)[\x00\t\n\r\f ]')
  """Matches startxref or xref in a PDF trailer, prefixed with >>."""

  PDF_XREF_SECTION_RE = re.compile(br'[\x00\t\n\r\f ]*(xref[\x00\t\n\r\f ]+)\d+[\x00\t\n\r\f ]+\d+[\x00\t\n\r\f ]+')
  """Matches the start of a PDF xref section.

  Some broken PDFs have whitespace in front the xref, so we accept that.
  Example: Cohn.pdf in https://github.com/pts/pdfsizeopt/issues/42
  """

  PDF_XREF_SUBSECTION_OR_TRAILER_RE = re.compile(
      br'(\d+)[\x00\t\n\r\f ]+(\d+)[\x00\t\n\r\f ]+|'
      br'[\x00\t\n\r\f ]*(xref[\x00\t\n\r\f ]|trailer(?:[\x00\t\n\r\f ]|<<))')
  """Matches a PDF xref entry, or the 'xref' or 'trailer' keyword."""

  PDF_XREF_ENTRY_RE = re.compile(
      br'(\d{10})[\x00\t\n\r\f ](\d{5})[\x00\t\n\r\f ]([nf])'
      br'[\x00\t\n\r\f ]{2}')
  """Matches a single PDF xref entry: obj_num, offset and slot type."""

  PDF_OBJ_OR_TRAILER_RE = re.compile(
      br'[\n\r](?:(\d+)[\x00\t\n\r\f ]+(\d+)[\x00\t\n\r\f ]+obj\b|'
      br'trailer(?=[\x00\t\n\r\f ]|<<))')
  """Matches an 'obj' start or a 'trailer' start."""

  PDF_TRAILER_WORD_RE = re.compile(r'[\x00\t\n\r\f ](trailer[\x00\t\n\r\f ]*<<)')
  """Matches whitespace, the 'trailer' and some more chars."""

  PDF_ENDSTREAM_ENDOBJ_RE = re.compile(br'([\x00\t\n\r\f ]*)endstream[\x00\t\n\r\f ]+endobj(?:[\x00\t\n\r\f /]|\Z)')
  """Matches endstream+endobj."""

  PDF_BAD_NUMBER_RE = re.compile(r'([\x00\t\n\r\f \[])[.](?=[\x00\t\n\r\f \]])')
  """Matches a bad (unparsable) number."""

  PDF_SIMPLE_VALUE_RE = re.compile(
      br'(?s)[\x00\t\n\r\f ]*('
      br'\[.*?\]|<<.*?>>|<[^>]*>|\(.*?\)|%[^\n\r]*|'
      + PDF_REF_RE.pattern +
      br'|/?[^\[\]()<>{}/\x00\t\n\r\f %]+)')
  """Matches a single PDF token or comment in a simplistic way.

  For [...], <<...>> and (...) which contain nested delimiters, only a prefix
  of the token will be matched.
  """

  PDF_SIMPLEST_KEY_VALUE_RE = re.compile(
      br'[\x00\t\n\r\f ]*/([-+A-Za-z0-9_.]+)(?=[\x00\t\n\r\f /\[(<])'
      br'[\x00\t\n\r\f ]*('
      br'\d+[\x00\t\n\r\f ]+\d+[\x00\t\n\r\f ]+R|'
      br'\([^()\\]*\)|<(?!<)(.|\n)*?>|'
      br'\[[^%(\[\]]*\]|<<[^%(<>]*>>|'
      br'/?[-+A-Za-z0-9_.]+(?=[\x00\t\n\r\f /\[(<]|\Z))')
  """Matches a very simple PDF key--value pair, in a most simplistic way."""
  # TODO(pts): How to prevent backtracking if the regexp doesn't match?

  PDF_WHITESPACE_AT_EOS_RE = re.compile(br'[\x00\t\n\r\f ]*\Z')
  """Matches whitespace (0 or more) at end of string."""

  PDF_WHITESPACE_RE = re.compile(br'[\x00\t\n\r\f ]+')
  """Matches whitespace (1 or more)."""

  PDF_WHITESPACE_OR_HEX_STRING_RE = re.compile(br'[\x00\t\n\r\f ]+|(<<)|<(?!<)([^>]*)>')
  """Matches whitespace (1 or more) or a hex string constant or <<."""

  PDF_NAME_HEX_OR_HASHMARK_RE = re.compile(br'#([0-9a-fA-F]{2})?')
  """Matches a hex escape (#AB) in a PDF name token."""

  PDF_NONNAME_CHARS = b'/[]{}()<>%\0\t\n\r\f '
  """Contains all characters which can't be part of a PDF name.

  Same as the CFF spec disallows in a FontName.
  """

  PDF_NONNAME_CHAR_RE = re.compile(b'[%s]' % re.escape(PDF_NONNAME_CHARS))
  """Matches a single character which can't be part of a PDF name."""

  PDF_NAME_LITERAL_RE = re.compile(br'/([^\[\]{}()<>/%\x00\t\n\r\f ]+)')
  """Matches a PDF /name literal."""

  PDF_INT_AT_EOS_RE = re.compile(br'[-+]?\d+\Z')
  """Matches a PDF integer token."""

  PDF_STRING_NONSIMPLE_CHAR_RE = re.compile(r'([()\\\r])')
  """Matches PDF string literal special chars ( ) \\ \r ."""

  PDF_SIMPLE_STRING_RE = re.compile(br'\(([^()\\\r]*)\)')
  """Matches a PDF string literal without special chars ( ) \\ \r . No \Z."""

  PDF_COMMENT_OR_STRING_RE = re.compile(br'%[^\r\n]*|\(([^()\\]*)\)|(\()')
  """Matches a comment, a string simple literal or a string literal opener."""

  PDF_COMMENT_RE = re.compile(br'%[^\r\n]*')
  """Matches a single comment line without a terminator."""

  PDF_SIMPLE2_REF_RE = re.compile(br'(\d+)[\x00\t\n\r\f ]+(\d+)[\x00\t\n\r\f ]+R\b')
  """Matches `<obj> <gen> R', not allowing comments.

  TODO(pts): Remove this, in favor of PDF_SIMPLE_REF_RE.
  """

  PDF_SIMPLE_REF_RE = re.compile(br'([-+]?\d+) \d+ R\b')
  """Matches an <obj> <gen> R, separated by a single space."""

  PDF_HEX_STRING_OR_DICT_RE = re.compile(br'<<|<(?!<)([^>]*)>')
  """Matches a hex string or <<."""

  PDF_SIMPLE_TOKEN_RE = re.compile(' |(/?[^/{}\[\]()<>\0\t\n\r\f %]+)|<<|>>|[\[\]]|<([a-f0-9]*)>')
  """Matches a simple PDF token.

  PdfObj.CompressValue(data, do_emit_strings_as_hex=True emits) a string of
  simple tokens, possibly concatenated by a single space.
  """

  PDF_NAME_ABBREVIATIONS = {
      'BPC': 'BitsPerComponent',
      'CS': 'ColorSpace',
      'D': 'Decode',
      'DP': 'DecodeParms',
      'F': 'Filter',
      'H': 'Height',
      'W': 'Width',
      'IM': 'ImageMask',
      'I': 'Interpolate',  # Can also be Indexed.
      'G': 'DeviceGray',
      'RGB': 'DeviceRGB',
      'CMYK': 'DeviceCMYK',
      'AHx': 'ASCIIHexDecode',
      'A85': 'ASCII85Decode',
      'LZW': 'LZWDecode',
      'Fl': 'FlateDecode',
      'RL': 'RunLengthDecode',
      'CCF': 'CCITTFaxDecode',
      'DCT': 'DCTDecode',
  }
  """Maps an abbreviated name (in an inline image) to its full equivalent.

  From table 4.43, 4.44, ++ on page 353 of pdf_reference_1-7.pdf .
  """

  def __init__(self, other, objs=None, file_ofs=0, start=0, end_ofs_out=None, do_ignore_generation_numbers=False, is_ilstream_ok=False):
    """Initialize from other.

    If other is a PdfObj, copy everything. Otherwise, if other is a string,
    start parsing it from `X 0 obj' (ignoring the number) till endobj.

    This method is optimized, because it is called for each PDF object read.
    (Some PDF files have 100000 objects.) Most of the time we try a simple
    parser first (using a regexp), and if it cannot parse the object, then
    we revert to a generic, but slower parser.

    This method doesn't implement a validating PDF parser.

    Args:
      other: PdfObj, or str or buffer (with full obj, stream, endstream, endobj
        + garbage) or None
      objs: A dictionary mapping object numbers to existing PdfObj objects.
        These can be used for resolving `R's to build self.
      file_ofs: Offset of other + start in the file. Used for error message
        generation.
      start: Offset in other (if a string) to start parsing from.
      end_ofs_out: None or an empty array output parameter for the end offsets
        (i.e. first after `stream' + whitespace, then after `endobj' +
        whitespace).
      do_ignore_generation_numbers: bool indicating whether to ignore
        generation numbers in references when parsing this object.
      is_ilstream_ok: bool indicating whether it is OK for the obj to have a
        stream with an indirect length.
    Raises:
      PdfTokenParseError: .
      PdfUnexpectedIlStreamError: .
      PdfIndirectLengthError: .
      Exception: Many others.
    """
    self._cache = None
    if not isinstance(other, (str, bytes)):
      if isinstance(other, PdfObj):
        self._head = other.head
        self.stream = other.stream
      elif other is None:
        self._head = None
        self.stream = None
      else:
        raise TypeError(type(other))
      return

    # --- Parse the rest as a byte string.

    # Also matches and strips leading whitespace and comments after 'obj'.
    match = self.PDF_OBJ_DEF_RE.match(other, start)
    if not match:
      raise PdfTokenParseError('X Y obj expected, got %r at ofs=%s' % (other[start : start + 32], file_ofs))
    obj_def_obj_num = int(match.group(1))
    head_idx = match.end()
    try:
      if self.PDF_STREAM_OR_ENDOBJ_RE.match(other, head_idx):
        # This is just for better error reporting,
        # PDF_PREFIXED_STREAM_OR_ENDOBJ_RE in ParseTokensToSafe would also
        # catch it by not matching.
        raise PdfTokenParseError('Empty obj head.')
      head, stream_start_idx = self.parse_tokens_to_safe(other, head_idx, end_ofs_out, do_expect_endobj=True)
      # !!! Don't call CheckSafePdfTokens here by default, for speed.
      # !!! This shouldn't be catching any problems, ParseTokensToSafe should
      #     have caught all already.
      # self.check_safe_pdf_tokens(head)
    except PdfTokenParseError as e:
      # !!! TODO(pts): Traceback in Python 2.4 and 2.7 wasn't retained. Why?
      raise (e.__class__('In obj data between ofs %d and %d: %s' %
             (file_ofs, file_ofs + len(other) - start, e)), None,
             sys.exc_info()[2])
    self._head = head

    if stream_start_idx is None:
      self.stream = None
      if head.startswith(b'<<'):
        if b'/Filter' in head:
          self.set(b'Filter', None)
        if b'/DecodeParms' in head:
          self.set(b'DecodeParms', None)
        if b'/Length' in head:
          self.set(b'Length', None)
      return

    if not head.startswith(b'<<') and head.endswith(b'>>'):
      raise PdfTokenParseError('stream must have a dict head at ofs=%s' % file_ofs)
    scanner = self.LENGTH_OF_STREAM_RE.scanner(head)
    match = scanner.search()
    if not match:
      # We happily accept the invalid PDF obj
      # `<</Foo[/Length 42]>>stream...endstream' above. This is OK, since
      # we don't implement a validating PDF parser.
      # !!! Do this with proper scanning, now that it's normalized.
      # !!! Do everything below in __init__ simpler.
      raise PdfTokenParseError('stream /Length not found at ofs=%s' % file_ofs)
    if scanner.search():
      # Duplicate /Length found. We need a full parsing to figure out
      # which one we need.
      stream_length = self.get(b'Length')
      if stream_length is None:
        raise PdfTokenParseError('proper stream /Length not found at ofs=%s' % file_ofs)
      match = self.LENGTH_OF_STREAM_RE.match('/Length %s ' % stream_length)
      assert match
    if match.group(2) is None:
      stream_end_idx = stream_start_idx + int(match.group(1))
    else:
      # For testing: lme_v6.pdf (and eurotex2006.final.pdf?)
      if int(match.group(2)) != 0 and not do_ignore_generation_numbers:
        raise NotImplementedError(
            'generational refs (in /Length %s %s R) not implemented '
            'at ofs=%s' % (match.group(1), match.group(2), file_ofs))
      obj_num = int(match.group(1))
      if obj_num <= 0:
        raise PdfTokenParseError('obj num %d >= 0 expected for indirect /Length at ofs=%s' % (obj_num, file_ofs))
      if not objs or obj_num not in objs:
        if is_ilstream_ok:
          raise PdfUnexpectedIlStreamError
        exc = PdfIndirectLengthError('missing obj for indirect /Length %d 0 R at ofs=%s' % (obj_num, file_ofs))
        exc.length_obj_num = obj_num
        raise exc
      try:
        stream_length = int(objs[obj_num].head)
      except ValueError:
        raise PdfTokenParseError('indirect /Length not an integer at ofs=%s' % file_ofs)
      stream_end_idx = stream_start_idx + stream_length
      # Inline the reference to /Length
      self._head = (self._head[:match.start()] + b'/Length %d' + self._head[match.end():]) % stream_length
    endstream_str = other[stream_end_idx : stream_end_idx + 128]
    match = self.PDF_ENDSTREAM_ENDOBJ_RE.match(endstream_str)
    if not match:
      # TODO(pts): Find the last match.
      for match in self.PDF_ENDSTREAM_ENDOBJ_RE.finditer(other[stream_start_idx:len(other)]):
        pass
      if match is None:
        raise PdfTokenParseError(
            'expected endstream+endobj in obj %d at ofs=%s' %
            (obj_def_obj_num, file_ofs + stream_end_idx))
      logger.log_warning(
          'incorrect /Length fixed for obj %d: %d to %d' %
          (obj_def_obj_num, stream_end_idx - stream_start_idx, match.end(1)))
      self.set(b'Length', match.end(1))  # Trailing whitespace included.
      stream_end_idx = match.end(1) + stream_start_idx
      if end_ofs_out is not None:
        end_ofs_out.append(match.end() + stream_start_idx)
    else:
      if end_ofs_out is not None:
        end_ofs_out.append(stream_end_idx + match.end())
    self.stream = other[stream_start_idx : stream_end_idx]
    if isinstance(self.get(b'Filter'), bytes):
      self.set(b'Filter', self.expand_abbreviations(self.get(b'Filter')))

  @classmethod
  def parse_tokens_to_safe(cls, data, start=0, end_ofs_out=None,
                           do_expect_endobj=False, do_expect_startxref=False,
                           is_simple_ok=True):
    """Parses a PDF token sequence to a safe PDF token sequence.

    Args:
      data: str or buffer containing a PDF token sequence.
      start: Offset in data to start parsing.
      end_ofs_out: None or an empty array output parameter for the end offset
        `endobj' + single whitespace or for the start offset of 'startxref'
        or 'xref'.
      do_expect_endobj: bool indicating whether endobj or stream is required
        in data. If true, parsing will terminate there, and it's an error if
        it is missing.
      do_expect_startxref: bool indicating whether xref or startxref is
        required in data. If true, parsing will terminate there, and it's an
        error if it is missing.
      is_simple_ok: bool indicating whether it is OK to use the simple (and
        fast) parsing method. The simple parsing method is used if is_simple_ok
        is true and the input string is deemed (autodetected) to be simple
        enough. The simple and the complicated methods are equivalent: they
        return the same output and they raise the same exceptions (possibly
        with a different message).
    Returns:
      (safe_data, stream_start_idx) pair, where safe_data is a string
      containing a safe PDF token sequence, and stream_start_idx is the start
      index (offset) of the stream data, or None if there is no stream data
      following.
    Raises:
      PdfTokenParseError: Also subclasses of it.
    """
    stream_start_idx, end, end_for_simple = None, len(data), len(data)
    if do_expect_endobj:
      if do_expect_startxref:
        raise ValueError
      # We do the simplest and fastest parsing approach first to find
      # endobj/endstream. This covers about 90% of the objs. Notable
      # exceptions are the /Producer, /CreationDate and /CharSet strings.
      match = cls.PDF_PREFIXED_STREAM_OR_ENDOBJ_RE.search(data, start)
      if not match:
        raise PdfTokenParseError('endobj/stream not found.')
      end_for_simple = match.start(1)
    elif do_expect_startxref:
      match = cls.PDF_PREFIXED_STARTXREF_RE.search(data, start)
      if not match:
        raise PdfTokenParseError('startxref/xref not found.')
      end_for_simple = match.start(1)
      match = None
    else:
      # We need to remove leading whitespace explicitly, otherwise the rest
      # would break.
      match = cls.PDF_COMMENTS_OR_WHITESPACE_RE.match(data, start)
      if match:
        start = match.end()
      match = None  # For the simple case below.

    _whitespace_re = cls.PDF_WHITESPACE_RE
    _unsafe_string_char_re = cls.PDF_STRING_UNSAFE_CHAR_RE
    _escape_hex = cls._escape_pdf_names_in_hex_tokens_safe
    if not is_simple_ok or cls.PDF_TOKENS_NONSIMPLE_CHAR_RE.search(data, start, end_for_simple):
      # Our simple parsing approach has failed, maybe because we've
      # found the wrong (early) 'endobj' in e.g. '(endobj rest) endobj'.
      output, i = [], start
      _interesting_re = cls.PDF_TOKENS_INTERESTING_RE
      _parse_pdf_string = cls._parse_non_simple_pdf_string

      while 1:
        match = _interesting_re.search(data, i, end)
        if not match:
          if do_expect_endobj:
            raise PdfTokenParseError('Full endobj/stream not found.')
          if do_expect_startxref:
            raise PdfTokenParseError('Full startxref/xref not found.')
          output.append(data[i : end])
          if end_ofs_out is not None:
            end_ofs_out.append(end)
          break
        if i != match.start():
          output.append(data[i : match.start()])
        if match.group(1) is not None:  # Whitespace or comment.
          # output[-1] is guaranteed to be a non-empty string, because
          # we don't append empty strings, and the input doesn't start with
          # whitespace (PDF_OBJ_DEF_RE and PDF_COMMENTS_OR_WHITESPACE_RE above
          # has removed leading whitespace).
          #
          # Here match.group(1) can be the empty string, if
          # do_expect_endobj=False and there is a comment or whitespace at the
          # end of the string. The condition works correctly.
          if not (output[-1][-1] in b'<>[](){}/' or match.group(1) in b'<>[](){}/'):
            output.append(b' ')
        elif match.group(2) is not None:  # Simple string.
          if _unsafe_string_char_re.search(match.group(2)):
            output.append(b'<' + match.group(2) + b'>')
          else:
            output.append(match.group())
        elif match.group(3):  # Beginning of string.
          strdata, i = _parse_pdf_string(data, match.start(), end)
          if _unsafe_string_char_re.search(strdata):
            output.append(b'<' + strdata + b'>')
          else:
            output.append('(%s)' % strdata)
          continue  # Don't change `i' below.
        elif match.group(4):  # /name with hex-escape (#AB).
          # Like NormalizePdfName, but we don't need the extra check.
          output.append(_escape_hex(match.group(4)))
        elif match.group(5):  # An empty name token (/).
          raise PdfTokenParseError('Found empty name token.')
        elif match.group(6):  # A hex-escape (usually in a name or a keyword).
          output.append(_escape_hex(match.group(6)))
        elif match.group(7):  # A hex string literal or <<.
          if match.group() == b'<<':
            output.append(b'<<')
          else:
            if chr(data[match.end() - 1]) != '>':
              if match.end() == end:
                raise PdfTokenTruncated('Truncated hex string.')
              else:
                raise PdfTokenParseError('Invalid < token.')
            strdata = _whitespace_re.sub('', data[match.start() + 1: match.end() - 1])
            if len(strdata) & 1 != 0:
              strdata += '0'
            strdata_dec = strdata.decode('hex')
            if _unsafe_string_char_re.search(strdata_dec):
              output.append('<%s>' % strdata.lower())
            else:
              output.append('(%s)' % strdata_dec)
        elif match.group(8):
          if match.group() == b'>>':
            output.append(b'>>')
          else:
            raise PdfTokenParseError('Invalid PDF token: %r' % match.group())
        elif not match.start() or data[match.start() - 1] not in b'<>[](){}/\0\t\n\r\f ':
          # `endobj' in the middle of a name token.
          output.append(match.group()[0])
          i = match.start() + 1
          continue
        elif do_expect_endobj and (match.group().startswith(b'stream') or match.group().startswith(b'endobj')):
          if match.group().startswith(b'stream'):
            stream_start_idx = match.end(9)
          if end_ofs_out is not None:
            end_ofs_out.append(match.end())
          break
        elif do_expect_startxref and (match.group().startswith(b'startxref') or match.group().startswith(b'xref')):
          if end_ofs_out is not None:
            end_ofs_out.append(match.start())
          break
        else:
          match = cls.PDF_KEYWORD_RE.match(data, match.start())
          # Appends the 'endobj', 'stream', 'xref' or 'startxref' keyword,
          # but not the following whitespace.
          output.append(data[match.start() : match.end()])
        i = match.end()
      if output and output[-1] == b' ':
        output.pop()
      data = b''.join(output)
    else:  # A simple processing.
      if match and match.group(1).startswith(b'stream'):
        stream_start_idx = match.end()
      if end_ofs_out is not None:
        if match:
          end_ofs_out.append(match.end())
        else:
          end_ofs_out.append(end_for_simple)

      def replacement_white_space(match):
        # It's OK that match.group(2) is empty.
        a, b = match.group(1), match.group(2)
        if a in b'<>[]/' or b in b'<>[]/':
          return a
        return a + b' '

      def replacement_angle(match):
        data = match.group()
        if data == b'<<':
          return data
        if chr(data[-1]) != '>':
          if match.end() == end and not (do_expect_endobj or do_expect_startxref):
            raise PdfTokenTruncated('Truncated hex string.')
          else:
            raise PdfTokenParseError('Invalid < token.')
        data = _whitespace_re.sub(b'', data[1:-1]) # buffer(data, 1, len(data) - 2)
        if len(data) & 1 != 0:
          data += b'0'
        # data_dec = data.decode('hex') TODO: determine if I actually need this line
        if _unsafe_string_char_re.search(data):
          return b'<%s>' % data.lower()
        else:
          return b'(%s)' % data

      data = data[start: end_for_simple] # buffer(data, start, end_for_simple - start)

      # !!! Benchmark this relatively to complicated implementation.
      #     (token_parsing_speed.txt)
      #     Seems to be tolerable for pdf_reference_1-7.pdf with >100000 objs.
      # !!! Report statistics about nonsimple obj parsing percentage.
      if cls.PDF_EMPTY_NAME_TOKEN_RE.search(data):
        raise PdfTokenParseError('Found empty name token.')
      end = len(data)
      # We check for syntax errors before replacement_white_space changes '< <'
      # to '<<' etc.
      for match in cls.PDF_ANGLE_BRACKET_FOR_SIMPLE_RE.finditer(data):
        a = match.group()
        if len(a) < 2 or chr(a[-1]) not in '<>':
          if (a[0] == '<' and a[1 : 2] != '<' and end == match.end() and
              not (do_expect_endobj or do_expect_startxref)):
            raise PdfTokenTruncated('Truncated hex string.')
          else:
            raise PdfTokenParseError('Invalid < or > token.')
      # !!! Bug: add these tests:
      # to fix, because we want '<< <5c>' changed to '<<<5c>' and '<5c> >>'
      # changed to '<5c>>>'.
      # This changes '< <' to '<<' and '> >' to '>>'. It's OK here.
      data = cls.PDF_WHITESPACE_IN_SIMPLE_RE.sub(replacement_white_space, data)
      if b'#' in data:
        data = _escape_hex(data)
      else:
        data = cls.PDF_UNSAFE_NAME_IN_SIMPLE_RE.sub(lambda match: b'#%02X' % ord(match.group()), data)
      if not ((data.startswith(b'<<') and data.find(b'<', 2) < 0) or data.find(b'<') < 0):  # The `if' is just a shortcut for speed.
        end = len(data)  # Recompute it, len(data) has changed.
        data = cls.PDF_HEX_STRING_LITERAL_OR_DICT_RE.sub(replacement_angle, data)

    # !!! Add everything what RewriteToParsable supports, e.g. integer normalization.
    # !!! Remove RewriteToParsable. Not so easy, RewriteToParsable also checks balancing of << and [.
    # !!! Remove the parsing parts of CompressValue.
    # !!! Normalize dict item order? Better elsewhere, in PdfData.OptimizeObjs (rather than `obj._cache = None' in PdfData.OptimizeStreams).
    return data, stream_start_idx

  def append_to(self, output, obj_num, do_emit_short_unsafe=False):
    """Append serialized self to output list, using obj_num."""
    # TODO(pts): Test this method.
    output.append(b'%d 0 obj\n' % obj_num)
    head = self.head.strip(self.PDF_WHITESPACE_CHARS)
    if do_emit_short_unsafe:
      # Also converts strings and names to short but unsafe.
      # !!! TODO(pts): Do this conversion faster. Does it matter?
      head = self.compress_value(head, do_emit_safe_names=False, do_emit_safe_strings=False)
    output.append(head)  # Implicit whitespace later.
    space = b' ' * int(chr(head[-1]) not in '>])}')
    if self.stream is not None:
      if self._cache:
        assert self.get(b'Length') == len(self.stream)
      else:
        # Don't waste time on the proper check.
        assert b'/Length' in head
      output.append(space + b'stream\n')
      output.append(self.stream)
      # We don't need '\nendstream' after a non-compressed content stream,
      # 'Qendstream endobj' is perfectly fine (accepted by gs and xpdf).
      output.append(b'endstream endobj\n')
    else:
      output.append(space + b'endobj\n')

  def __get_head(self):
    if self._head is None and self._cache is not None:
      self._head = self.serialize_dict(self._cache)
    return self._head

  def __set_head(self, head):
    if head != self._head:  # Works for None as well.
      self._head = head
      self._cache = None

  head = property(__get_head, __set_head)

  @property
  def size(self):  # GetSize().
    # + 20 for obj...endobj, + 20 for the xref entry
    if self.stream is None:
      return len(self.head) + 40
    else:
      return len(self.head) + len(self.stream) + 52

  @classmethod
  def get_bad_numbers_fixed(cls, data):
    # !!! Remove this once PdfObj.__init__ does it.
    if data == '.':
      return '0'
    # Just convert '.' to '0' in an array.
    # We don't convert `42.' to '42.0' here.
    return cls.PDF_BAD_NUMBER_RE.sub(
        lambda match: match.group(1) + '0', data)

  @classmethod
  def is_space_needed(cls, data1, data2):
    """Return a bool indicating a space is needed between these PDF values."""
    assert data1
    assert data2

    a = b = ''

    if isinstance(data1, str):
      a = data1[-1]
    elif isinstance(data1, bytes):
      a = chr(data1[-1])
    elif isinstance(data1, int):
      a = chr(data1)[-1]

    if isinstance(data2, str):
      b = data2[0]
    elif isinstance(data2, int):
      b = chr(data2)[0]
    # We don't cate about `{' or `}', because they can't appear in PDF values.
    return not (a in ')>]' or b in '(<[/')

  @classmethod
  def get_number(cls, data):
    """Return an int, log, float or None."""
    if isinstance(data, int):
      return int(data)
    elif isinstance(data, float):
      pass
    elif not isinstance(data, str):
      return None
    elif data == '.':
      return 0
    elif re.match(r'-?\d+[.]', data):
      data = float(data[:-1])
    else:
      try:
        if '.' in data:
          data = float(data)
        else:
          return int(data)
      except ValueError:
        return None
    if isinstance(data, float) and int(data) == data:
      return int(data)
    else:
      return data

  @classmethod
  def _check_dict_head(self, head):
    """Check syntax of a head of a dict or stream obj."""
    if not head.startswith(b'<<'):
      raise PdfTokenParseError('expected a dict or stream obj: %r')
    if not head.endswith(b'>>'):
      if b'>>' in head:
        if b'endobj' in head or b'endstream' in head:
          raise PdfTokenParseError('syntax error in endobj/endstream')
        else:
          raise PdfTokenParseError('missing endobj/endstream')
      else:
        raise PdfTokenParseError('dict obj must end with >>')

  def get(self, key: bytes, default=None):
    """Get value for key if self.head is a PDF dict.

    Use self.ResolveReferences(obj.Get(...)) to resolve indirect refs.

    Args:
      key: A PDF name literal without a slash, e.g. 'ColorSpace'
      default: The value to return if key was not found. None by default.
    Returns:
      An str, bool, int, or None value, as returned by
      self.ParseSimpleValue, default, if key was not found.
    """
    if key.startswith(b'/'):
      raise TypeError('slash in the key= argument')
    if self._cache is None:
      assert self._head is not None
      self._check_dict_head(self.head)
      if (b'/' + key) not in self._head:
        # Quick return False, without having to parse.
        # TODO(pts): Special casing for /Length, we don't want to parse that.
        return None
      self._cache = self.parse_dict(self._head)
    return self._cache.get(key, default)

  def set(self, key: bytes, value, do_keep_null=False):
    """Set value to key or remove key if value is None.

    To set key to 'null', specify value='null' or value=None,
    do_keep_null=True.

    To remove key, specify value=None (and do_keep_null=False by default).
    """
    if key.startswith(b'/'):
      raise TypeError('slash in the key= argument')
    if value is None:
      if do_keep_null:
        value = b'null'
    elif value == b'null':
      pass
    elif isinstance(value, bytes):
      value = self.parse_simple_value(value)
    else:
      self.serialize_simple_value(value)  # just for the TypeError
    if self._cache is None:
      assert self._head is not None
      self._check_dict_head(self.head)
      self._cache = self.parse_dict(self._head)
    if value is None:
      if key in self._cache:
        del self._cache[key]
        self._head = None  # self.__GetHead will regenerate it.
    else:
      # It's good that we don't support isinstance(value, float), because
      # comparing NaNs would fail here.
      if self._cache.get(key) != value:
        self._cache[key] = value
        self._head = None  # self.__GetHead will regenerate it.

  def set_stream_and_compress(self, data: bytes, may_keep_old=False, is_flate_ok=True,
                              predictor_width=None, pdf=None):
    """Set self.stream, compress it and set /Length, /Filter and /DecodeParms.

    If the uncompressed version is the shortest, then clear /Filter and
    /DecodeParms.

    This method tries all the following compression methods, and picks the
    one which produces the smallest output: original, uncompressed, ZIP, ZIP
    with the PNG y-predictor, ZIP with the TIFF predictor acting as an
    y-predictor.
    """
    if not isinstance(data, bytes):
      raise TypeError

    items = [[None, 'uncompressed', PdfObj(self)]]
    items[-1][2].stream = data
    items[-1][2].set(b'Length', len(items[-1][2].stream))
    items[-1][2].set(b'Filter', None)
    items[-1][2].set(b'DecodeParms', None)
    items[-1][0] = items[-1][2].size

    if data:
      if is_flate_ok:
        items.append([None, 'zip', PdfObj(self)])
        items[-1][2].stream = zlib.compress(data, 9)
        items[-1][2].set(b'Length', len(items[-1][2].stream))
        items[-1][2].set(b'Filter', b'/FlateDecode')
        items[-1][2].set(b'DecodeParms', None)
        items[-1][0] = items[-1][2].size

      if predictor_width is not None and is_flate_ok:
        assert isinstance(predictor_width, int)
        assert len(data) % predictor_width == 0

        output = []
        output.append(b'\x00')  # no-predictor mark
        output.append(data[:predictor_width])
        i = predictor_width
        while i < len(data):
          output.append(b'\x02')  # y-predictor mark
          b = bytearray(data[i : i + predictor_width])
          k = i - predictor_width
          for j in range(predictor_width):  # Implement the y predictor.
            b[j] = (b[j] - data[k + j]) & 255
          output.append(bytes(b))
          i += predictor_width
        items.append([None, 'zip-pred10', PdfObj(self)])
        items[-1][2].stream = zlib.compress(b''.join(output), 9)
        items[-1][2].set(b'Length', len(items[-1][2].stream))
        items[-1][2].set(b'Filter', b'/FlateDecode')
        # Oddly enough, Multivalent fails if /Predictor 10 or /Predictor 11
        # is specified for the /Type /XRef obj; but it succeeds with
        # /Predictor 12. See https://github.com/pts/pdfsizeopt/issues/56
        # for example PDF 1206.3686v1.pdf .
        items[-1][2].set(b'DecodeParms', b'<</Predictor 12/Columns %d>>' % predictor_width)
        items[-1][0] = items[-1][2].size

        output = []
        output.append(data[:predictor_width])
        i = predictor_width
        while i < len(data):
          b = bytearray(data[i : i + predictor_width])
          k = i - predictor_width
          for j in range(predictor_width):  # Implement the y predictor.
            b[j] = (b[j] - data[k + j]) & 255
          output.append(bytes(b))
          i += predictor_width
        items.append([None, 'zip-pred2', PdfObj(self)])
        items[-1][2].stream = zlib.compress(b''.join(output), 9)
        items[-1][2].set(b'Length', len(items[-1][2].stream))
        items[-1][2].set(b'Filter', b'/FlateDecode')
        items[-1][2].set(b'DecodeParms', b'<</Predictor 2/Colors %d/Columns %d>>' % (predictor_width, len(data) / predictor_width))
        items[-1][0] = items[-1][2].size

      if may_keep_old:
        items.append([self.size, '0old', self])

    # def compare_str(a, b):
    #   return (a < b and -1) or (a > b and 1) or 0
    #
    # def compare_size(a, b):
    #   # Compare first by byte size, then by command name.
    #   return a[0].__cmp__(b[0]) or compare_str(a[1], b[1])
    items.sort(key=lambda m: (m[0], m[1]))
    if items[0][2] is not self:
      self.stream = items[0][2].stream
      self.set(b'Length', len(self.stream))
      self.set(b'Filter', items[0][2].get(b'Filter'))
      self.set(b'DecodeParms', items[0][2].get(b'DecodeParms'))
      if (pdf and items[0][1] == 'zip-pred2' and predictor_width > 4 and pdf.version < '1.3'):
        pdf.version = '1.3'

  @classmethod
  def parse_trailer(cls, data, start=0, end_ofs_out=None):
    """Parse PDF trailer at offset start."""
    match = PdfObj.PDF_TRAILER_RE.match(data, start)
    if not match:
      raise PdfTokenParseError('bad trailer data: %r' % data[start : start + 256])
    # We don't use match.end(), because PDF_TRAILER_RE is not smart enough
    # to find the end of the trailer. ParseTokensToSafe is smart.
    start = match.start(1)  # Start of '<<'.

    trailer_obj = PdfObj(None)
    trailer_obj.head, _ = cls.parse_tokens_to_safe(data, start=start, end_ofs_out=end_ofs_out, do_expect_startxref=True)
    # We don't remove 'Prev' here, the caller might be interested.
    return trailer_obj

  @classmethod
  def _parse_non_simple_pdf_string(
      cls, data, start, end,
      _escapes = dict(('n\n', 'r\r', 't\t', 'b\b', 'f\f', '4\4', '5\5', '6\6', '7\7'))):
    """Internal method. Use ParsePdfString instead."""
    i = start + 1
    j, output, depth = i, [], 1
    while 1:
      if j == end:
        raise PdfTokenTruncated
      c = data[j]
      if chr(c) == '(':
        depth += 1
        j += 1
      elif chr(c) == ')':
        depth -= 1
        if not depth:
          output.append(data[i : j])
          j += 1
          i = j
          break
        j += 1
      elif chr(c) == '\\':
        if j + 1 == end:
          raise PdfTokenTruncated
        c = data[j + 1]
        if chr(c) in '0123':
          output.append(data[i : j])
          if j + 2 == end or data[j + 2] not in '01234567':
            output.append(chr(int(c, 8)))
            j += 2
          elif j + 3 == end or data[j + 3] not in '01234567':
            output.append(chr(int(data[j + 1 : j + 3], 8)))
            j += 3
          else:
            output.append(chr(int(data[j + 1 : j + 4], 8)))
            j += 4
        elif chr(c) in 'nrtbf4567':
          output.append(data[i : j])
          output.append(_escapes[c])
          j += 2
        elif chr(c) == '\n':  # Skip '\n'.
          output.append(data[i : j])
          j += 2
        elif chr(c) == '\r':  # Skip '\r' or '\r\n'.
          output.append(data[i : j])
          j += 2
          if j < end and data[j] == '\n':
            j += 1
        else:
          output.append(data[i : j])
          output.append(c.to_bytes())  # Append without the backslash.
          j += 2
        i = j
      elif chr(c) == '\r':
        output.append(data[i : j])
        output.append('\n')
        j += 1
        if j < end and data[j] == '\n':
          j += 1
        i = j
      else:
        j += 1
    return b''.join(output), i

  @classmethod
  def parse_pdf_string(cls, data, start=0, end=None, is_partial_ok=False):
    """Parses a PDF string literal (hex or non-hex).

    Args:
      data: str or buffer in which to parse.
      start: Offset in data to start parsing.
      end: End (first excluded) offset in data, to end parsing at. The value
        None means len(data).
      is_partial_ok: bool indicating whether a string literal ending earlier
        than `end' is OK. If not, PdfTokenParseError will be raised.
    Returns:
      Tuple (string, idx). idx is the index in data right after the parsed
      string literal.
    Raises:
      PdfTokenTruncated: .
      PdfTokenNotString: .
      PdfTokenParseError: .
    """
    # FYI Section 3.2. of the PDF reference 1.7 says thes about CR and
    # LF in string literals:
    #
    # * The carriage return (CR) and line feed (LF) characters, also
    #   called newline characters, are treated as end-of-line (EOL)
    #   markers. The combination of a carriage return followed
    #   immediately by a line feed is treated as one EOL marker.
    #
    # * The backslash and the end-of-line marker following it are not
    #   considered part of the string.
    #
    # * If an end-of-line marker appears within
    #   a literal string without a preceding backslash, the result is
    #   equivalent to \n (regardless of whether the end-of-line marker
    #   was a carriage return, a line feed, or both).''
    if not isinstance(data, (bytes, str)):
      raise TypeError
    if end is None:
      end = len(data)
    if not (0 <= start <= end <= len(data)):
      raise ValueError('Bad offsets.')
    if start >= len(data):
      raise PdfTokenTruncated

    if chr(data[start]) == '<':
      match = cls.PDF_HEX_STRING_LITERAL_RE.match(data, start, end)
      if not match or data[match.end() - 1] != '>':
        if match and match.end() == end:
          raise PdfTokenTruncated('Truncated hex string.')
        raise PdfTokenParseError('Bad hex string.')
      i = match.end()
      data = cls.PDF_WHITESPACE_RE.sub('', data[start+1:match.end()-1])
      if (len(data) & 1) != 0:
        data += '0'
      data = data.decode('hex')
    elif chr(data[start]) == '(':
      match = cls.PDF_SIMPLE_STRING_RE.match(data, start, end)
      if match:
        data, i = match.group(1), match.end()
      else:  # A non-simple string. Parse it the hard way, counting parens.
        data, i = cls._parse_non_simple_pdf_string(data, start, end)
    else:
      raise PdfTokenNotString
    if not is_partial_ok and i != end:
      raise PdfTokenParseError('PDF string ended too early.')
    return data, i

  @classmethod
  def parse_simple_value(cls, data: bytes):
    """Parse a simple (non-composite) PDF value (or keep it as a string).

    Args:
      data: String containing a PDF token to parse (no whitespace around it).
    Returns:
      Parsed value: True, False, None, an int or a str (for
      anything else). Returns PDF literals as <hex>. If the return value is
      an str, then it's normalized.
    Raises:
      PdfTokenParseError: .
      PdfTokenTruncated: .
    """
    if not isinstance(data, bytes):
      raise TypeError
    data = data.strip(cls.PDF_WHITESPACE_CHARS)
    if data in (b'true', b'false'):
      return data == b'true'
    elif data == b'null':
      return None
    elif cls.PDF_INT_AT_EOS_RE.match(data):
      return int(data)
    elif data.startswith(b'('):
      return '<%s>' % cls.parse_pdf_string(data)[0].encode('hex')
    elif data.startswith(b'<<'):
      if not data.endswith(b'>>'):
        raise PdfTokenParseError('Unclosed dict in %r' % data)
      return data
    elif data.startswith(b'['):
      if not data.endswith(b']'):
        raise PdfTokenParseError('Unclosed array in %r' % data)
      return data
    elif data.startswith(b'<'):  # See also data.startswith('<<') above.
      # This would also work here, but it contains an unnecessary
      # .decode('hex').encode('hex'):
      # return '<%s>' % cls.ParsePdfString(data)[0].encode('hex')
      match = cls.PDF_HEX_STRING_LITERAL_RE.match(data)
      if not match or data[match.end() - 1] != '>':
        if match and match.end() == len(data):
          raise PdfTokenTruncated('Truncated hex string %r' % data)
        raise PdfTokenParseError('Bad hex string %r' % data)
      data = cls.PDF_WHITESPACE_RE.sub('', data).lower()
      if (len(data) & 1) != 0:
        return data[:-1] + '0>'
      else:
        return data
    elif data.startswith(b'/'):
      if len(data) == 1 or cls.PDF_NONNAME_CHAR_RE.search(data, 1):
        raise PdfTokenParseError('Bad PDF name token %r' % str(data))
      # Like NormalizePdfName, but we don't need the extra check.
      return cls._escape_pdf_names_in_hex_tokens_safe(data)
    elif data.endswith(b'R'):
      match = cls.PDF_REF_AT_EOS_RE.match(data)
      if not match:
        raise PdfTokenParseError('Bad reference %r' % data)
      return b'%d %d R' % (int(match.group(1)), int(match.group(2)))
    else:
      number_match = data and cls.PDF_NUMBER_AT_EOS_RE.match(data)
      if number_match:
        # Integer was already parsed above.
        # Don't parse float, we usually don't need them parsed. Thus we also
        # won't verify float.
        return cls._normalize_number(number_match)
      raise PdfTokenParseError('Syntax error in %r' % data)

  @classmethod
  def parse_simplest_dict(cls, data):
    """Parse simplest PDF token sequence to a dict mapping strings to values.

    This method returns a PDF token sequence without comments (%).

    The simplest approach involves a regexp match over the PDF token sequence.
    This cannot parse e.g. nested arrays or strings inside arrays (but
    `[' inside a string is OK). PdfTokenNotSimplest gets raised in this case,
    and parsing should be retried with ParseDict.

    Parsing of dict values is not recursive (i.e. if the value is composite,
    it is left as is as a string).

    Please note that this method doesn't implement a validating parser: it
    happily accepts some invalid PDF constructs.

    For duplicate keys, only the last key--value pair is kept.

    Args:
      data: String containing a PDF token sequence for a dict, like '<<...>>'.
    Returns:
      A dict mapping strings to values (usually strings).
    Raises:
      PdfTokenNotSimplest: If `data' cannot be parsed with this simplest
        approach.
    """
    # TODO(pts): Measure what percentage can be parsed.
    assert data.startswith('<<')
    assert data.endswith('>>')
    start = 2
    end = len(data) - 2
    dict_obj = {}
    scanner = cls.PDF_SIMPLEST_KEY_VALUE_RE.scanner(data, start, end)
    while 1:
      match = scanner.match()
      if not match:
        break
      start = match.end()
      dict_obj[match.group(1)] = cls.parse_simple_value(match.group(2))
    if not cls.PDF_WHITESPACE_AT_EOS_RE.match(data, start, end):
      raise PdfTokenNotSimplest(
          'not simplest at %d, got %r' % (start, data[start : start + 16]))
    return dict_obj

  @classmethod
  def parse_dict(cls, data):
    """Parse any PDF token sequence to a dict mapping strings to values.

    This method returns values without comments (%).

    Parsing of dict values is not recursive (i.e. if the value is composite,
    it is left as is as a string).

    Please note that this method doesn't implement a validating parser: it
    happily accepts some invalid PDF constructs.

    Please note that this method involves a ParseSimplestDict call, to parse
    the simplest PDF token sequences the fastest way possible.

    For duplicate keys, only the last key--value pair is kept.

    Whitespace is not stripped from the inner sides of [...] or <<...>>, to
    remain compatible with ParseSimplestDict.

    Args:
      data: String containing a PDF token sequence for a dict, like '<<...>>'.
        There must be no leading or trailing whitespace.
    Returns:
      A dict mapping strings to values (usually strings). The values are the
      result of ParseSimpleValue, so they can be None, int, bool or str.
    Raises:
      PdfTokenParseError:
    """
    # TODO(pts): Integate this with Get(), Set() and output optimization
    if not data.startswith(b'<<'):
      raise PdfTokenParseError('dict should start with <<')
    if not data.endswith(b'>>'):
      raise PdfTokenParseError('dict should end with >>')
    start = 2
    end = len(data) - 2

    dict_obj = {}
    scanner = cls.PDF_SIMPLEST_KEY_VALUE_RE.scanner(data, start, end)
    while 1:
      match = scanner.match()
      if not match:
        break  # Match the rest with PDF_SIMPLE_VALUE_RE.
      start = match.end()
      dict_obj[match.group(1)] = cls.parse_simple_value(match.group(2))

    # Continue with non-simplest keys.
    if not cls.PDF_WHITESPACE_AT_EOS_RE.match(data, start, end):
      list_obj = cls._parse_tokens(
          data=data, start=start, end=end,
          count_limit=end)
      if 0 != (len(list_obj) & 1):
        raise PdfTokenParseError('odd item count in dict')
      for i in range(0, len(list_obj), 2):
        key = list_obj[i]
        if not isinstance(key, bytes) or not key.startswith(b'/'):
          # TODO(pts): Report the offset as well.
          raise PdfTokenParseError('dict key expected, got %r... ' % (str(key)[0 : 16]))
        dict_obj[key[1:]] = list_obj[i + 1]

    return dict_obj

  @classmethod
  def parse_array(cls, data):
    """Parse any PDF array token sequence to a Python list.

    This method returns values without comments (%).

    Parsing of values is not recursive (i.e. if the value is composite,
    it is left as is as a string).

    Please note that this method doesn't implement a validating parser: it
    happily accepts some invalid PDF constructs.

    There is no corresponding super fast ParseSimplestArray call implemented,
    because parsing arrays is not a common operation.

    For duplicate keys, only the last key--value pair is kept.

    Whitespace is not stripped from the inner sides of [...] or <<...>>, to
    remain compatible with ParseSimplestDict.

    Args:
      data: String containing a PDF token sequence for a dict, like '<<...>>'.
        There must be no leading or trailing whitespace.
    Returns:
      A dict mapping strings to values (usually strings).
    Raises:
      PdfTokenParseError:
    """
    assert data.startswith('[')
    assert data.endswith(']')
    start = 1
    end = len(data) - 1
    return cls._parse_tokens(data, start, end, end)

  @classmethod
  def parse_token_list(cls, data, count_limit=None, start=0, end=None, end_ofs_out=None):
    """Return an array of parsed PDF values.

    Limitation: If the object end with `x y R', then count_limit is enforced
    at `x'. This is not a problem for object streams, because uncontained
    reference values are forbidden there.

    As soon as count_limit is reached, the rest of the string is not parsed,
    and it is not even checked for syntax errors.

    Args:
      data: String containing a PDF token sequence. It might contain leading
        or trailing whitespace.
    Returns:
      A list of parsed PDF values (None, int, bool or str).
    Raises:
      PdfTokenParseError:
    """
    if end is None:
      end = len(data)
    if count_limit is None:
      count_limit = end
    return cls._parse_tokens(data, start, end, count_limit, end_ofs_out=end_ofs_out)

  @classmethod
  def normalize_pdf_name(cls, name):  # !!! Add unit tests.
    """Normalize hex-escaping (#) in a PDF name."""
    if len(name) < 2 or name[:1] != '/' or cls.PDF_NONNAME_CHAR_RE.search(name, 1):
      raise PdfTokenParseError('Bad PDF name token %r.' % str(name))
    return cls._escape_pdf_names_in_hex_tokens_safe(name)

  @classmethod
  def _escape_pdf_names_in_hex_tokens_safe(cls, data: bytes, _cache = None):  # !!! Add unit tests.
    if _cache is None:
      _cache = [cls.PDF_SAFE_KEEP_HEX_ESCAPED_RE.sub(lambda match: b'#%02X' % int.from_bytes(match.group(), 'big'), (i).to_bytes(1,'big')) for i in range(256)]
    """Data is a PDF token sequence containing all strings as <hex>."""
    if b'#' in data:  # Works for both strings and buffers.
      # This unescapes e.g. #41 to A, and keeps e.g. #20 escaped. It doesn't
      # touch unescaped chars (e.g. * or A).
      try:
        data = cls.PDF_NAME_HEX_OR_HASHMARK_RE.sub(lambda match: _cache[int(match.group(1), 16)], data)
      except TypeError:  # In int(...) if match.group(1) is None.
        # It's OK to report an error here (rather than including a literal
        # #), because pdf_reference_1-7.pdf says that # must also be escaped.
        raise PdfTokenParseError('Hex error in name %r.' % data)
    m = cls.PDF_HEXTOKENS_SAFE_HEX_ESCAPE_RE.match(data)
    return cls.PDF_HEXTOKENS_SAFE_HEX_ESCAPE_RE.sub(lambda match: b'#%02X' % ord(match.group()), data) # Escapes e.g. * to #2A.

  @classmethod
  def _escape_pdf_names_in_hex_tokens_optimized(cls, data, idx=None, _cache = None):  # !!! Add unit tests.
    if _cache is None:
      _cache = [cls.PDF_SAFE_KEEP_HEX_ESCAPED_RE.sub(lambda match: b'#%02X' % int.from_bytes(match.group(), 'big'), (i).to_bytes(1, 'big')) for i in range(256)]
    """Data is a PDF token sequence containing all strings as <hex>."""
    if b'#' not in data:  # Works for both strings and buffers.
      return data
    # This unescapes e.g. #41 to A, and keeps e.g. #20 escaped. It doesn't
    # touch unescaped chars (e.g. * or A).
    try:
      return cls.PDF_NAME_HEX_OR_HASHMARK_RE.sub(lambda match: _cache[int(match.group(1), 16)], data)
    except TypeError:  # In int(...) if match.group(1) is None.
      # It's OK to report an error here (rather than including a literal
      # #), because pdf_reference_1-7.pdf says that # must also be escaped.
      raise PdfTokenParseError('Hex error in name %r at ofs=%s' % (data, idx))

  @classmethod
  def _parse_tokens(cls, data, start, end, count_limit, end_ofs_out=None):
    """Helper method to scan tokens and build values in data[start : end].

    Limitation: If the object end with `x y R', then count_limit is enforced
    at `x'. This is not a problem for object streams, because uncontained
    reference values are forbidden there.

    As soon as count_limit is reached, the rest of the string is not parsed,
    and it is not even checked for syntax errors.

    !!! Most of this can be simplified if the input is a safe PDF token
    sequence. Reuse ParseTokensToSafe.

    Raises:
      PdfTokenParseError:
    """
    if count_limit <= 0:
      return []
    list_obj = []
    scanner = cls.PDF_SIMPLE_VALUE_RE.scanner(data, start, end)
    match = scanner.match()
    while match:
      start = match.end()
      value = match.group(1)
      kind = chr(value[0])
      if kind == '%':
        if start >= end:
          raise PdfTokenParseError('unterminated comment %r at %d' % (value, start))
        match = scanner.match()
        continue
      if kind == '/':
        # It's OK that we don't apply this normalization recursively to
        # [/?Foo] etc.
        #
        # Like NormalizePdfName, but we don't need the extra check.
        value = cls._escape_pdf_names_in_hex_tokens_safe(value)
      elif kind == '(':
        # !!! get rid of this string parsing.
        if cls.PDF_STRING_NONSIMPLE_CHAR_RE.scanner(value, 1, len(value) - 1).search():
          # Parse the string in a slow way.
          end_ofs_out = []
          value1 = data[match.start(1):]  # Add more chars if needed.
          try:
            value2 = cls.rewrite_to_parsable(value1, end_ofs_out=end_ofs_out)
          except PdfTokenTruncated as exc:
            raise PdfTokenParseError('truncated string literal at %d, got %r...: %s' % (match.start(1), value1[0 : 16], exc))
          except PdfTokenParseError as exc:
            raise PdfTokenParseError('bad string literal at %d, got %r...: %s' % (match.start(1), value1[0 : 16], exc))
          assert value2.startswith(' <') and value2.endswith('>')
          value = value2[1:]
          start = match.start(1) + end_ofs_out[0]
          scanner = cls.PDF_SIMPLE_VALUE_RE.scanner(data, start, end)
          match = None
        else:
          value = cls.parse_simple_value(value)
      elif kind == '[':
        value1 = value[1 : -1]
        if b'%' in value1 or b'[' in value1 or b'(' in value1:
          # !! TODO(pts): Implement a faster solution if no % or (
          end_ofs_out = []
          value1 = data[match.start(1):]  # Add more chars if needed.
          try:
            value2 = cls.rewrite_to_parsable(value1, end_ofs_out=end_ofs_out)
          except PdfTokenTruncated as exc:
            raise PdfTokenParseError('truncated array at %d, got %r...: %s' % (match.start(1), value1[0 : 16], exc))
          except PdfTokenParseError as exc:
            raise PdfTokenParseError('bad array at %d, got %r...: %s' % (match.start(1), value1[0 : 16], exc))
          assert value2.startswith(b' [') and value2.endswith(b']')
          start = match.start(1) + end_ofs_out[0]
          # If we had `value = value2[1:] instead of the following
          # assignment, we would get the clean, pre-parsed value.
          # But we don't want that because that would be inconsistent with
          # the ('[' in value) above.
          if b'%' in value:
            value = cls.compress_value(value2[1:])
          else:
            value = data[match.start(1) : start]
          scanner = cls.PDF_SIMPLE_VALUE_RE.scanner(data, start, end)
          match = None
      elif value.startswith(b'<<'):
        value1 = value[2 : -2]
        if '%' in value1 or '<' in value1 or '(' in value1:
          # !! TODO(pts): Implement a faster solution if no % or (
          end_ofs_out = []
          value1 = data[match.start(1):]  # Add more chars if needed.
          try:
            value2 = cls.rewrite_to_parsable(value1, end_ofs_out=end_ofs_out)
          except PdfTokenTruncated as exc:
            raise PdfTokenParseError('truncated array at %d, got %r...: %s' % (match.start(1), value1[0 : 16], exc))
          except PdfTokenParseError as exc:
            raise PdfTokenParseError('bad array at %d, got %r...: %s' % (match.start(1), value1[0 : 16], exc))
          assert value2.startswith(' <<') and value2.endswith('>>')
          start = match.start(1) + end_ofs_out[0]
          if '%' in value:
            value = cls.compress_value(value2[1:])
          else:
            value = data[match.start(1) : start]
          scanner = cls.PDF_SIMPLE_VALUE_RE.scanner(data, start, end)
          match = None
      elif kind == '<':  # '<<' is handled above
        value = cls.parse_simple_value(value)
      else:
        if match.group(2):
          value = b'%d %d R' % (int(match.group(2)), int(match.group(3)))
        elif not cls.PDF_KEYWORD_OR_NUMBER_AT_EOS_RE.match(value):
          raise PdfTokenParseError('syntax error in PDF keyword or number %r at %d' % (value, match.start(1)))
        else:
          value = cls.parse_simple_value(value)  # Convert '42' to 42 etc.
      if value == 'R':
        if (len(list_obj) < 2 or
            not isinstance(list_obj[-1], int) or list_obj[-1] < 0 or
            not isinstance(list_obj[-2], int) or list_obj[-2] <= 0):
          raise PdfTokenParseError('bad indirect ref at %d, got %r after %r' % (start, data[start : start + 16], list_obj))
        list_obj[-2] = '%d %d R' % (list_obj[-2], list_obj[-1])
        list_obj.pop()
      else:
        list_obj.append(value)
        if len(list_obj) >= count_limit:
          if end_ofs_out is not None:
            end_ofs_out.append(start)
          return list_obj
      match = scanner.match()
    if not cls.PDF_WHITESPACE_AT_EOS_RE.scanner(data, start, end).match():
      # TODO(pts): Be more specific, e.g. if we get this in a truncated
      # string literal `(foo'.
      raise PdfTokenParseError('token sequence parse error at %d, got %r' % (start, data[start : start + 16]))
    if end_ofs_out is not None:
      end_ofs_out.append(start)
    return list_obj

  @classmethod
  def parse_xref_stream_widths(cls, w_value):
    """Parse the /W key of a PDF cross-reference stream.

    Args:
      w_value: Result of trailer_obj.Get('W').
    Returns:
      A tuple of 3 integers.
    Raises:
      PdfXrefStreamWidthsError:
    """
    if w_value is None:
      raise PdfTokenParseError('missing /W in xref object')
    if not isinstance(w_value, str) or not w_value.startswith('['):
      raise PdfTokenParseError('item /W in xref object is not an array')
    widths = PdfObj.parse_array(w_value)
    if (len(widths) != 3 or
        [1 for item in widths if not isinstance(item, int) or
         item < 0 or item > 10] or
        widths[1] < 1):
      raise PdfTokenParseError('bad /W array: %r' % widths)
    return tuple(widths)

  def get_xref_stream(self, xref_ofs=None, xref_obj_num=None):
    """Parse and return the xref stream data and its parameters.

    Args:
      xref_ofs: File offset of this xref object, or None. It's safe to pass
        None, the offset is used to prevent a warning.
      xref_obj_num: Object number of this xref object, or None. It's safe to
        pass None, the offset is used to prevent a warning.
    Returns:
      Tuple (w0, w1, w2, index0, index1, xref_data), where w0, w1 and w2 are
      the field lengths; index is a tuple of an even number of values:
      startidx, count for each subsection; and xref_data is the uncompressed
      xref stream data,
    Raises:
      PdfXrefStreamError:
    """
    if self.get(b'Type') != b'/XRef':
      raise PdfXrefStreamError('expected /Type/XRef for xref stream')
    widths = list(self.parse_xref_stream_widths(self.get(b'W')))
    index_value = self.get(b'Index')
    if index_value is None:
      size = self.get(b'Size')
      if not isinstance(size, int) or size < 0:
        raise PdfXrefStreamError('bad or missing /Size for xref stream')
      index = [0, size]
    else:
      if not isinstance(index_value, str) or not index_value.startswith('['):
        raise PdfTokenParseError('item /Index in xref object is not an array')
      index = tuple(PdfObj.parse_array(index_value))
      if (not index or len(index) % 2 != 0 or
          [1 for item in index if not isinstance(item, int) or item < 0] or
          [1 for i in range(1, len(index), 2) if index[i] <= 0]):
        raise PdfTokenParseError('bad /Index array: %r' % (index,))
    xref_data = self.get_uncompressed_stream()
    if len(xref_data) % sum(widths) != 0:
      raise PdfXrefStreamError('data length does not match /W: %r' % widths)
    index_item_count = sum(index[i] for i in range(1, len(index), 2))
    xref_item_count = len(xref_data) / sum(widths)
    if index_item_count != xref_item_count:
      msg = ('data length does not match /Index: '
             'xref_data_size=%d widths=%r index=%r' %
             (len(xref_data), widths, index))
      if xref_item_count < index_item_count:
        raise PdfXrefStreamError(msg)
      if xref_item_count == index_item_count + 1:
        # All this parsing is about hiding a warning, e.g. for pgfmanual.pdf in
        # https://code.google.com/p/pdfsizeopt/issues/detail?id=75
        w0, w1, w2 = widths
        i = len(xref_data) - sum(widths)
        if w0:
          f0 = PdfData.msb_first_to_integer(xref_data[i: i + w0])
        else:
          f0 = 1
        f1 = PdfData.msb_first_to_integer(xref_data[i + w0: i + w0 + w1])
        if w2:
          f2 = PdfData.msb_first_to_integer(xref_data[i + w0 + w1: i + w0 + w1 + w2])
        else:
          f2 = 0
        if (index and index[-2] + index[-1] == xref_obj_num and
            f0 == 1 and f1 == xref_ofs and f2 == 0):
          msg = None
      if msg:
        logger.log_warning(msg)
      xref_data = xref_data[:index_item_count * sum(widths)]
    widths.append(index)
    widths.append(xref_data)
    return tuple(widths)

  def get_and_clear_xref_stream(self, xref_ofs, xref_obj_num):
    """Like GetXrefStream, and removes xref stream entries from self.head."""
    xref_tuple = self.get_xref_stream(
        xref_ofs=xref_ofs, xref_obj_num=xref_obj_num)
    self.stream = None
    self.set(b'Type', None)
    self.set(b'W', None)
    self.set(b'Index', None)
    self.set(b'Filter', None)
    self.set(b'Length', None)
    self.set(b'DecodeParms', None)
    return xref_tuple

  @classmethod
  def get_reference_target(cls, data):
    """Convert `5 0 R' to 5. Return None if data is not a reference."""
    # TODO(pts): Allow comments in `data'.
    match = cls.PDF_REF_AT_EOS_RE.match(data)
    if match and int(match.group(2)) == 0:
      return int(match.group(1))
    else:
      return None

  @classmethod
  def compress_value(cls, data, obj_num_map=None, old_obj_nums_ret=None,
                     do_emit_strings_as_hex=False,
                     do_expect_postscript_name_input=False,
                     do_emit_safe_names=True,
                     do_emit_safe_strings=True):
    """Return shorter representation of a PDF token sequence.

    This method doesn't optimize integer constants starting with 0.

    Args:
      data: A PDF token sequence. !!! TODO(pts): Accept safe input only.
      obj_num_map: Optional dictionary mapping ints to ints; or a nonempty
        string (to force a specific obj_num placeholder), or None. It instructs
        this method to change all occurrences of `<key> 0 R' to `<value> 0 R'
        in data.
      old_obj_nums_ret: Optional list to which this method will append
        num for all occurrences of `<num> 0 R' in data.
        TODO(pts): Write a simpler method which returns only this.
      do_emit_strings_as_hex: bool indicating whether to emit strings as
        hex <...>.
      do_expect_postscript_name_input: bool indicating where to expect
        PostScript names on the input, i.e. don't do #AB hex escaping.
      do_emit_safe_names: bool indicating whether to emit safe PDF name tokens.
        (Safe has the same #AB hex-escaping as PdfObj and ParseSimpleValue.)
      do_emit_safe_strings: bool indicating whether to emit safe PDF string
        tokens. If either do_emit_safe_strings or do_emit_strings_as_hex is
        true, then unsafe strings are emitted as <hex>.
    Returns:
      A string containing a PDF token sequence. If do_emit_safe_names and
      do_emit_safe_strings are true, then the return value is safe. If
      do_emit_safe_names and do_emit_safe_strings and do_emit_strings_as_hex
      are false, then the return value is the most compact PDF token
      sequence possible: without superfluous whitespace; with '(' string
      literals. It may contain \n only in string literals. The most compact
      PDF token seuqnce is not always a safe token sequence, for example, it
      may contain '(())' or '/foo*'.
    Raises:
      PdfTokenParseError: .
    """
    if b'(' in data:  # Remove comments, replace strings with hex.
      output = []
      i = 0
      scanner = cls.PDF_COMMENT_OR_STRING_RE.scanner(data, 0, len(data))
      match = scanner.search()
      while match:
        if i < match.start():
          output.append(data[i : match.start()])
        i = match.end()
        if match.group(1) is not None:  # simple string literal
          output.append(b'<' + match.group(1) + b'>')
        elif match.group(2):  # complicated string literal
          end_ofs_out = []
          try:
            # Ignore return value (the parsable string).
            output.append(cls.rewrite_to_parsable(
                data=data, start=match.start(), end_ofs_out=end_ofs_out,
                do_expect_postscript_name_input=
                    do_expect_postscript_name_input))
          except PdfTokenTruncated as exc:
            raise PdfTokenParseError(
                'could not find end of string in %r: %s' %
                (data[match.start() : match.start() + 256], exc))
          i = end_ofs_out[0]
          scanner = cls.PDF_COMMENT_OR_STRING_RE.scanner(data, i, len(data))
        else:  # comment
          output.append(b' ')
        match = scanner.search()
      output.append(data[i:])
      data = b''.join(output)
    else:
      # According the the PDF reference, comments are equivalent to whitespace.
      data = cls.PDF_COMMENT_RE.sub(b' ', data)

    if do_emit_safe_names:
     if do_expect_postscript_name_input:
       data = data.replace(b'#', b'#23')
       # This escapes eg. * to #2A.
       data = cls.PDF_HEXTOKENS_SAFE_HEX_ESCAPE_RE.sub(lambda match: b'#%02X' % ord(match.group()), data)
     else:
       # Like NormalizePdfName, but we don't need the extra check.
       data = cls._escape_pdf_names_in_hex_tokens_safe(data)
    else:
      if do_expect_postscript_name_input:
        data = data.replace(b'#', b'#23')
      else:
        data = cls._escape_pdf_names_in_hex_tokens_optimized(data)

    # TODO(pts): Optimize integer constants starting with 0.

    if obj_num_map:  # nonempty dict

      def replacement_ref(match):
        obj_num = int(match.group(1))
        if old_obj_nums_ret is not None:
          old_obj_nums_ret.append(obj_num)
        if isinstance(obj_num_map, str):
          obj_num = obj_num_map
        else:
          obj_num = obj_num_map.get(obj_num, obj_num)
        if obj_num is None:
          return b'null'
        else:
          # TODO(pts): Keep the original generation number (match.group(2))
          return b'%d 0 R' % int(obj_num)

      data = cls.PDF_SIMPLE2_REF_RE.sub(replacement_ref, data)
    elif old_obj_nums_ret is not None:
      for match in cls.PDF_SIMPLE2_REF_RE.finditer(data):
        old_obj_nums_ret.append(int(match.group(1)))

    def replacement_white_or_string(match):
      """Return replacement for whitespace or hex string `match'.

      This function assumes that match is in data.
      """
      if match.group(2) is not None:  # hex string
        s = cls.PDF_WHITESPACE_RE.sub(b'', match.group(2))
        if len(s) % 2 != 0:
          s += b'0'
        # try:
        #   s = s.decode('hex')
        # except TypeError:
        #   raise PdfTokenParseError('invalid hex string %r' % s)
        if do_emit_strings_as_hex:
          return b'<' + s + b'>'
        elif do_emit_safe_strings:
          return cls.serialize_pdf_string_safe(s)
        else:
          return cls.serialize_pdf_string_unsafe(s)
      elif match.group(1):  # '<<'
        return match.group(1)
      else:  # Remove whitespace unless needed.
        if (match.start() == 0 or match.end() == len(data) or
            chr(data[match.start() - 1]) in '<>)[]{}' or  # % not needed.
            chr(data[match.end()]) in '/<>([]{}'):  # % not needed.
          return b''
        else:
          return b' '

    # This must be the last step, because it can emit unsafe strings.
    return cls.PDF_WHITESPACE_OR_HEX_STRING_RE.sub(replacement_white_or_string, data)

  @classmethod
  def simple_value_to_string(cls, value):
    if isinstance(value, str):
      return value
    elif isinstance(value, bool):  # must be above int
      return str(value).lower()
    elif isinstance(value, int):
      return str(value)
    elif value is None:
      return 'null'
    # We deliberately don't serialize float because of precision and
    # representation issues (PDF doesn't support exponential notation).
    else:
      raise TypeError

  @classmethod
  def serialize_simple_value(cls, value):
    if isinstance(value, bytes):
      if (value.startswith(b'(') or
          (value.startswith(b'<') and not value.startswith(b'<<'))):
        return cls.serialize_pdf_string_safe(cls.parse_pdf_string(value)[0])
      else:
        return value
    elif isinstance(value, bool):  # must be above int
      return b'true' if value else b'false'
    elif isinstance(value, int):
      return b'%d' % value
    elif value is None:
      return b'null'
    # We deliberately don't serialize float because of precision and
    # representation issues (PDF doesn't support exponential notation).
    else:
      raise TypeError

  @classmethod
  def serialize_dict(cls, dict_obj):
    """Serialize a dict (such as in PdfObj.head) to a PDF dict string.

    Please note that this method doesn't normalize or optimize the dict values
    (it doesn't even remove leading and trailing whitespace). To get that, use
    cls.CompressValue(cls.RewriteToParsable(cls.SerializeDict(dict_obj))),
    of which cls.RewriteToParsable is slow.
    """
    output = [b'<<']
    for key in sorted(dict_obj):
      output.append(b'/' + key)
      value = cls.serialize_simple_value(dict_obj[key])
      if chr(value[0]) not in '<({[/\0\t\n\r\f %':
        output.append(b' ')
      output.append(value)
    output.append(b'>>')
    return b''.join(output)

  @classmethod
  def serialize_pdf_string_safe(cls, data):
    """Serializes a string as a PDF string: (...) if safe, otherwise <...>."""
    if cls.PDF_STRING_UNSAFE_CHAR_RE.search(data):
      return b'<' + data + b'>'
    else:
      return b'(' + data + b')'

  @classmethod
  def serialize_pdf_string_unsafe(cls, data: bytes):
    """Escape a string to the shortest possible PDF string literal.

    Args:
      data: An arbitrary byte string (str) (not a PDF string).
    Results:
      A string containing a PDF token. Please note that it won't always be a
      safe string, e.g. '(\n)' and '(())' are both unsafe. To get a safe string
      as result, use SerializePdfStringSafe.
    """
    if not isinstance(data, bytes):
      raise TypeError
    # We never emit hex strings (e.g. <face>), because they cannot ever be
    # shorter than the literal binary string.
    no_open = b'(' not in data
    no_close = b')' not in data
    if no_open or no_close:
      # No way to match parens.
      if no_open and no_close:
        data = b'(' + data.replace(b'\\', b'\\\\') + b')'
        return data.replace(b'\r', b'\\r')
      else:
        data = '(%s)' % cls.PDF_STRING_NONSIMPLE_CHAR_RE.sub(r'\\\1', data)
        return data.replace('\\\r', '\\r')
    close_remaining = data.count(')')
    depth = 0
    output = [b'(']
    i = j = 0
    while j < len(data):
      c = data[j]
      if (c == '\\' or
          (c == ')' and depth == 0) or
          (c == '(' and close_remaining <= depth)):
        output.append(data[i : j])  # Flush unescaped.
        output.append('\\' + c)
        if c == ')':
          close_remaining -= 1
        j += 1
        i = j
      else:
        if c == '(':
          depth += 1
        elif c == ')':
          depth -= 1
          close_remaining -= 1
        j += 1
    output.append(data[i:])
    output.append(')')
    assert depth == 0
    assert close_remaining == 0
    data = ''.join(output)
    # Without this replacement, (\r\n) would become just \n (EOL).
    return data.replace('\r', '\\r')

  @classmethod
  def pdf_to_ps_name(cls, data, is_nonname_char_ok=False):
    """Converts a PDF name (in data) to a PostScript name.

    The most important conversion step is converting hex escapes (#AB) to
    their literal form, e.g. '/pedal.#2A' becomes '/pedal.*'.

    Args:
      data: String containing a PDF name (can start with /).
      is_nonname_char_ok: If a nonname char (e.g. '{') is encountered, emit
          '<...>cvn' instead of raising ValueError.
    Returns:
      String containing the equivalent PostScript name.
    Raises:
      ValueError: If there is no PostScript name which can represent this
        PDF name. (Maybe keep it escaped then, in a separate function?)
      PdfTokenParseError: If data is not a valid PDF name.
    """
    data_size = len(data)
    data = data.lstrip('/')
    slash_count = data_size - len(data)
    if slash_count > 1:
      raise PdfTokenParseError('Too many leading slashes in name: %r' % data)
    if not data:
      raise ValueError('Empty name: %r' % data)
    try:
      data = cls.PDF_NAME_HEX_OR_HASHMARK_RE.sub(lambda match: chr(int(match.group(1), 16)), data)
    except TypeError:  # In int(...) if match.group(1) is None.
      raise PdfTokenParseError('Invalid hex escape in PDF name.')
    match = cls.PDF_NONNAME_CHAR_RE.search(data)
    if match:
      if is_nonname_char_ok and slash_count == 1:
        # This happens in https://github.com/pts/pdfsizeopt/issues/28
        # with /FontName/YHKXAA+#7B#7D . This shouldn't matter anyway,
        # because /FontName gets overwritten to Obj000.... in
        # psproc.TYPE1C_GENERATOR.
        return '<%s>cvn' % data.encode('hex')
      raise ValueError('Char not allowed in PostScript name: %r' % match.group())
    return '/' * slash_count + data

  @classmethod
  def parse_value_recursive(cls, data, do_expect_postscript_name_input=False):
    """Parse PDF token sequence data to a recursive Python structure.

    As a relaxation, numbers are allowed as dict keys.

    Contrary to the function name, the implementation is not recursive.

    Args:
      data: String containing a PDF token sequence.
      do_expect_postscript_name_input: bool indicating where to expect
        PostScript names on the input, i.e. don't do #AB hex escaping.
    Returns:
      A recursive Python data structure.
    Raises:
      PdfTokenParseError
    """
    # PdfObj.CompressValue converts some characters in names to hex,
    # thus e.g. /pedal.* becomes /pedal.#2A.
    data = PdfObj.compress_value(
        data, do_emit_strings_as_hex=True, do_emit_safe_names=True,
        do_expect_postscript_name_input=do_expect_postscript_name_input)
    scanner = PdfObj.PDF_SIMPLE_TOKEN_RE.scanner(data)
    match = scanner.match()
    last_end = 0
    stack = [[]]
    # TODO(pts): Reimplement this using a stack.
    while match:
      last_end = match.end()
      token = match.group()
      if token == '<<':
        stack.append({})
        match = scanner.match()
        continue
      elif token == '[':
        stack.append([])
        match = scanner.match()
        continue
      elif token == ' ':
        match = scanner.match()
        continue
      elif match.group(1):
        try:
          token = int(token)
        except ValueError:
          if token == 'true':
            token = True
          elif token == 'false':
            token = False
          elif token == 'null':
            token = None
      elif token == '>>':
        if not isinstance(stack[-1], dict):
          raise PdfTokenParseError('unexpected dict-close')
        token = stack.pop()
      elif token == ']':
        if not isinstance(stack[-1], list):
          raise PdfTokenParseError('unexpected array-close')
        token = stack.pop()
        if not stack:
          raise PdfTokenParseError('unexpected array-close at top level')
      # Otherwise token is a hex string constant. Keep it as is (in hex).
      if stack[-1] is None:  # token is a value in a dict
        stack.pop()
        stack[-2][stack[-1]] = token
        stack.pop()
      elif isinstance(stack[-1], dict):  # token is a key in a dict
        if isinstance(token, str) and token[0] == '/':
          stack.append(token[1:])
        else:
          stack.append(token)
        stack.append(None)
      else:  # token is an item in an array
        stack[-1].append(token)
      match = scanner.match()

    if last_end != len(data):
      raise PdfTokenParseError('syntax error at %r...' % data[last_end : last_end + 32])
    if len(stack) != 1:
      raise PdfTokenParseError('data structures not closed')
    token = stack.pop()
    if not token:
      raise PdfTokenParseError('no values received')
    if len(token) > 1:
      raise PdfTokenParseError('multiple values received')
    return token[0]

  def has_image_to_hide(self):
    """Return bool indicating if we contain /Image to hide from Multivalent."""
    head = self._head
    if head is not None:
      if '/Subtype' not in head or '/Image' not in head or '/Filter' not in head:
        return False
    if self.get(b'Subtype') != b'/Image':
      return False
    filter_value = self.get(b'Filter')
    return isinstance(filter_value, str) and filter_value[0] in '[/'

  @classmethod
  def expand_abbreviations(cls, data):
    """Expands /Fl to /FlateDecode, /IM to /ImageMask etc."""
    _abbrs = cls.PDF_NAME_ABBREVIATIONS
    return cls.PDF_NAME_LITERAL_RE.sub(lambda match: b'/' + _abbrs.get(match.group(1), match.group(1)), data)

  @classmethod
  def _normalize_number(cls, number_match):
    """Normalizes a number, returns the string representation."""
    # From the PDF reference: Note: PDF does not support the PostScript
    # syntax for numbers with nondecimal radices (such as 16#FFFE) or in
    # exponential format (such as 6.02E23).

    # Convert the number to canonical (shortest) form.
    token = (number_match.group(1) or '') + number_match.group(2)
    if '.' in token:
      token = token.rstrip('0')
      if token.endswith('.'):
        token = token[:-1]  # Convert real to integer: '42.' -> '42'
    if token in ('', '-'):
      token = '0'
    return token

  PDF_CLASSIFY = [40] * 256
  """Mapping a 0..255 byte to a character type used by RewriteToParsable.

  * PDF whitespace(0) is  [\\000\\011\\012\\014\\015\\040]
  * PDF separators(10) are < > { } [ ] ( ) / %
  * PDF regular(40) character is any of [\\000-\\377] which is not whitespace
    or separator.
  """

  PDF_CLASSIFY[ord('\0')] = PDF_CLASSIFY[ord('\t')] = 0
  PDF_CLASSIFY[ord('\n')] = PDF_CLASSIFY[ord('\f')] = 0
  PDF_CLASSIFY[ord('\r')] = PDF_CLASSIFY[ord(' ')] = 0
  PDF_CLASSIFY[ord('<')] = 10
  PDF_CLASSIFY[ord('>')] = 11
  PDF_CLASSIFY[ord('{')] = 12
  PDF_CLASSIFY[ord('}')] = 13
  PDF_CLASSIFY[ord('[')] = 14
  PDF_CLASSIFY[ord(']')] = 15
  PDF_CLASSIFY[ord('(')] = 16
  PDF_CLASSIFY[ord(')')] = 17
  PDF_CLASSIFY[ord('/')] = 18
  PDF_CLASSIFY[ord('%')] = 19

  @classmethod
  def rewrite_to_parsable(
      cls, data, start=0,
      end_ofs_out=None, do_terminate_obj=False,
      do_expect_postscript_name_input=False):
    """Rewrite PDF token sequence so it will be easier to parse by regexps.

    Please note that this method is very slow. Use ParseSimpleValue or
    ParseDict (or Get) or ParseValueRecursive to get faster results most of
    the time. In the complicated case, those methods will call
    RewriteToParsable to do the hard work of proper parsing. You can avoid the
    complicated case if you don't have comments or `(...)' string constants in
    the token sequence. Hex string constants (<...>) are OK.

    Parsing stops at `stream', `endobj' or `startxref', which will also
    be returned at the end of the string.

    This code is based on pdf_rewrite in pdfdelimg.pl, which is based on
    pdfconcat.c . Lesson learned: Perl is more compact than Python; Python is
    easier to read than Perl.

    This method doesn't check the type of dict keys, the evenness of dict
    item size (i.e. it accepts dicts of odd length, e.g. `<<42>>') etc.

    Please don't change ``parsable'' to ``parsable'', see
    http://en.wiktionary.org/wiki/parsable .

    Args:
      data: str or buffer containing a PDF token sequence.
      start: Offset in data to start the parsing at.
      end_ofs_out: None or a list for the first output byte
        (which is unparsed) offset to be appended. Terminating whitespace is
        not included, except for a single whitespace is only after
        do_terminate_obj.
      do_terminate_obj: bool indicating whether look for and include the
        `stream' or `endobj' (or any other non-literal name)
        plus one whitespace (or \\r\\n) at end_ofs_out
        (and in the string).
      do_expect_postscript_name_input: bool indicating where to expect
        PostScript names on the input, i.e. don't do #AB hex escaping.
    Returns:
      Nonempty string containing a PDF token sequence which is easier to
      parse with regexps, because it has additional whitespace, it has no
      funny characters, and it has strings escaped as hex. The returned string
      starts with a single space. An extra \\n is inserted in front of each
      name key of a top-level dict.
    Raises:
      PdfTokenParseError: TODO(pts): Report the error offset as well.
      PdfTokenTruncated:
    """
    # !! precompile regexps in this method (although sre._compile uses cache,
    # but if flushes the cache after 100 regexps)
    data_size = len(data)
    i = start
    if data_size <= start:
      raise PdfTokenTruncated
    output: list(bytes) = []
    # Stack of '[' (list) and '<' (dict)
    stack: list(bytes) = [b'-']
    if do_terminate_obj:
      stack[:0] = [b'.']

    while stack:
      if i >= data_size:
        raise PdfTokenTruncated('structures open: %r' % stack)

      o = cls.PDF_CLASSIFY[data[i]]
      if o == 0:  # whitespace
        i += 1
        while i < data_size and cls.PDF_CLASSIFY[ord(data[i])] == 0:
          i += 1
      elif o == 14:  # [
        stack.append(b'[')
        output.append(b' [')
        i += 1
      elif o == 15:  # ]
        item = stack.pop()
        if item != b'[':
          raise PdfTokenParseError('got list-close, expected %r' % item)
        output.append(b' ]')
        i += 1
        if stack[-1] == b'-':
          stack.pop()
      elif o in (18, 40):  # name or /name or number
        # TODO(pts): Be more strict on PDF token names.
        j = i
        p = o == 18
        i += 1
        while i < data_size and cls.PDF_CLASSIFY[data[i]] == 40:
          i += 1
        if chr(data[j]) == '/':
          token = data[j + 1 : i]
          if not token:
            raise PdfTokenTruncated('Empty PDF name token.')
          if not do_expect_postscript_name_input:
            try:
              token = cls.PDF_NAME_HEX_OR_HASHMARK_RE.sub(lambda match: chr(int(match.group(1), 16)), token)
            except TypeError:  # In int(...) if match.group(1) is None.
              raise PdfTokenParseError('Invalid hex escape in PDF name %r' % ('/' + token))
          token = b'/' + cls.PDF_SAFE_KEEP_HEX_ESCAPED_RE.sub(lambda match: '#%02X' % ord(match.group()), token)
        else:
          token = data[j : i]
          if token != 'R' and not cls.PDF_KEYWORD_OR_NUMBER_AT_EOS_RE.match(token):
            raise PdfTokenParseError('Invalid character in keyword or number %r' % token)

        number_match = cls.PDF_NUMBER_AT_EOS_RE.match(token)
        if number_match:
          output.append(b' ' + cls._normalize_number(number_match))
        else:
          output.append(b' ' + token)

        if number_match or token[0] == '/' or token in ('true', 'false', 'null', 'R'):
          if token == 'R' and (
             len(output) < 3 or
             not re.match(r' -?\d+\Z', output[-2]) or
             not re.match(r' -?\d+\Z', output[-3])):
            raise PdfTokenParseError('invalid R after %r' % output[-2:])
          if stack[-1] == b'-':
            if re.match(' -?\d+\Z', output[-1]):
              # We have parsed `5' from `5 6 R', try to find the rest.
              # TODO(pts): raise PdfTokenTruncated if not available?
              match = cls.REST_OF_R_RE.match(data, i, len(data))
              if match:
                num2 = int(match.group(1))
                if int(output[-1]) <= 0 or num2 < 0:
                  raise PdfTokenParseError('invalid R: %s %s' % (output[-1], num2))
                output.append(' %s R' % num2)
                i = match.end()
            stack.pop()
        else:
          # TODO(pts): Support parsing PDF content stream operators.
          if stack[-1] != b'.':
            raise PdfTokenParseError('invalid operator %r with stack %r' % (token, stack))
          stack.pop()
          if i == len(data):
            raise PdfTokenTruncated
          elif data[i] == b'\r':
            i += 1
            if i == data_size:
              if output[-1] == b' stream':
                raise PdfTokenTruncated('missing \\n after \\r')
            elif data[i] == b'\n':  # Skip over \r\n.
              i += 1
          elif cls.PDF_CLASSIFY[ord(data[i])] == 0:
            i += 1  # Skip over whitespace.
      elif o == 11:  # >
        i += 1
        if i == data_size:
          raise PdfTokenTruncated
        if data[i] != '>':
          raise PdfTokenParseError('dict-close expected')
        item = stack.pop()
        if item != '<':
          raise PdfTokenParseError('got dict-close, expected %r' % item)
        output.append(b' >>')
        i += 1
        if stack[-1] == b'-':
          stack.pop()
      elif o == 10:  # <
        i += 1
        if i == data_size:
          raise PdfTokenTruncated
        if data[i] == '<':
          stack.append(b'<')
          output.append(b' <<')
          i += 1
        else:  # A hex string literal.
          # This would also work here, but it contains an unnecessary
          # .decode('hex').encode('hex'):
          # s, i = cls.ParsePdfString(
          #     data, i - 1, data_size, is_partial_ok=True)
          # output.append(' <%s>' % s.encode('hex'))
          match = cls.PDF_HEX_STRING_LITERAL_RE.match(data, i - 1, data_size)
          if not match or data[match.end() - 1] != '>':
            if match and match.end() == data_size:
              raise PdfTokenTruncated('Truncated hex string.')
            raise PdfTokenParseError('Bad hex string.')
          j = match.end()
          s = cls.PDF_WHITESPACE_RE.sub('', data[i:j-1])
          output.append(' <%s%s>' % (s.lower(), '0' * (len(s) & 1)))
          i = j
          del s  # Save memory.
          if stack[-1] == b'-':
            stack.pop()
      elif o == 16:  # A (...) string literal.
        s, i = cls.parse_pdf_string(data, i, data_size, is_partial_ok=True)
        output.append(b' <' + s + b'>')
        del s  # Save memory.
        if stack[-1] == b'-':
          stack.pop()
      elif o == 19:  # A single-line comment.
        while i < data_size and data[i] != '\r' and data[i] != '\n':
          i += 1
        if i < data_size:
          i += 1  # Don't increase it further.
      else:
        raise PdfTokenParseError('syntax error, expecting PDF token, got %r' % data[i])

    assert i <= data_size
    output_data = b''.join(output)
    assert output_data
    if end_ofs_out is not None:
      end_ofs_out.append(i)
    return output_data

  def copy_stream_obj(self, objs=None):
    """Returns a new PdfObj containing just the stream of self."""
    if self.stream is None:
      raise ValueError('Missing stream in obj.')
    obj = type(self)('1 0 obj<<>>endobj')
    if not self.has_uncompressed_stream():
      if objs is None:
        objs = {}
      # Don't copy e.g. `/MetaData <n> 0 R', which Type1CParser won't be
      # able to resolve.
      for name in ('Filter', 'DecodeParms'):
        obj.set(name, self.resolve_references(self.get(name), objs=objs))
    obj.stream = self.stream
    obj.set(b'Length', len(obj.stream))
    return obj

  def has_uncompressed_stream(self):  # !!! Add unit tests.
    """Returns a bool indicating whether this obj has an uncompressed stream."""
    return self.stream is not None and (b'/Filter' not in self.head or self.get(b'Filter') in (None, b'[]'))

  def get_uncompressed_stream(self, objs=None):
    """Returns the uncompressed stream data in this obj.

    Args:
      objs: None or a dict mapping object numbers to PdfObj objects. It will be
        passed to ResolveReferences.
    Returns:
      A string containing the stream data in this obj uncompressed.
    Raises:
      FilterNotImplementedError: .
      FilterError: .
    """
    if self.has_uncompressed_stream():
      assert self.stream is not None
      return self.stream
    filter_value = self.get(b'Filter')
    decodeparms = self.get(b'DecodeParms') or b''
    if objs is None:
      objs = {}
    filter_value = self.resolve_references(filter_value, objs)
    decodeparms = self.resolve_references(decodeparms, objs)
    if not isinstance(filter_value, bytes):
      raise FilterError('/Filter is not a valid type: %r' % (filter_value,))
    if not isinstance(decodeparms, bytes):
      raise FilterError('/DecodeParms is not a valid type.')
    if filter_value in (b'/FlateDecode', b'[/FlateDecode]') and b'/Predictor' not in decodeparms:
      try:
        return permissive_zlib_decompress(self.stream)
      except zlib.error as e:
        raise FilterError('Flate decompression error: %s' % e)
    is_gs_ok = True  # TODO(pts): Add command-line flag to disable.
    if not is_gs_ok:
      raise FilterNotImplementedError('filter not implemented: ' + filter_value)
    if '/JBIG2Decode' in filter_value and '/JBIG2Globals' in decodeparms:
      raise FilterNotImplementedError('/JBIG2Globals not supported.')

    ps_file_name = None
    tmp_file_name = TMP_PREFIX + 'filter.tmp.bin'
    f = open(tmp_file_name, 'wb')
    write_ok = False
    try:
      f.write(self.stream)
      write_ok = True
    finally:
      f.close()
      if not write_ok:
        os.remove(tmp_file_name)
    decodeparms_pair = ''
    if decodeparms:
      decodeparms_pair = '/DecodeParms ' + decodeparms

    # !! batch all decompressions, so we don't have to run gs again.

    gs_code = (
        '/i INFN(r)file<</CloseSource true '
        '/Intent 2/Filter %s%s>>/ReusableStreamDecode filter def '
        '/o(%%stdout)(w)file def/s 4096 string def '
        '{i s readstring exch o exch writestring not{exit}if}loop '
        'o closefile quit' %
        (filter_value, decodeparms_pair))
    if sys.platform.startswith('win'):
      # TODO(pts): If tmp_file_name contains funny characters, Ghostscript
      # will fails with data == ''. Fix it (possibly not use -s...="..." on
      # Windows?).
      ps_file_name = TMP_PREFIX + 'filter.tmp.ps'
      f = open(ps_file_name, 'wb')
      try:
        f.write(gs_code)
      finally:
        f.close()
      gs_defilter_cmd = (
          '%s -dNODISPLAY -sINFN=%s -q -P- %s' %
          (get_gs_command(), shell_quote_file_name(tmp_file_name, is_gs=True),
           shell_quote_file_name(ps_file_name, is_gs=True)))
    else:
      gs_defilter_cmd = (
          '%s -dNODISPLAY -sINFN=%s -q -P- -c %s' %
          (get_gs_command(), shell_quote_file_name(tmp_file_name, is_gs=True),
           ShellQuote(gs_code)))
    logger.log_proportional_info(
        'decompressing %d bytes with Ghostscript '
        '/Filter%s%s' % (len(self.stream), filter_value, decodeparms_pair))
    sys.stdout.flush()
    f = os.popen(RedirectOutput(gs_defilter_cmd, mode=True), 'rb')
    # On Windows, data would start with 'Error: ' on a Ghostscript error, and
    # data will be '' if gswin32c is not found.
    data = f.read()  # TODO(pts): Handle IOError etc.
    if f.close():
      raise FilterError(
          'Ghostscript decompression with filter %r failed: %s (%r)' %
          (filter_value, gs_defilter_cmd, data))
    os.remove(tmp_file_name)
    if ps_file_name:
      os.remove(ps_file_name)
    return data

  @classmethod
  def resolve_references(cls, data, objs, do_strings=False):
    """Resolve references (<x> <y> R) in a PDF token sequence.

    As a side effect, this function may remove comments and whitespace
    from data.

    Args:
      data: A string containing a PDF token sequence; or None, or an int
        or a float or True or False.
      objs: Dictionary mapping object numbers to PdfObj instances.
      do_strings: bool indicating whether to embed the referred
        streams as strings.
    Returns:
      new_data, which can be an int, a float, a bool, None, or an str.
      ParseSimpleValue is used on new_data (to convert str to other types) if
      data is a str containing a single reference (R).
    Raises:
      PdfTokenParseError:
      PdfReferenceTargetMissing:
      TypeError:
    """
    # !! always do a ResolveReferences to flatten /Filter and /DecodeParms.
    if not isinstance(objs, dict):
      raise TypeError
    if data is None or isinstance(data, int) or isinstance(data, float) or isinstance(data, bool):
      return data
    if not isinstance(data, bytes):
      raise TypeError
    if not (b'R' in data and cls.PDF_REF_RE.search(data)): # cls.PDF_END_OF_REF_RE.search(data) and
      # Shortcut if there are no references in data.
      return data

    current_obj_nums = []

    def replacement(match):
      obj_num = int(match.group(1))
      if obj_num < 1:
        raise PdfTokenParseError('invalid object number: %d' % obj_num)
      gen_num = int(match.group(2))
      if gen_num != 0:
        raise PdfTokenParseError('invalid generation number: %d' % gen_num)
      obj = objs.get(obj_num)
      if obj is None:
        raise PdfReferenceTargetMissing('missing object: %d 0 obj' % obj_num)
      if obj.stream is None:
        new_data = obj.head.strip(cls.PDF_WHITESPACE_CHARS)
        if ('R' in new_data and cls.PDF_END_OF_REF_RE.search(new_data) and
            cls.PDF_REF_RE.search(new_data)):
          # Do the recursive replacement in new_data.
          if obj_num in current_obj_nums:
            current_obj_nums.append(obj_num)
            raise PdfReferenceRecursiveError(
                'recursive reference chain: %r' % current_obj_nums)
          current_obj_nums.append(obj_num)
          if '%' in new_data or '(' in new_data:  # ')'
            new_data = cls.compress_value(new_data, do_emit_strings_as_hex=True)
            new_data = cls.PDF_REF_RE.sub(replacement, new_data)
            new_data = cls.compress_value(new_data)
          else:
            new_data = cls.PDF_REF_RE.sub(replacement, new_data)
          current_obj_nums.pop()
        elif '%' in new_data:
          # Remove trailing comment.
          new_data = cls.compress_value(new_data)
        return new_data
      else:
        if not do_strings:
          raise UnexpectedStreamError('unexpected stream in: %d 0 obj' % obj_num)
        return obj.serialize_pdf_string_safe(obj.get_uncompressed_stream(objs=objs))


    match = cls.PDF_REF_AT_EOS_RE.match(data)
    if match:  # Shortcut and type conversion.
      return cls.parse_simple_value(replacement(match))

    data0 = data
    if '(' in data or '%' in data:  # ')'
      # !!! Is this necessary? Can `data' not be a safe PDF token sequence?
      data = cls.compress_value(data, do_emit_strings_as_hex=True)
      # Compress strings back to non-hex once the references are
      # resolved.
      do_compress = True
    else:
      do_compress = False
    # There is no need to add whitespace around the replacement, good.
    # TODO(pts): If the replacement for a reference is a `(string)' (or an
    # array etc.), then remove the whitespace around the `<x> <y> R'.
    data = cls.PDF_REF_RE.sub(replacement, data)
    if do_compress:
      data = cls.compress_value(data)
    return data

  @classmethod
  def pdf_rstrip_buffer(cls, data, start, end):
     """Return a buffer of data[start : end] with whitespace rstripped."""
     assert start >= 0
     while end > start and data[end - 1] in cls.PDF_WHITESPACE_CHARS:
       end -= 1
     return data[start:end]

  def parse_obj_stm(self, obj_num):
    """Parses a /Type/ObjStm trailer_obj.

    Args:
      obj_num: Object number, used only in exception texts.
    Returns:
      Tuple (compressed_obj_nums, compressed_obj_headbufs), both of items
      being lists of the same size, the first containing object numbers, the
      second buffer (Python) objects containing the head of each PDF object.
    Raises:
      PdfXrefStreamError:
      NotImplementedError:
    """
    if self.get(b'Type') != b'/ObjStm':
      raise PdfXrefStreamError('expected /Type/ObjStm for obj %d' % obj_num)
    n = self.get(b'N')  # Number of objects in self.
    if n is None:
      raise PdfXrefStreamError('missing /N in objstm obj %d' % obj_num)
    if not isinstance(n, int) or n < 1:
      raise PdfXrefStreamError('invalid /N in objstm obj %d %r' % obj_num)
    first = self.get(b'First')  # Offset of the first object.
    if first is None:
      raise PdfXrefStreamError('missing /First in objstm obj %d' % obj_num)
    if not isinstance(first, int) or first <= 0:
      raise PdfXrefStreamError('invalid /First in objstm obj %d' % obj_num)

    # Probably we can just ignore /Extends, at least we can do it for
    # http://www.oreilly.com/web-platform/free/files/python-web-frameworks.pdf
    # Commenting out the check below proactively.
    #if self.Get('Extends') is not None:
    #  raise NotImplementedError('/Extends in /Type/ObjStm not implemented')

    # TODO(pts): Handle the various exceptions raised by
    #            trailer_obj.GetUncompressedStream().
    objstm_data = self.get_uncompressed_stream()
    rstrip_buffer = self.pdf_rstrip_buffer
    end_ofs_ary = []
    numbers = PdfObj.parse_token_list(
        objstm_data, 2 * n, end_ofs_out=end_ofs_ary)
    end_ofs = end_ofs_ary[0]
    match = PdfObj.PDF_COMMENTS_OR_WHITESPACE_RE.match(objstm_data, end_ofs)
    if match:  # Skip whitespace and comments after the last number.
      # TODO(pts): Maybe skip only one character of whitespace?
      end_ofs = match.end()
    if first < end_ofs:
      logger.log_warning('first too early in objstm obj %d: first=%d end_ofs=%d' % (obj_num, first, end_ofs))
    if len(numbers) != 2 * n:
      raise PdfXrefStreamError(
          'expected %d, but got %d values in token sequence objstm obj %d' %
          (2 * n, len(numbers), obj_num))
    compressed_obj_nums = []
    # List of (str) buffer objects corresponding to the PDF token sequence
    # string in the respective compressed_obj_nums item.
    compressed_obj_headbufs = []
    prev_offset = -1
    for i in range(0, len(numbers), 2):
      compressed_obj_num = numbers[i]
      compressed_obj_ofs = numbers[i + 1]
      if not isinstance(compressed_obj_num, int):
        raise PdfXrefStreamError('expected int compressed_obj_num in objstm obj %d' % obj_num)
      if not isinstance(compressed_obj_ofs, int):
        raise PdfXrefStreamError('expected int compressed_obj_ofs in objstm obj %d' % obj_num)
      if compressed_obj_num < 1:
        raise PdfXrefStreamError('bad compressed_obj_num %d in objstm obj %d' % obj_num)
      if compressed_obj_ofs < 0 or compressed_obj_ofs + first >= len(objstm_data):
        raise PdfXrefStreamError('bad compressed_obj_obs %d in objstm obj %d' % obj_num)
      compressed_obj_ofs += first
      compressed_obj_nums.append(compressed_obj_num)
      # Although the PDF spec doesn't say, we assume that compressed
      # objects don't overlap. This is reasonable, because
      # the PDF spec requires increasing offsets.
      if prev_offset > 0:
        compressed_obj_headbufs.append(rstrip_buffer(objstm_data, prev_offset, compressed_obj_ofs))
      prev_offset = compressed_obj_ofs
    if prev_offset > 0:
      compressed_obj_headbufs.append(rstrip_buffer(objstm_data, prev_offset, len(objstm_data)))
    assert len(compressed_obj_nums) == len(compressed_obj_headbufs)
    return compressed_obj_nums, compressed_obj_headbufs
