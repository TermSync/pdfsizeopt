import re
import zlib
import struct

from lib.pdfsizeopt.util import *
from lib.pdfsizeopt.PdfObj import PdfObj

logger = Logger()

class PdfData(object):

  __slots__ = [
    'objs', 'trailer', 'version', 'file_name', 'file_size', 'do_ignore_generation_numbers', 'has_generational_objs'
  ]

  def __init__(self, do_ignore_generation_numbers=False):
    self.do_ignore_generation_numbers = bool(do_ignore_generation_numbers)
    self.has_generational_objs = False
    # Maps an object number to a PdfObj
    self.objs = {}
    # None or a PdfObj of type dict. Must contain /Size (max # objs) and
    # /Root ref.
    self.trailer = None
    # PDF version string.
    # TODO(pts): Bump the version number to 1.2. if #AB hex escapes are used in names.
    self.version = '1.0'
    self.file_name = None
    self.file_size = None

  def load(self, file_data: str, is_no_objs_ok=False, is_parse_error_ok=True, is_proportional=False):
    """Load PDF from file_name to self, return self."""

    logger.log_info('loading PDF from: %s' % (file_data,), is_proportional)
    data: bytes = bytes()

    try:
      with open(file_data, 'rb') as f:
        data = f.read()
    except IOError as e:
      logger.log_fatal('error opening PDF (%s): %s' % (e, file_data))

    logger.log_info('loaded PDF of %s bytes' % len(data), is_proportional)
    self.has_generational_objs = False
    self.file_name = f.name
    self.file_size = len(data)
    # For some PDFs, there are some junk bytes in front of thje %PDF- header.
    # Just like Google Chrome, Evince and gv, We just ignore these junk bytes,
    # and we assume that offset 0 of the PDF file is where %PDF- starts.
    #
    # Example: https://github.com/pts/pdfsizeopt/issues/76
    match = PdfObj.PDF_VERSION_HEADER_RE.search(data[:256])
    if not match:
      raise PdfTokenParseError('unrecognized PDF signature %r' % data[: 16])
    data = data[match.start():]
    self.version = match.group(1)
    self.objs = objs = {}
    self.trailer = None

    try:
      try:
        obj_starts, self.has_generational_objs = self.parse_using_xref(data, do_ignore_generation_numbers=self.do_ignore_generation_numbers)
      except PdfXrefStreamError as exc:
        raise
      except PdfXrefError as exc:
        logger.log_warning('problem with xref table: %s' % exc)
        logger.log_warning('trying to load objs without the xref table')
        obj_starts, self.has_generational_objs = self.parse_without_xref(
            data,
            do_ignore_generation_numbers=self.do_ignore_generation_numbers)

      assert 'trailer' in obj_starts, 'no PDF trailer'
      assert len(obj_starts) > 1, 'no objects found in PDF (file corrupt?)'
      obj_count = len(obj_starts)
      obj_count_extra = ''
      if 'xref' in obj_starts:
        obj_count_extra += ' + xref'
        obj_count -= 1
      if 'trailer' in obj_starts:
        obj_count_extra += ' + trailer'
        obj_count -= 1
      logger.log_info('separated to %s objs%s' % (obj_count, obj_count_extra), is_proportional)
      last_ofs = trailer_ofs = obj_starts.pop('trailer')
      if isinstance(trailer_ofs, PdfObj):
        self.trailer = trailer_ofs
        last_ofs = len(data)
        obj_starts.pop('xref', None)
      else:
        self.trailer = PdfObj.parse_trailer(data, start=trailer_ofs)
        self.trailer.set(b'XRefStm', None)
        self.trailer.set(b'Prev', None)
        if 'xref' in obj_starts:
          last_ofs = min(trailer_ofs, obj_starts.pop('xref'))
      self.check_not_encrypted(trailer_obj=self.trailer)  # Also raised earlier.
    except PdfFileEncryptedError:
      # TODO(pts): Add decrypted input support.
      raise NotImplementedError(
          'encrypted PDF input not supported, use this command to '
          'decrypt first: qpdf --decrypt %s %s' %
          (shell_quote_file_name(self.file_name),
           shell_quote_file_name(os.path.splitext(self.file_name)[0] +
           '.decrypted.pdf')))

    if not (self.trailer.get(b'Root') or b'').endswith(b'R'):
      raise PdfMissingRootError('/Root reference not found in trailer.')

    obj_items = []
    for obj_num in obj_starts:
      obj_ofs = obj_starts[obj_num]
      if isinstance(obj_ofs, PdfObj):
        objs[obj_num] = obj_ofs  # Updates self.objs.
      else:
        obj_items.append((obj_ofs, obj_num))
    obj_items.sort()

    if last_ofs <= obj_items[-1][0]:
      last_ofs = len(data)
    obj_items.append((last_ofs, 'last'))

    # Remove objs with bad obj header from obj_items.
    # This fixes https://github.com/pts/pdfsizeopt/issues/30 .
    # For testing: irbookonlinereading.pdf
    _pdf_obj_def_re = PdfObj.PDF_OBJ_DEF_RE
    obj_items2 = []
    for i in range(1, len(obj_items)):
      start_ofs, obj_num = obj_items[i - 1]
      obj_data = data[start_ofs:obj_items[i][0]]  # obj_data = buffer(data, start_ofs, obj_items[i][0] - start_ofs)
      assert obj_data, 'duplicate object start offset'
      if _pdf_obj_def_re.match(obj_data):
        obj_items2.append((start_ofs, obj_num))
      else:
        logger.log_warning(
            'cannot parse obj %d: obj header (X Y obj) expected, '
            'got %r at ofs=%s' %
            (obj_num, obj_data[:32], start_ofs))
    obj_items2.append((last_ofs, 'last'))
    obj_items = obj_items2

    # Pairs mapping object numbers to strings of format ``X Y obj ...
    # endobj' (+ junk).
    objs_to_parse = sorted(  # Sorted by obj_num.
        (obj_items[i - 1][1], data[obj_items[i - 1][0]:obj_items[i][0]])  # buffer(data, obj_items[i - 1][0], obj_items[i][0] - obj_items[i - 1][0])
        for i in range(1, len(obj_items2)))

    objs_with_ilstream = []
    for is_ilstream_ok in (True, False):
      for obj_num, obj_data in objs_to_parse:
        try:
          self.objs[obj_num] = PdfObj(
              obj_data, objs=self.objs, file_ofs=obj_starts[obj_num],
              do_ignore_generation_numbers=self.do_ignore_generation_numbers,
              is_ilstream_ok=is_ilstream_ok)
        except PdfUnexpectedIlStreamError:  # Happens with is_ilstream_ok=True.
          # For testing: eurotex2006.final.pdf and lme_v6.pdf
          # Defer parsing this obj later, after we have the length objects
          # parsed.
          objs_with_ilstream.append((obj_num, obj_data))
        except PdfTokenParseError as e:
          if not is_parse_error_ok:
            raise
          # We just skip unparsable objects (so we don't add them to
          # obj_starts).
          logger.log_warning(
              'cannot parse obj %d: %s.%s: %s' %
              (obj_num, e.__class__.__module__, e.__class__.__name__, e))
      if not objs_with_ilstream:
        break
      objs_to_parse, objs_with_ilstream = objs_with_ilstream, None

    logger.log_info('parsed %d objs' % len(self.objs), is_proportional)
    if not objs and not is_no_objs_ok:
      # Happens e.g. when no objs can be parsed.
      raise PdfNoObjsError('No objs found in PDF.')

    return self

  @classmethod
  def check_not_encrypted(cls, trailer_obj):
    """Raises an exception if the PDF file is encrypted."""
    if trailer_obj.get(b'Encrypt') is not None:
      raise PdfFileEncryptedError

  @classmethod
  def yield_xref_stream_entries(cls, w0, w1, w2, index, xref_data):
    w01 = w0 + w1
    w012 = w01 + w2
    ii = 0
    obj_num = None
    ii_remaining = 0
    for i in range(0, len(xref_data), w012):
      if not ii_remaining:
        # PdfObj.GetAndClearXrefStream() guarantees that we get a positive
        # ii_remaining and we don't exhaust the index array below.
        if ii >= len(index):
          raise PdfXrefStreamError(
              'Index too large: ii=%d index_size=%d' % (ii, len(index)))
        if obj_num is not None and index[ii] <= obj_num:
          # TODO(pts): Check in xref_obj.GetAndClearXrefStream() instead.
          raise PdfXrefStreamError(
              'Sections within an xref stream not increasing: '
              'old_obj_num=%d new_obj_num=%d' %
              (obj_num, index[ii]))
        obj_num = index[ii]
        ii_remaining = index[ii + 1] - 1
        assert ii_remaining >= 0
        ii += 2
      else:
        obj_num += 1
        ii_remaining -= 1
      if w0:
        f0 = cls.msb_first_to_integer(xref_data[i: i + w0])
      else:
        f0 = 1
      f1 = cls.msb_first_to_integer(xref_data[i + w0: i + w01])
      if w2:
        f2 = cls.msb_first_to_integer(xref_data[i + w01: i + w012])
      else:
        f2 = 0
      if not f0:  # A free object, ignore it.
        continue
      yield obj_num, f0, f1, f2

  @classmethod
  def parse_using_xref_stream(cls, data, do_ignore_generation_numbers,
                              xref_ofs, xref_obj_num, xref_generation, obj_starts=None, do_allow_duplicate_obj=False):
    """Determine obj offsets in a PDF file using the cross-reference stream.

    Args:
      data: String containing the PDF file.
    Returns:
      (obj_starts, has_generational_objs)
      obj_starts is a dict mapping object numbers (and the string
      'trailer', possibly also 'xref') to pre-parsed PdfObj instances.
    Raises:
      PdfXrefStreamError: If the cross-reference stream is corrupt.
      PdfXrefError: If the cross-reference table is corrupt, but there is
        a chance to parse the file without it.
      AssertionError: If the PDF file us totally unparsable.
      NotImplementedError: If the PDF file needs parsing code not implemented.
      other: If the PDF file us totally unparsable.
    """
    has_generational_objs = False
    # Parse the cross-reference stream (xref stream).
    if obj_starts is None:
      # Maps object numbers to offset or (objstm_obj_num, index) values.
      obj_starts = {'xref': xref_ofs}  # 'xref' is just informational.
    # Maps /Type/ObjStm object numbers to compressed_obj_headbufs, or
    # None if that object stream is not loaded yet.
    obj_streams = {}
    trailer_obj = None
    xref_obj_nums = set()
    keep_obj_starts = set()
    # Contains (compressed_obj_num, stream_obj_num) pairs.
    compressed_objects_to_ignore = set()

    while 1:
      if xref_generation:
        if not do_ignore_generation_numbers:
          raise NotImplementedError(
              'generational objects (in xref %s %s) not supported at %d' %
              (xref_obj_num, xref_generation, xref_ofs))
        has_generational_objs = True
      if xref_obj_num in xref_obj_nums:
        raise PdfXrefStreamError('duplicate xref obj %d' % xref_obj_num)
      xref_obj_nums.add(xref_obj_num)
      try:
        xref_obj = PdfObj(data, start=xref_ofs, file_ofs=xref_ofs)
      except PdfTokenParseError as e:
        raise PdfXrefStreamError('parse xref obj %d: %s' % (xref_obj_num, e))
      cls.check_not_encrypted(trailer_obj=xref_obj)

      # Parse the xref stream data.
      #
      # TODO(pts): Handle the various exceptions raised by
      #            xref_obj.GetUncompressedStream().
      w0, w1, w2, index, xref_data = xref_obj.get_and_clear_xref_stream(
          xref_ofs=xref_ofs, xref_obj_num=xref_obj_num)
      for obj_num, f0, f1, f2 in cls.yield_xref_stream_entries(w0, w1, w2, index, xref_data):
        if obj_num in obj_starts:
          if obj_num in keep_obj_starts:
            if f0 == 2:
              compressed_objects_to_ignore.add((obj_num, f1))
            continue  # Ignore this entry, obj defined in higher xref stream.
          if not do_allow_duplicate_obj:
            raise PdfXrefStreamError('duplicate obj %d' % obj_num)
        if f0 == 1:  # f1 is the object offset in the file.
          if f2:
            if not do_ignore_generation_numbers:
              raise NotImplementedError(
                  'generational objects (in %s %s) not supported at %d' %
                  (obj_num, f2, xref_ofs))
            has_generational_objs = True

          if f1 < 9:
            # Accept (and ignore) a 0 offset, Multivalent generates such files:
            # `/W [0 x 0]', and some offsets are 0.
            if f1:
              raise PdfXrefStreamError('offset of obj %d too small: %d' % (obj_num, f1))
          else:
            obj_starts[obj_num] = f1
        elif f0 == 2:
          # f1: Object number of the object stream of obj_num.
          # f2: Index of obj_num within its object stream.
          obj_starts[obj_num] = (f1, f2)
          obj_streams.setdefault(f1, None)
      if trailer_obj is None:
        trailer_obj = xref_obj  # Takes ownership.
      else:
        dict_obj = xref_obj._cache
        assert dict_obj is not None, '/Type/ObjStm obj not parsed yet.'
        # The code below merges entries from xref_obj (the current /Prev
        # xref stream trailer) to trailer_obj (the final PDF trailer,
        # initially the trailer of the main xref obj).
        #
        # As documented by section 3.4.5 Incremental updates in
        # pdf_reference_1-7.pdf, we don't have to merge anything, and the
        # main trailer has to be used. This behavior is consistent with
        # self.ParseUsingXref.

      prev = xref_obj.get(b'Prev')
      if prev is None:
        break
      trailer_obj.set(b'Prev', None)
      # TODO(pts): For testing: issue58.pdf.
      if not isinstance(prev, int) or prev < 9:
        raise PdfXrefStreamError('invalid /Prev at %d: %r' % (xref_ofs, prev))
      match = PdfObj.PDF_OBJ_DEF_RE.match(data, prev)
      if not match:
        raise PdfXrefStreamError('could not find obj at /Prev at %d: %d' %
                                 (xref_ofs, prev))
      xref_ofs = prev
      xref_obj_num = int(match.group(1))
      xref_generation = int(match.group(2))
      # Subsequent /Prev xref objects are not allowed to modify objects
      # we've already created. For an example, see
      # https://code.google.com/p/pdfsizeopt/issues/detail?id=71
      keep_obj_starts.update(obj_starts)

    del xref_obj  # Save complexity (and a little bit of memory).
    assert trailer_obj
    logger.log_proportional_info(
        'found %d obj offsets and %d obj streams in xref stream' %
        (len(obj_starts) - ('xref' in obj_starts) - ('trailer' in obj_starts),
         len(obj_streams)))
    max_obj_num = None
    for xref_obj_num in sorted(xref_obj_nums):
      obj_start = obj_starts.get(xref_obj_num)
      if obj_start is None:
        if max_obj_num is None:
          max_obj_num = max(
              (obj_num != 'xref' and obj_num != 'trailer' and obj_num or 0) for obj_num in obj_starts)
        if xref_obj_num != max_obj_num + 1:
          # pgfmanual.pdf in
          # https://code.google.com/p/pdfsizeopt/issues/detail?id=75
          logger.log_warning(
              'missing offset for xref stream obj %d' % xref_obj_num)
      else:
        if not isinstance(obj_start, int):
          logger.log_warning(
              'in-object-stream xref stream obj %d' % xref_obj_num)
        del obj_starts[xref_obj_num]

    # Parse the object streams.
    compressed_obj_errors = {}
    for objstm_obj_num in sorted(obj_streams):
      obj_start = obj_starts.get(objstm_obj_num)
      if obj_start is None:
        raise PdfXrefStreamError('Missing xref obj stream %d' % objstm_obj_num)
      if not isinstance(obj_start, int):
        raise PdfXrefStreamError('In-object-stream obj stream %d' %
                                 objstm_obj_num)
      try:
        objstm_obj = PdfObj(data, start=obj_start, file_ofs=obj_start)
      except PdfIndirectLengthError as e:
        # Example: objstm_obj_num == 16 in functional-programming-python.pdf
        if e.length_obj_num not in obj_starts:
          raise PdfXrefStreamError('Parse objstm obj %d: %s' %
                                   (objstm_obj_num, e))
        length_obj_start = obj_starts[e.length_obj_num]
        length_obj = PdfObj(
            data, start=length_obj_start, file_ofs=length_obj_start)
        try:
          objstm_obj = PdfObj(data, start=obj_start, file_ofs=obj_start,
                              objs={e.length_obj_num: length_obj})
        except PdfTokenParseError as e:
          raise PdfXrefStreamError('Parse objstm obj %d: %s' %
                                   (objstm_obj_num, e))
      except PdfTokenParseError as e:
        raise PdfXrefStreamError('Parse objstm obj %d: %s' %
                                 (objstm_obj_num, e))
      obj_streams[objstm_obj_num] = objstm_obj.parse_obj_stm(objstm_obj_num)

    # Parse used compressed objs (in objstm objs), and add them to
    # obj_starts with the PdfObj (instead of the offset) as a value.
    for obj_num in sorted(obj_starts):
      obj_start = obj_starts.get(obj_num)
      if not isinstance(obj_start, int):
        objstm_obj_num, i = obj_start
        # Already populated in the loop above.
        compressed_obj_nums, compressed_obj_headbufs = obj_streams[objstm_obj_num]
        if i >= len(compressed_obj_headbufs):
          raise PdfXrefStreamError(
              'Too few compressed objs (%d) in objstm obj %d, '
              'needed index %d for obj %d.' %
              (len(compressed_obj_headbufs), objstm_obj_num, i, obj_num))
        if compressed_obj_nums[i] != obj_num:
          raise PdfXrefStreamError(
              'Mismatch in obj_num: obj_num_in_xref_stream=%d '
              'obj_num_in_objstm=%d objstm_obj_num=%d i=%d' %
              (obj_num, compressed_obj_nums[i], objstm_obj_num, i))
        compressed_obj_nums[i] = None
        assert isinstance(compressed_obj_headbufs[i], (buffer, str))
        obj_starts[obj_num] = compressed_obj_headbufs[i] = PdfObj('%d 0 obj\n%s\nendobj\n' % (obj_num, compressed_obj_headbufs[i]))
    for obj_num in sorted(obj_streams):
      del obj_starts[obj_num]
    obj_starts['trailer'] = trailer_obj

    # Report number of unused compressed objs.
    all_unused_obj_count = 0
    unused_obj_items = []
    for objstm_obj_num in sorted(obj_streams):
      unused_obj_count = sum(1 for j in obj_streams[objstm_obj_num][0] if j is not None)
      if unused_obj_count:
        unused_obj_items.append((objstm_obj_num, unused_obj_count))
        all_unused_obj_count += unused_obj_count
    if all_unused_obj_count:
      logger.log_proportional_info(
          'ignoring %d unused compressed objs: %s' %
          (all_unused_obj_count,
           ', '.join('%d in objstm obj %d' % (b, a)
                     for a, b in unused_obj_items)))

    return obj_starts, has_generational_objs

  @classmethod
  def parse_using_xref(cls, data, do_ignore_generation_numbers):
    """Determine obj offsets in a  PDF file using the cross-reference table.

    If this method detects a cross-reference stream, it calls
    cls.ParseUsingXrefStream instead.

    Args:
      data: String containing the PDF file.
    Returns:
      (obj_starts, has_generational_objs)
      obj_starts is a dict mapping object numbers (and the string
      'trailer', possibly also 'xref') to their start offsets (or to
      pre-parsed PdfObj instances) within a file.
    Raises:
      PdfXrefStreamError: If the cross-reference stream is corrupt.
      PdfXrefError: If the cross-reference table is corrupt, but there is
        a chance to parse the file without it.
      AssertionError: If the PDF file us totally unparsable.
      NotImplementedError: If the PDF file needs parsing code not implemented.
      other: If the PDF file us totally unparsable. Example: zlib.error.
    """
    match = None
    # Some PDFs have a few bytes of garbage at the end, so we don't anchor
    # this regexp to the end of the string, but we try to find the last match.
    #
    # Example: server-based-java-programming.pdf in
    # https://github.com/pts/pdfsizeopt/issues/80
    # Example: https://github.com/pts/pdfsizeopt/issues/86
    for match in PdfObj.PDF_STARTXREF_EOF_RE.finditer(data[-400:]):
      pass  # Find the last math.
    if match is None:
      raise PdfXrefError('startxref+%%EOF not found')
    xref_ofs = int(match.group(1))
    match = PdfObj.PDF_OBJ_DEF_RE.match(data, xref_ofs)
    if match:
      xref_obj_num = int(match.group(1))
      xref_generation = int(match.group(2))
      return cls.parse_using_xref_stream(data, do_ignore_generation_numbers, xref_ofs, xref_obj_num, xref_generation)

    has_generational_objs = False
    obj_starts = {'xref': xref_ofs}  # 'xref' is just informational.
    obj_starts_rev = {}
    # Set of object numbers not to be overwritten.
    keep_obj_nums = set()
    xrefstm_objs = []
    _xref_re = PdfObj.PDF_XREF_SUBSECTION_OR_TRAILER_RE
    _xref_section_re = PdfObj.PDF_XREF_SECTION_RE
    _xref_entry_re = PdfObj.PDF_XREF_ENTRY_RE
    while 1:
      # Maybe PDF doesn't allow multiple consecutive `xref' sections,
      # but we accept that.
      match = _xref_section_re.match(data, xref_ofs)
      if not match:
        raise PdfXrefError('xref table not found at %s' % xref_ofs)
      xref_ofs = match.end(1)
      while 1:
        # Start a new subsection.
        # For testing whitespace before trailer: enc.pdf
        # obj_count == 0 is fine, see
        # http://code.google.com/p/pdfsizeopt/issues/detail?id=25
        match = _xref_re.match(data, xref_ofs)
        if not match:
          raise PdfXrefError('xref subsection syntax error at %d' % xref_ofs)
        if match.group(3) is not None:  # 'xref' or 'trailer'.
          xref_ofs = match.start(3)
          break
        obj_num = int(match.group(1))
        obj_count = int(match.group(2))
        xref_ofs = match.end()
        while obj_count > 0:
          match = _xref_entry_re.match(data, xref_ofs)
          if not match:
            raise PdfXrefError('syntax error in xref entry at %s' % xref_ofs)
          if match.group(3) == b'n':
            generation = int(match.group(2))
            if generation != 0:
              if not do_ignore_generation_numbers:
                raise NotImplementedError(
                    'generational objects (in %s %s n) not supported at %d' %
                    (match.group(1), match.group(2), xref_ofs))
              has_generational_objs = True
            obj_ofs = int(match.group(1))
            if obj_num in obj_starts:
              if obj_num in keep_obj_nums:
                # for testing: obj 5 in bfilter.pdf
                # It is not tested if this assignment is replaced by `pass',
                # to make /Prev override.
                obj_ofs = 0
              else:
                raise PdfXrefError('duplicate obj %s' % obj_num)
            if obj_ofs != 0:
              # for testing: obj 10 in pdfsizeopt_charts.pdf has offset 0:
              # "0000000000 00000 n \n"
              if obj_ofs in obj_starts_rev:
                raise PdfXrefError(
                    'duplicate use of obj offset %s: %s and %s' %
                    (obj_ofs, obj_starts_rev[obj_ofs], obj_num))
              obj_starts_rev[obj_ofs] = obj_num
              # TODO(pts): Check that we match PdfObj.OBJ_DEF_RE at obj_ofs.
              obj_starts[obj_num] = obj_ofs
          # TODO(pts): Process deleted objects since /Prev.
          obj_num += 1
          obj_count -= 1
          xref_ofs += 20
      if match.group(2) == b'xref':
        # TODO(pts): Test this.
        raise NotImplementedError('multiple xref sections (with generation numbers) not implemented')
      # Keep only the very first trailer.
      obj_starts.setdefault('trailer', xref_ofs)

      # TODO(pts): How to test this?
      try:
        trailer_obj = PdfObj.parse_trailer(data, start=xref_ofs)
      except PdfTokenParseError as exc:
        raise PdfXrefError(str(exc))
      xrefstm_ofs = trailer_obj.get(b'XRefStm')
      if xrefstm_ofs is not None:  # Hybrid.
        if not isinstance(xrefstm_ofs, int):
          raise PdfXrefError('/XRefStm offset not an int: %r' % (xrefstm_ofs,))
        match = PdfObj.PDF_OBJ_DEF_RE.match(data, xrefstm_ofs)
        if not match:
          raise PdfXrefStreamError('/XRefStm obj definition expected at %d' % xrefstm_ofs)
        xrefstm_obj_num = int(match.group(1))
        xrefstm_obj_generation = int(match.group(2))
        xrefstm_objs.append((xrefstm_ofs, xrefstm_obj_num, xrefstm_obj_generation))
      xref_ofs = trailer_obj.get(b'Prev')
      del trailer_obj  # Save memory.
      if xref_ofs is None:
        break
      if not isinstance(xref_ofs, int):
        raise PdfXrefError('/Prev xref offset not an int: %r' % (xref_ofs,))
      # Subsequent /Prev xref tables are not allowed to modify objects
      # we've already created.
      # for testing: obj 5 in bfilter.pdf
      keep_obj_nums.update(obj_starts)
    if xrefstm_objs:
      obj_start_nums = set(obj_starts)
      obj_start_nums.add('trailer')
      obj_start_nums.add('xref')
      for xrefstm_ofs, xrefstm_obj_num, xrefstm_obj_generation in xrefstm_objs:
        obj_starts_copy = dict(obj_starts)
        # Updates obj_starts, changes obj_starts['trailer'] to a PdfObj.
        cls.parse_using_xref_stream(data, do_ignore_generation_numbers, xrefstm_ofs, xrefstm_obj_num, xrefstm_obj_generation, obj_starts_copy, do_allow_duplicate_obj=True)
        for obj_num, obj_ofs in obj_starts_copy.items():
          if obj_num not in obj_start_nums:
            obj_starts[obj_num] = obj_ofs
    return obj_starts, has_generational_objs

  @classmethod
  def parse_without_xref(cls, data, do_ignore_generation_numbers=False):
    """Parse a PDF file without having a look at the cross-reference table.

    This method doesn't consult the cross-reference table (`xref'): it just
    searches for objects looking at '\nX Y obj' strings in `data'.

    This method may find a false match, such as `( 1 0 obj )'.
    TODO(pts): Get rid of false matches. The problem is that we might have
    /Length of a stream as an indirect forward reference (usually if it's
    indirect, then it's forward), so we cannot be fully reliable this way.

    Args:
      data: String containing the PDF file.
    Returns:
      (obj_starts, has_generational_objs)
      objs_starts is a dict mapping object numbers (and the string
      'trailer', possibly also 'xref') to their start offsets within a file.
    """
    # None, an int or 'trailer'.
    prev_obj_num = None
    obj_starts = {}
    has_generational_objs = False

    for match in PdfObj.PDF_OBJ_OR_TRAILER_RE.finditer(data):
      if match.group(1) is not None:
        prev_obj_num = int(match.group(1))
        generation = int(match.group(2))
        if generation != 0:
          if not do_ignore_generation_numbers:
            raise NotImplementedError(
                'generational objects (in %s %s n) not supported at %d' %
                (match.group(1), match.group(2), match.start() + 1))
          has_generational_objs = True
        assert prev_obj_num not in obj_starts, (
            'duplicate obj %d' % prev_obj_num)
        # Skip over '\n'
        obj_starts[prev_obj_num] = match.start() + 1
      else:
        prev_obj_num = 'trailer'
        # Allow multiple trailers. Keep the last one. This heuristic works
        # for http://code.google.com/p/pdfsizeopt/issues/detail?id=25 .
        # TODO(pts): Test multiple trailers with: pdf.a9p4/5176.CFF.a9p4.pdf
        # Skip over '\n'.
        obj_starts[prev_obj_num] = match.start() + 1

    # TODO(pts): Learn to parse no trailer in PDF-1.5
    # (e.g. pdf_reference_1-7-o.pdf)
    assert prev_obj_num == 'trailer', prev_obj_num
    return obj_starts, has_generational_objs

  @classmethod
  def generate_xref_stream(cls, obj_numbers, obj_ofs, xref_ofs, trailer_obj,
                           trailer_obj_num, objstm_obj_num, objstm_obj_numbers,
                           is_flate_ok=True):
    """Generate the xref stream for the specified trailer object.

    Add the appropriate, size-optimized trailer_obj.stream, add the
    following names: /Size, /Type, /W, add or remove some names: /Index.

    Please note that xref streams were introduced in PDF 1.5.

    Args:
      obj_numbers: Sorted list of object numbers the PDF contains. Generation
        numbers are all 0. Won't be modified. Must not contain the object
        number of the trailer.
      obj_ofs: Dict mapping object numbers to object file offsets. Won't be
        modified. Must not contain the offset of the trailer (xref_ofs).
      xref_ofs: File offset of the xref stream (trailer_obj)
      trailer_obj: PdfObj whose .head contains the PDF trailer. Will be
        modified in place: .stream and some names (including /Size, /Type, /W
        and /Length) added, some other names (including /Index, /Filter and
        /DecodeParms) added or cleared.
      trailer_obj_num: Obect number of trailer_obj, must not be present in
        obj_numbers.
      objstm_obj_num: Object number of the /Type/ObjStm obj, or None. Must not
        be present in obj_numbers.
      objstm_obj_numbers: Sequence of object numbers within the /Type/ObjStm
        obj, or None.
      is_flate_ok: bool indicating if it's OK to generate xref and object
        streams with /Filter/FlateDecode.
    """
    assert obj_numbers or objstm_obj_numbers
    assert trailer_obj.head.startswith(b'<<')
    assert trailer_obj.stream is None
    assert not [obj_num for obj_num in obj_numbers if obj_ofs[obj_num] <= 0]
    assert xref_ofs not in obj_ofs
    assert trailer_obj_num not in obj_numbers  # Slow.
    if objstm_obj_numbers:
      assert trailer_obj_num not in objstm_obj_numbers  # Slow.
    need_w0 = False  # Do we need w0 be 1 instead of 0?
    max_w2 = -1
    max_obj_num = (obj_numbers and obj_numbers[-1]) or 0
    if objstm_obj_numbers:
      assert objstm_obj_num
      need_w0 = True
      obj_numbers = set(obj_numbers)
      assert objstm_obj_num not in obj_numbers
      obj_numbers_size = len(obj_numbers)
      obj_numbers.update(objstm_obj_numbers)
      obj_numbers.add(objstm_obj_num)
      assert (len(obj_numbers) ==
              obj_numbers_size + len(objstm_obj_numbers) + 1), (
          '/Type/ObjStm and non-objstm object numbers must be disjoint.')
      obj_numbers = sorted(obj_numbers)
      max_w2 = max(max_w2, len(objstm_obj_numbers) - 1)
      objstm_obj_numbers_rev = {}
      for i, obj_num in enumerate(objstm_obj_numbers):
        objstm_obj_numbers_rev[obj_num] = -i  # Can be 0.
      ofs_list = []
      for obj_num in obj_numbers:
        if obj_num in objstm_obj_numbers_rev:
          ofs_list.append(objstm_obj_numbers_rev[obj_num])  # Negative or 0.
        else:
          ofs_list.append(obj_ofs[obj_num])  # Positive.
      del objstm_obj_numbers_rev  # Save memory.
    else:
      ofs_list = [obj_ofs[obj_num] for obj_num in obj_numbers]

    # TODO(pts): Do we need to have the offset /Type/XRef trailer_obj in the
    # xref stream? The xref stream would be shorter without this.
    ofs_list.append(xref_ofs)
    max_obj_num = max(max_obj_num, trailer_obj_num)

    trailer_obj.set(b'Size', max_obj_num + 1)
    trailer_obj.set(b'Type', b'/XRef')
    max_ofs = xref_ofs
    if obj_numbers[0] != 0:  # /Index [0 Size] is the default.
      # Usually obj_numbers[0] == 1, and we'll take care of emitting a
      # free_entry later for that, and removing /Index (so it can be the
      # default).
      index_data = b'[%d %d]' % (obj_numbers[0], max_obj_num - obj_numbers[0] + 1)
      trailer_obj.set(b'Index', index_data)
      index_size = len(index_data) + 7  # 7 == len('/Index ').
      del index_data
    else:
      trailer_obj.set(b'Index', None)
      index_size = 0

    for i in range(1, len(obj_numbers)):
      if obj_numbers[i] - 1 != obj_numbers[i - 1]:
        if not (obj_numbers[i] - 2 == obj_numbers[i - 1] and
                obj_numbers[i] - 1 == trailer_obj_num):
          need_w0 = True
      max_ofs = max(max_ofs, ofs_list[i])  # Negative entries are ignored.
    if objstm_obj_numbers:
      max_ofs = max(max_ofs, objstm_obj_num)
    if trailer_obj_num != obj_numbers[-1] + 1:
      need_w0 = True

    max_ofs_size = 1
    while max_ofs >= 1 << (8 * max_ofs_size):
      max_ofs_size += 1
    if max_ofs_size > 8:
      raise NotImplementedError('unsupported max_ofs_size=%d for max_ofs=%d' % (max_ofs_size, max_ofs))
    if need_w0:
      # TODO(pts): Consider encoding free objects as multiple ranges in
      # /Index instead of the direct encoding below. Maybe the result
      # will be smaller.
      if max_w2 >= 0:
        max_w2_size = 1
        while max_w2 >= 1 << (8 * max_w2_size):
          max_w2_size += 1
      else:
        max_w2_size = 0
      w2_zero_str = b'\0' * max_w2_size
      ofs_output = []
      trailer_obj.set(b'W', b'[1 %d %d]' % (max_ofs_size, max_w2_size))
      free_entry = b'\x00' * (1 + max_ofs_size + max_w2_size)
      done_obj_num = 0
      if index_size:
        assert obj_numbers[0] != 0
        if obj_numbers[0] <= (index_size - 1) / max_ofs_size:
          # For testing: --use-multivalent=yes --do-generate-xref-stream=yes
          # --do-generate-object-stream=yes /mnt/mandel/warez/tmp/issue57.pdf
          done_obj_num = obj_numbers[0]
          ofs_output.append(free_entry * done_obj_num)
          trailer_obj.set(b'Index', None)
      i = 0
      for ofs in ofs_list:
        if i < len(obj_numbers):
          obj_num_limit = obj_numbers[i]
        else:
          assert i == len(obj_numbers)
          obj_num_limit = trailer_obj_num
        while done_obj_num < obj_num_limit:
          ofs_output.append(free_entry)
          done_obj_num += 1
        i += 1
        if ofs <= 0:  # An object from the /Type/ObjStm obj.
          ofs_output.append(b'\x02')
          if max_ofs_size <= 4:
            ofs_output.append(struct.pack('>L', objstm_obj_num)[4 - max_ofs_size:])
          else:
            ofs_output.append(struct.pack('>Q', objstm_obj_num)[8 - max_ofs_size:])
          if max_w2_size <= 4:
            ofs_output.append(struct.pack('>L', -ofs)[4 - max_w2_size:])
          else:
            ofs_output.append(struct.pack('>Q', -ofs)[8 - max_w2_size:])
        else:
          ofs_output.append(b'\x01')
          if max_ofs_size <= 4:
            ofs_output.append(struct.pack('>L', ofs)[4 - max_ofs_size:])
          else:
            ofs_output.append(struct.pack('>Q', ofs)[8 - max_ofs_size:])
          if max_w2_size:
            ofs_output.append(w2_zero_str)
        done_obj_num += 1
      data = b''.join(ofs_output)
      del ofs_output
      extra_width = 1 + max_w2_size
    else:
      data = ''
      if index_size:
        assert obj_numbers[0] != 0
        if obj_numbers[0] <= (index_size - 1) / max_ofs_size:
          # Save a few bytes by removing /Index and adding zeros to the
          # beginning of the stream.
          #
          # For testing: --use-multivalent=no --do-generate-xref-stream=yes
          # --do-generate-object-stream=no issue57.pdf
          data = '\0' * (obj_numbers[0] * max_ofs_size)
          trailer_obj.set(b'Index', None)
      assert max_w2 == -1
      trailer_obj.set(b'W', '[0 %d 0]' % max_ofs_size)
      if max_ofs_size == 1:
        data += struct.pack('>%dB' % len(ofs_list), *ofs_list)
      elif max_ofs_size == 2:
        data += struct.pack('>%dH' % len(ofs_list), *ofs_list)
      elif max_ofs_size == 3:
        data += ''.join(struct.pack('>L', ofs)[1:] for ofs in ofs_list)
      elif max_ofs_size == 4:
        data += struct.pack('>%dL' % len(ofs_list), *ofs_list)
      else:
        i = 8 - max_ofs_size
        data += ''.join(struct.pack('>Q', ofs)[i:] for ofs in ofs_list)
      extra_width = 0
      #assert False, (len(data), max_ofs_size, extra_width)
    trailer_obj.set_stream_and_compress(data, predictor_width=(max_ofs_size + extra_width), is_flate_ok=is_flate_ok)

  def _assert_before_write(self):
    """Do some assertions before saving or serializing the PDF."""
    assert self.objs
    assert self.trailer.head.startswith(b'<<')
    assert self.trailer.head.endswith(b'>>')

  def append_serialized_pdf(self, output,
                            do_hide_images=False,
                            do_generate_xref_stream=True,
                            do_generate_object_stream=True,
                            do_emit_short_unsafe=True,
                            is_flate_ok=True):
    """Appends a serialized PDF file to the list output.

    Args:
      output: A list of strings, will be appended in place. Must be empty
        in the beginning.
      do_hide_images: bool indicating whether to hide images from Multivalent.
      do_generate_xref_stream: bool indicating if we should generate a PDF
        containing a cross-reference stream.
      do_generate_object_stream: bool indicating if we should generate a PDF
        containing all non-stream objects packed to an object stream (objstm).
      do_emit_short_unsafe: bool indicating whether to emit unsafe PDF token
        squences. If true, pdfsizeopt wouldn't be able to process these strings
        further without additional parsing by PdfObj.ParseTokensToSafe. So true
        is OK for output .pdf files.
      is_flate_ok: bool indicating if it's OK to generate xref and object
        streams with /Filter/FlateDecode.
    Returns:
      The number of bytes appended.
    """
    if not isinstance(output, list):
      raise TypeError
    assert not output
    # Emit header.
    if do_generate_xref_stream:
      version = max(self.version, b'1.5')
    else:
      version = self.version
    output.extend((b'%PDF-', version, b'\n%\xD0\xD4\xC5\xD0\n'))

    output_size = [0]
    output_size_idx = [0]

    def get_output_size():
      if output_size_idx[0] < len(output):
        for i in range(output_size_idx[0], len(output)):
          output_size[0] += len(output[i])
        output_size_idx[0] = len(output)
      return output_size[0]

    # Dictionary mapping object numbers to 0-based offsets in the output file.
    obj_ofs = {}

    obj_count = len(self.objs)
    obj_numbers = sorted(self.objs)
    assert obj_count == len(obj_numbers), 'Duplicate object number found.'
    if obj_numbers:
      next_obj_num = obj_numbers[-1] + 1
    else:
      next_obj_num = 0
    objstm_obj = None
    objstm_obj_numbers = None

    if do_generate_object_stream:
      objstm_output = [b'', b'>']  # Sentinel for IsSpaceNeeded below.
      objstm_size = 0  # In bytes.
      objstm_numbers = []
      objstm_objcount = 0
      objstm_overhead_size = 0
      objstm_obj_numbers = []
      # TODO(pts): Reorder heads (lexicographically? by size? by
      # subtype--type? try all?) to achieve better ZIP compression.
      for obj_num in obj_numbers:
        # According the the PDF reference, object streams must not contain
        # objects: which have a stream; which have a non-zero generation
        # number (this can't happen here); which are the document's
        # encryption dictionary (this can't happen here, we don't generate
        # an encryption dictionary at all); which are an integer
        # representing a /Length value of a stream object (this can't
        # happen here, all stream objects got their /Length inlined when
        # the PdfObj is created). So we exclude those objects below.
        #
        # Strings in objects in an object stream must not be encrypted. We
        # ensure this, because we don't encrypt at all.
        pdf_obj = self.objs[obj_num]
        if pdf_obj.stream is None:
          # TODO(pts): Renumber objstm objects, group them together, so the
          # xref stream can be compressed to become shorter.
          head = pdf_obj.head
          # The PDF reference says that objects who are just `X Y R' must
          # not be part of an object stream. So we skip them here.
          if not (head.endswith(b'R') and PdfObj.PDF_REF_END_RE.search(head)):
            if do_emit_short_unsafe:
              # To call this in the final objstm_output instead, we'd have to
              # fix the offsets (objstm_numbers). See also
              # https://github.com/pts/pdfsizeopt/issues/69 .
              head = PdfObj.compress_value(head, do_emit_safe_names=False, do_emit_safe_strings=False)
            if PdfObj.is_space_needed(objstm_output[-1], head[0]):
              objstm_output.append(b' ')
              objstm_size += 1
            objstm_numbers.append(obj_num)
            # If we append the wrong offset here, Ghostscript can still process
            # the output PDF, because it ignores this offset.
            objstm_numbers.append(objstm_size)
            objstm_output.append(head)
            objstm_size += len(head)
            objstm_objcount += 1
            # This 19 includes 4 bytes in the xref stream and
            # ' 0 obj  endobj\n'.
            # TODO(pts): Improve the estimate of 4 bytes in the xref stream:
            # take compression, len(w0) and len(w1) into account.
            objstm_overhead_size += 19 + len(str(obj_num)) + len(head)
            objstm_obj_numbers.append(obj_num)
      del head  # Save memory.
      if objstm_size:
        # TODO(pts): If the generated object stream is longer than the
        # sum of the individual objects, don't use it.
        # Replace the simulated digit.
        objstm_output[0] = b' '.join(b'%d' % i for i in objstm_numbers)
        objstm_output[1] = b' ' * (
            len(objstm_output) > 2 and
            PdfObj.is_space_needed(objstm_output[0], objstm_output[2]))
        objstm_first = len(objstm_output[0]) + len(objstm_output[1])
        objstm_output = b''.join(objstm_output)
        objstm_obj = PdfObj(None)
        objstm_obj.head = b'<<>>'
        # For the statistics below.
        objstm_size = len(objstm_output) + objstm_overhead_size
        objstm_obj.set_stream_and_compress(objstm_output, is_flate_ok=is_flate_ok)
        del objstm_output  # Save memory.
        objstm_obj.set(b'Type', b'/ObjStm')
        objstm_obj.set(b'N', objstm_objcount)
        objstm_obj.set(b'First', objstm_first)
        logger.log_info(
            'generated object stream of %d bytes in %d objects (%s)' %
            (len(objstm_obj.stream), objstm_objcount,
             format_percent(len(objstm_obj.stream), objstm_size)))
        i = j = 0
        objstm_obj_numbers_set = set(objstm_obj_numbers)
        while i < len(obj_numbers):
          if obj_numbers[i] in objstm_obj_numbers_set:
            i += 1  # Remove this object from obj_numbers.
          else:
            obj_numbers[j] = obj_numbers[i]
            i += 1
            j += 1
        del obj_numbers[j:]
        del objstm_obj_numbers_set  # Save memory.

    # Emit objs outside the object stream.
    # Number of objects including missing ones.
    for obj_num in obj_numbers:
      obj_ofs[obj_num] = get_output_size()
      pdf_obj = self.objs[obj_num]
      if do_hide_images and pdf_obj.has_image_to_hide():
        pdf_obj = PdfObj(pdf_obj)
        # We convert /Subtype/Image to /Subtype/ImagE and /Filter
        # to /FilteR and /DecodeParms to /DecodeParmS. This way we force
        # Multivalent not to treat the obj as image, i.e. not to recompress
        # it (suboptimally).
        assert pdf_obj.get(b'FilteR') is None
        assert pdf_obj.get(b'DecodeParmS') is None
        pdf_obj.set(b'Subtype', b'/ImagE')
        filter_value = pdf_obj.get(b'Filter')
        if filter_value is not None:
          pdf_obj.set(b'FilteR', filter_value)
          pdf_obj.set(b'Filter', None)
        decodeparms = pdf_obj.get(b'DecodeParms')
        if decodeparms is not None:
          pdf_obj.set(b'DecodeParmS', decodeparms)
          pdf_obj.set(b'DecodeParms', None)

        # Trick to force Multivalent not to uncompress + compress the
        # object.
        # For testing: multivalent_filter_test.pdf
        pdf_obj.set(b'Filter', b'/JPXDecode')
      pdf_obj.append_to(output, obj_num, do_emit_short_unsafe=do_emit_short_unsafe)

    if objstm_obj:
      objstm_obj_num = next_obj_num  # The largest.
      next_obj_num += 1
      obj_ofs[objstm_obj_num] = get_output_size()
      objstm_obj.append_to(output, objstm_obj_num, do_emit_short_unsafe=do_emit_short_unsafe)
    else:
      objstm_obj_num = None
    del objstm_obj  # Save memory.

    trailer_obj_num = next_obj_num
    next_obj_num += 1
    trailer_obj = PdfObj(self.trailer)
    trailer_obj.set(b'Prev', None)
    trailer_obj.set(b'XRefStm', None)
    trailer_obj.set(b'Compress', None)  # emitted by Multivalent.jar
    # Emitted by Multivalent.jar etc., see section 10.3 in
    # pdf_reference_1-7.pdf .
    trailer_obj.set(b'ID', None)
    assert trailer_obj.head.startswith(b'<<')
    assert trailer_obj.head.endswith(b'>>')
    assert trailer_obj.stream is None

    xref_ofs = get_output_size()
    if do_generate_xref_stream:  # Emit xref stream containing trailer.
      # Also modifies trailer_obj, setting .stream, /Size etc.
      self.generate_xref_stream(obj_numbers=obj_numbers, obj_ofs=obj_ofs,
                                xref_ofs=xref_ofs, trailer_obj=trailer_obj,
                                trailer_obj_num=trailer_obj_num,
                                objstm_obj_num=objstm_obj_num,
                                objstm_obj_numbers=objstm_obj_numbers,
                                is_flate_ok=is_flate_ok)
      trailer_obj.append_to(output, trailer_obj_num, do_emit_short_unsafe=do_emit_short_unsafe)
    else:  # Emit xref and trailer.
      trailer_obj.set(b'Size', obj_numbers[-1] + 1)  # max_obj_num + 1.
      if obj_numbers[0] == 1:
        i = 0
        j = i + 1
        while j < obj_count and obj_numbers[j] - 1 == obj_numbers[j - 1]:
          j += 1
        output.append('xref\n0 %s\n0000000000 65535 f \n' % (j + 1))
        while i < j:
          output.append('%010d 00000 n \n' % obj_ofs[obj_numbers[i]])
          i += 1
      else:
        output.append('xref\n0 1\n0000000000 65535 f \n')
        i = 0

      # Add subsequent xref subsections.
      while i < obj_count:
        j = i + 1
        while j < obj_count and obj_numbers[j] - 1 == obj_numbers[j - 1]:
          j += 1
        output.append('%s %s\n' % (obj_numbers[i], j - i))
        while i < j:
          output.append('%010d 00000 n \n' % obj_ofs[obj_numbers[i]])
          i += 1

      output.append('trailer\n%s\n' % trailer_obj.head)

    output.append(b'startxref\n%d\n' % xref_ofs)
    output.append(b'%%EOF\n')  # Avoid doubling % in printf().
    return get_output_size()

  def add_obj(self, obj):
    """Add PdfObj to self.objs, return its object number."""
    if not isinstance(obj, PdfObj):
      raise TypeError
    obj_num = len(self.objs) + 1
    if obj_num in self.objs:
      obj_num = max(self.objs) + 1
    self.objs[obj_num] = obj
    return obj_num

  # !!! Do proper PDF token sequence parsing (ParseTokensToSafe).
  PDFDATA_INDEXED_COLORSPACE_FOR_SUB_RE = re.compile(
      r'\A\[[\x00\t\n\r\f ]*/Indexed[\x00\t\n\r\f ]*'
      r'/([^\x00\t\n\r\f /<(]+)(.|\n)*')

  @classmethod
  def _IsSlowCmdName(cls, cmd_name):
    return ('pngout' in cmd_name or 'zopflipng' in cmd_name or
            'optipng' in cmd_name or 'ect' in cmd_name or
            'advpng' in cmd_name or 'pngwolf' in cmd_name)

  def fix_all_bad_numbers(self):
    # !!! Remove this once PdfObj.__init__ does it.
    # buggy pdfTeX-1.21a generated
    # /Matrix[1. . . 1. . .]/BBox[. . 612. 792.]
    # in eurotex2006.final.bad.pdf . Fix: convert `.' to 0.
    #
    # We may also wan to convert 612. to 612 elsewhere, to save 1 byte.
    for obj_num in sorted(self.objs):
      obj = self.objs[obj_num]
      if (obj.head.startswith(b'<<') and
          # !!! TODO(pts): Do proper PDF token sequence parsing.
          re.search(br'/Subtype[\x00\t\n\r\f ]*/Form\b', obj.head) and
          obj.get(b'Subtype') == '/Form'):
        matrix = obj.get(b'Matrix')
        if isinstance(matrix, str):
          obj.set(b'Matrix', obj.get_bad_numbers_fixed(matrix))
        bbox = obj.get(b'BBox')
        if isinstance(bbox, str):
          obj.set(b'BBox', obj.get_bad_numbers_fixed(bbox))
    return self

  def remove_unused_objs(self):
    """Removes unused objects from self.objs."""
    # Since PdfObj.head is a safe token sequence, we can use PDF_SIMPLE_REF_RE
    # to find references.
    _simple_ref_re = PdfObj.PDF_SIMPLE_REF_RE
    objs = self.objs
    reached_obj_nums = set()
    todo = [self.trailer.head]
    depth = 0
    while todo:
      depth += 1
      todo2 = []
      for head in todo:
        for match in _simple_ref_re.finditer(head):
          obj_num = int(match.group(1))
          if obj_num not in reached_obj_nums:
            obj = objs.get(obj_num)
            if obj:
              reached_obj_nums.add(obj_num)
              todo2.append(obj.head)
      todo = todo2
    unused_count = 0
    for obj_num in sorted(objs):
      if obj_num not in reached_obj_nums:
        del objs[obj_num]
        unused_count += 1
    if unused_count:
      logger.log_info('eliminated %d unused objs, depth=%d' % (unused_count, depth))

  @classmethod
  def find_eqclasses(cls, objs, do_remove_unused=False, do_renumber=False, do_unify_pages=True):
    """Find equivalence classes in objs, return new objs.

    Args:
      objs: A dict mapping object numbers (or strings such as 'trailer') to
        PdfObj instances.
      do_remove_unused: A boolean indicating whether to remove all objects
        not reachable from 'trailer' etc.
      do_renumber: A boolean indicating whether to renumber all objects,
        ordered by decreasing number of referrers.
    Returns:
      A new dict mapping object numbers to PdfObj instances.
    """
    # List of list of desc ([obj_num, head_minus, stream, refs_to,
    # inrefs_count]). Each list of eqclasses is an eqiuvalence class of
    # object descs.
    eqclasses = []
    # Maps object numbers to an element of eqclasses.
    eqclass_of = {}

    # Maps (head_minus, stream) to a list of desc.
    by_form = {}
    # List of desc.
    search_todo = []
    for obj_num in objs: # sorted(objs):
      refs_to = []  # List of object numbers obj_num refers to).
      head = objs[obj_num].head
      # !! TODO(pts): reorder dicts to canonical order
      # CompressValue changes all generational refs to generation 0.
      head_minus = PdfObj.compress_value(head, obj_num_map='0', old_obj_nums_ret=refs_to, do_emit_strings_as_hex=True)
      stream = objs[obj_num].stream
      desc = [obj_num, head_minus, stream, refs_to, 0]
      if isinstance(obj_num, str):  # for 'trailer'
        eqclasses.append([desc])
        eqclass_of[obj_num] = eqclasses[-1]
        if do_remove_unused:
          search_todo.append(desc)
      elif (not do_unify_pages and
            stream is None and head_minus.startswith('<<') and
            objs[obj_num].get(b'Type') == '/Page'):
        # Make sure that /Page objects are not unified. xpdf and evince
        # display the error message `Loop in Pages tree' (but still display
        # the PDF) if we unify equivalent pages, but since the PDF spec
        # doesn't say that it's not allowed to unify equivalent pages, we do
        # unify (by avoiding this code block) by default, but the user can
        # say --do-unify-pages=false to disable /Page object unification.
        eqclasses.append([desc])
        eqclass_of[obj_num] = eqclasses[-1]
      else:
        form = (head_minus, stream)
        form_desc = by_form.get(form)
        if form_desc is not None:
          form_desc.append(desc)
          eqclass_of[obj_num] = form_desc
        else:
          eqclasses.append([desc])
          eqclass_of[obj_num] = by_form[form] = eqclasses[-1]
    del by_form  # save memory

    #for eqclass in eqclasses:
    #  for desc in eqclass:
    #    print desc
    #  print

    had_split = True
    while had_split:
      had_split = False
      for eqclass in eqclasses:
        assert eqclass
        if len(eqclass) > 1:
          desc = eqclass[0]
          refs_to = desc[3]
          eqlist = [desc]
          nelist = []
          for i in range(1, len(eqclass)):
            descb = eqclass[i]
            refs_tob = descb[3]
            j = 0
            while (j < len(refs_to) and
                   eqclass_of.get(refs_to[j]) is eqclass_of.get(refs_tob[j])):
              j += 1
            if j == len(refs_to):
              eqlist.append(descb)
            else:
              nelist.append(descb)
          if nelist:  # everybody in eqclass is equivalent to desc
            had_split = True
            eqclasses.append(nelist)
            for descb in nelist:
              assert eqclass_of[descb[0]] is eqclass
              eqclass_of[descb[0]] = nelist
            eqclass[:] = eqlist

    eliminated_count = len(objs) - len(eqclasses)
    assert eliminated_count >= 0
    if eliminated_count > 0:
      logger.log_info('eliminated %s duplicate objs' % eliminated_count)

    # Set of eqclass-leader object numbers.
    unused_obj_nums = set()
    if do_remove_unused or do_renumber:
      unused_obj_nums = set([eqclass[0][0] for eqclass in eqclasses])
      for desc in search_todo:
        unused_obj_nums.remove(desc[0])
      for desc in search_todo:  # breadth-first search from trailer
        for obj_num in desc[3]:  # refs_to
          target_class = eqclass_of.get(obj_num)
          if target_class is not None:
            target_class[0][4] += 1  # inrefs_count
            target_obj_num = target_class[0][0]
            if target_obj_num in unused_obj_nums:
              search_todo.append(target_class[0])
              unused_obj_nums.remove(target_obj_num)
      if not do_remove_unused:
        unused_obj_nums.clear()
      elif unused_obj_nums:
        logger.log_info(
            'eliminated %s unused objs in %s classes' %
            (sum([len(eqclass_of[obj_num]) for obj_num in unused_obj_nums]),
             len(unused_obj_nums)))

    # Maps eqclass-leader object number to object number.
    obj_num_map = {}
    if do_renumber:
      descs = [eqclass[0] for eqclass in eqclasses if not isinstance(eqclass[0][0], str) and eqclass[0][0] not in unused_obj_nums]
      descs.sort(key=lambda desc: (-desc[4], desc[0]))
      i = 0
      for desc in descs:
        i += 1
        obj_num_map[desc[0]] = i

    objs_ret = {}
    for eqclass in eqclasses:
      obj_num, head_minus, stream, refs_to, _ = eqclass[0]
      if obj_num in unused_obj_nums:
        continue

      refs_to_rev = refs_to[:]
      refs_to_rev.reverse()

      def replacement_ref(match):
        match_obj_num = int(match.group(1))
        assert match_obj_num == 0  # Real ref target is in refs_to_rev[-1].
        assert refs_to_rev
        target_obj_num = refs_to_rev.pop()
        new_class = eqclass_of.get(target_obj_num)
        if new_class is None:
          logger.log_warning(
              'obj %s missing, referenced by objs %r...' %
              (target_obj_num, [desc[0] for desc in eqclass]))
          return b'null'
        else:
          new_obj_num = new_class[0][0]
          return b'%d 0 R' % obj_num_map.get(new_obj_num, new_obj_num)

      head = PdfObj.PDF_SIMPLE2_REF_RE.sub(replacement_ref, head_minus)
      assert not refs_to_rev

      # Since above we've called PdfObj.CompressValue(...,
      # do_emit_strings_as_hex=True), we have to undo it (i.e. make hex strings
      # binary instead) here.
      head = PdfObj.PDF_HEX_STRING_OR_DICT_RE.sub(
          lambda match: (match.group(1) is not None and
              PdfObj.serialize_pdf_string_safe(match.group(1))
              or b'<<'), head)

      obj = PdfObj(None)
      obj.head = head
      obj.stream = stream
      objs_ret[obj_num_map.get(obj_num, obj_num)] = obj

    return objs_ret

  def optimize_streams(self, do_decompress_only=False):
    """Recompress all non-image streams, keep the smallest.

    Args:
      do_decompress_only: Decompress all non-image streams, don't attempt to
        compress them.
    """
    # TODO(pts): Merge much of the code from here to SetStreamAndCompress.

    counts = {}
    skipped_count = 0
    for obj_num in sorted(self.objs):
      obj = self.objs[obj_num]
      if obj.stream is None:
        skipped_count += 1
        continue
      if (b'/Subtype' in obj.head and b'/Image' in obj.head and
          obj.get(b'Subtype') == b'/Image'):
        # Force regeneration from obj._cache, give self.OptimizeObjs a better
        # chance to find duplicates.
        #
        # TODO(pts): Do this regeneration from self.OptimizeObjs.
        obj._head = None
        skipped_count += 1
        #print obj.head
        continue

      obj_infos = []
      if obj.has_uncompressed_stream():
        data, filter_value = obj.stream, None
        obj.set(b'Filter', None)
        obj.set(b'DecodeParms', None)
        # '#' has a small ASCII code, so prefer '#orig' to 'zip'.
        obj_infos.append((obj.size, '#orig', obj))
      else:
        filter_value = str(obj.get(b'Filter'))
        # Keep objects with lossy filters untouched.
        if '/DCTDecode' in filter_value or '/JPXDecode' in filter_value:
          skipped_count += 1
          continue
        try:
          data = obj.get_uncompressed_stream(self.objs)
        except (FilterNotImplementedError, FilterError) as e:
          logger.log_warning('error decompressing obj %d: %s' % (obj_num, e))
          counts['#dec-error'] = counts.get(b'#dec-error', 0) + 1
          continue
        obj_infos.append((obj.size, '#orig', obj))
        obj2 = PdfObj(obj)
        obj2.stream = data
        obj2.set(b'Filter', None)
        obj2.set(b'DecodeParms', None)
        obj2.set(b'Length', len(obj2.stream))
        obj_infos.append((obj2.size, 'uncompressed', obj2))
        del obj2  # Save memory.

      if do_decompress_only:
        del obj_infos[:-1]  # Keep only the last, uncompressed stream.
      else:
        # Try flate with maximum effort.
        obj2 = PdfObj(obj)
        obj2.stream = zlib.compress(data, 9)
        obj2.set(b'Length', len(obj2.stream))
        obj2.set(b'Filter', b'/FlateDecode')
        obj2.set(b'DecodeParms', None)
        obj_infos.append((obj2.size, 'zip', obj2))
        del obj2  # Save memory.

        # TODO(pts): Additionally, try advzip etc., or flate with
        #            predictors, like in SetStreamAndCompress.

        obj_infos.sort()

      self.objs[obj_num] = obj_infos[0][2]  # Pick the smallest.
      counts[obj_infos[0][1]] = counts.get(obj_infos[0][1], 0) + 1
      del obj_infos  # Save memory.

    if do_decompress_only:
      what = 'decompressed'
    else:
      what = 'optimized'
    if counts:
      msg = ', '.join('%d %s' % (c, k) for k, c in sorted(counts.items()))
    else:
      msg = 'none'
    logger.log_info(
        '%s %d streams, kept %s' % (what, len(self.objs) - skipped_count, msg))

  def compress_uncompressed_streams(self):
    """Compress uncompressed stream data in all objects.

    Only /FlateDecode will be tried. If it's larger than the original, then
    the original will be kept.
    """
    compress_count = uncompressed_count = 0
    for pdf_obj in self.objs.values():
      if (pdf_obj.stream is not None and
          pdf_obj.head.startswith(b'<<') and
          pdf_obj.get(b'Filter') in (None, '[]')):
        pdf_obj.set_stream_and_compress(
            pdf_obj.get_uncompressed_stream(self.objs), pdf=self)
        if pdf_obj.get(b'Filter'):
          uncompressed_count += 1
        compress_count += 1
    logger.log_info('compressed %d streams, kept %d of them uncompressed' % (compress_count, uncompressed_count))

  def optimize_objs(self, do_unify_pages):
    """Optimize PDF objects.

    This method does the following:

    * Calls PdfObj.CompressValue for all obj.head
    * Removes unused objs.
    * Removes duplicate objs.
    * In multiple iterations, removes duplicate trees.
    * Removes gaps between object numbers.
    * Reorders objs so most-referenced objs come early.

    This method unifies equivalent sets with circular references (just like
    Multivalent).
    TODO(pts): Test this with: pts2.zip.4times.pdf and tuzv.pdf

    Args:
      do_unify_pages: Unify equivalent /Type/Page objects to a single object.
    Returns:
      self.
    """
    # TODO(pts): Inline ``obj null endobj'' and ``obj<<>>endobj'' etc.
    self.objs['trailer'] = self.trailer
    new_objs = self.find_eqclasses(
        self.objs, do_remove_unused=True, do_renumber=True,
        do_unify_pages=do_unify_pages)
    self.trailer = new_objs.pop('trailer')
    self.objs.clear()
    self.objs.update(new_objs)
    return self

  def parse_sequentially(self, data, file_name=None, offsets_out=None,
                         obj_num_by_ofs_out=None, setitem_callback=None):
    """Load a PDF by parsing the file data sequentially.

    This method overrides the old contents of self from data.

    This method is more relaxed and more lazy than Load, because it can
    parse a PDF with an xref stream (/Type/XRef; instead of an xref table). It
    doesn't parse the xref stream, however. It also doesn't decode
    object streams (/Type/ObjStm), so it's oblivious about the contents of
    non-stream objects.

    Args:
      data: String containing PDF file data.
      file_name: File name of the PDF, or None.
      offsets_out: None or list to append object offsets, the xref offset
        (if any) and the trailer offset.
      obj_num_by_ofs_out: None or dict that will be populated with object
        offsets mapped by object numbers.
      setitem_callback: None or function taking obj_num, pdf_obj and enf_ofs
        as arguments. The default implementation extends self.objs.
    Returns:
      self.
    Raises:
      PdfTokenParseError: On error, the state of self is unknown, it may be
        partially filled.
      TypeError:
    """
    # TODO(pts): Add unit tests for this method.

    if obj_num_by_ofs_out is None:
      obj_num_by_ofs_out = {}
    elif not isinstance(obj_num_by_ofs_out, dict):
      raise TypeError

    def default_set_item(obj_num, pdf_obj, unused_end_ofs):
      if obj_num is not None:
        if obj_num in self.objs:
          raise PdfTokenParseError('duplicate object number %s' % obj_num)
        self.objs[obj_num] = pdf_obj

    if setitem_callback is None:
      setitem_callback = default_set_item

    match = PdfObj.PDF_VERSION_HEADER_RE.search(data[:256])
    if not match:
      raise PdfTokenParseError('unrecognized PDF signature %r' % data[: 16])
    data = data[match.start():]
    version = match.group(1)
    header_end_ofs = match.end()
    setitem_callback(None, match.group(), 'header')

    # We set xref_ofs if available. It is not an error not to have it
    # (e.g. with a broken PDF with xref + trailer).
    xref_ofs = None
    i = data.rfind('startxref')
    if i >= 0:
      match = PdfObj.PDF_STARTXREF_EOF_AT_EOS_RE.match(data, i - 1)
      if match:
        xref_ofs = int(match.group(1))

    # TODO(pts): Fill this properly. We'd have to parse the xref table and
    # xref stream though.
    self.has_generational_objs = False
    self.version = version
    self.objs.clear()
    self.trailer = None
    self.file_name = file_name
    self.file_size = len(data)
    i = header_end_ofs
    end_ofs_out = []
    # None or a dict mapping object numbers to their start offsets in data.
    obj_starts = None
    length_objs = {}
    ws = PdfObj.PDF_WHITESPACE_CHARS

    # When this loop exist, data[i : i + 16].startswith('startxref') will be true.
    while 1:
      if i >= len(data):
        raise PdfTokenParseError('unexpeted EOF in PDF')
      i0 = i

      # It's important that it doesn't match leading whitespace, so we'll count
      # leading whitespace as wasted.
      match = PdfObj.PDF_OBJ_DEF_OR_XREF_RE.search(data, i)
      if not match:
        raise PdfTokenParseError('next obj or xref or startxref not found at ofs=%d' % i)
      i = match.start()
      if i0 != i:
        # Report wasted bytes between objs.
        setitem_callback(None, data[i0 : i], 'wasted')

      prefix = data[i : i + 16]
      if prefix.startswith('startxref'):
        break
      if prefix.startswith('xref'):
        i0 = i
        match = PdfObj.PDF_TRAILER_WORD_RE.search(data, i)
        if not match:
          raise PdfTokenParseError('cannot find trailer after xref')
        trailer_ofs = match.start(1)
        j = trailer_ofs
        while data[j - 1] in ws:
          j -= 1
        if data[j : j + 2] in (' \n', ' \r', '\r\n'):
          j += 2
        callback_calls = [(None, data[i : j], 'xref')]
        if trailer_ofs > j:
          # TODO(pts): Also add comments in here.
          callback_calls.append((None, data[j : trailer_ofs], 'whitespace_after_xref'))
        i = trailer_ofs
        del end_ofs_out[:]
        # TODO(pts): What if there are multiple trailers (linearized)?
        self.trailer = PdfObj.parse_trailer(data, start=i, end_ofs_out=end_ofs_out)
        self.trailer.set(b'XRefStm', None)
        self.trailer.set(b'Prev', None)
        if self.trailer.get(b'Type') is not None:
          raise PdfTokenParseError('unexpected trailer obj type: %s' % self.trailer.get(b'Type'))
        i = end_ofs_out[-1]
        if data[i : i + 1] in ws:
          i += 1
        i1 = i
        # Usually there is a single space only.
        match = PdfObj.PDF_COMMENTS_OR_WHITESPACE_RE.match(data, i)
        if match:
          i = match.end()
        if PdfObj.PDF_STARTXREF_EOF_AT_EOS_RE.match(data, i - 1):
          callback_calls.append((None, data[trailer_ofs : i1], 'trailer'))
          if i > i1:
            callback_calls.append((None, data[i1 : i], 'whitespace_after_trailer'))
          for callback_call in callback_calls:
            setitem_callback(*callback_call)
          callback_calls = None  # Save memory.
          break
        # We reach this point in case of a linearized PDF. We usually have
        # `startxref <offset> %%EOF' here, and then we get new objs.
        match = PdfObj.PDF_STARTXREF_EOF_RE.match(data, i - 1)
        if match:
          i = match.end()
        elif data[i : i + 9].startswith('startxref'):  # Fallback.
          i += 9
        # This contains 'xref ... trailer ... startxref ... %%EOF\n'.
        setitem_callback(None, data[i0 : i], 'linearized_xref')
        continue
      if prefix.startswith('trailer'):
        raise PdfTokenParseError('unexpected trailer at ofs=%d' % i)
      del end_ofs_out[:]  # Save memory.
      obj_num = int(match.group(1))
      try:
        pdf_obj = PdfObj(data, start=i, end_ofs_out=end_ofs_out, file_ofs=i, objs=length_objs)
      except PdfIndirectLengthError as exc:
        # For testing: eurotex2006.final.pdf and lme_v6.pdf
        if obj_starts is None:
          obj_starts, self.has_generational_objs = self.parse_using_xref(
              data,
              do_ignore_generation_numbers=self.do_ignore_generation_numbers)
        j = obj_starts[exc.length_obj_num]
        if exc.length_obj_num not in length_objs:
          if not isinstance(j, PdfObj):
            j = PdfObj(data, start=j, file_ofs=j)
          length_objs[exc.length_obj_num] = j
        pdf_obj = PdfObj(data, start=i, end_ofs_out=end_ofs_out, file_ofs=i,
                         objs=length_objs)
      if offsets_out is not None:
        offsets_out.append(i)
      obj_num_by_ofs_out[i] = obj_num
      if xref_ofs == i:
        self.trailer = pdf_obj
        if self.trailer.get(b'Type') != '/XRef':
          raise PdfTokenParseError('unexpected trailer obj type: %s' % self.trailer.get(b'Type'))
      assert end_ofs_out[-1] > i
      i = end_ofs_out[-1]
      setitem_callback(obj_num, pdf_obj, i)  # self.objs[obj_num] = pdf_obj

    # Parse and check the startxref number.
    #
    # Postcondition of the loop above.
    assert data[i : i + 9].startswith('startxref')
    offsets_out.append(i)  # startxref
    match = PdfObj.PDF_STARTXREF_EOF_AT_EOS_RE.match(data, i - 1)
    if not match:
      raise PdfTokenParseError('startxref syntax error at ofs=%d' % i)
    assert xref_ofs == int(match.group(1))

    if self.trailer is None:
      raise PdfTokenParseError('trailer/xref obj not found')
    # Postcondition of the code above.
    assert self.trailer.get(b'Type') in ('/XRef', None)
    return self

  @classmethod
  def msb_first_to_integer(cls, s):
    """Convert a string containing a base-256 MSBFirst number to an integer."""
    # TODO(pts): Optimize this, including calls.
    assert isinstance(s, str)
    assert s
    if len(s) == 1:
      return ord(s)
    elif len(s) == 2:
      return struct.unpack('>H', s)[0]
    elif len(s) == 4:
      return int(struct.unpack('>L', s)[0])
    else:
      ret = 0
      for c in s:
        ret = ret << 8 | ord(c)
      return ret

  PDFDATA_MULTIVALENT_EXT_SUB_RE = re.compile(r'[.][^.]+\Z')

  def save(self, file_name, display_file_name,
           do_update_file_meta,
           do_generate_xref_stream,
           do_generate_object_stream,
           is_flate_ok):
    """Save this PDF to a file, with or without Multivalent.

    Args:
      file_name: PDF file name to save self to.
      display_file_name: PDF file name to display.
      do_update_file_meta: bool indicating whether self.file_name and
        self.file_size should be updated after a successful save.
      is_flate_ok: bool indicating if it's OK to generate xref and object
        streams with /Filter/FlateDecode.
    """
    if not display_file_name:
      display_file_name = file_name
    assert do_generate_xref_stream or not do_generate_object_stream, 'Object streams need an xref stream.'

    logger.log_info('saving PDF with %s objs to: %s' % (len(self.objs), display_file_name))
    self._assert_before_write()

    jobs = [[dict(
        is_flate_ok=is_flate_ok,
        do_generate_xref_stream=do_generate_xref_stream,
        do_generate_object_stream=do_generate_object_stream),
        'original', 0, None]]
    # This is an upper estimate of the byte size of the generated PDF files,
    # because it assumes do_generate_xref_stream=False and
    # do_generate_object_stream=False.
    estimated_size = 40 + self.trailer.size + sum(pdf_obj.size for pdf_obj in self.objs.values())
    if estimated_size < 10000 and len(self.objs) < 40:
      # The file is small, so it may be worth trying other settings.
      if do_generate_xref_stream and do_generate_object_stream:
        jobs.append([dict(
            is_flate_ok=is_flate_ok, do_generate_xref_stream=True,
            do_generate_object_stream=False), 'xrefstm', 1, None])
      if do_generate_xref_stream:
        jobs.append([dict(
            is_flate_ok=is_flate_ok, do_generate_xref_stream=False,
            do_generate_object_stream=False), 'nostm', 2, None])
    if len(jobs) > 1:
      logger.log_info('trying %d jobs and using the smallest' % len(jobs))

    for job in jobs:
      output = []
      output_size = self.append_serialized_pdf(output=output, **job[0])
      if len(jobs) > 1:
        logger.log_info(
            'job %s generated %d bytes (%s)' %
            (job[1], output_size, format_percent(output_size, self.file_size)))
      job[3] = b''.join(output)
      del output  # Save memory.
      assert len(job[3]) == output_size

    if len(jobs) > 1:
      jobs.sort(key=lambda job: (job[3], job[2]))
      logger.log_info('jobs result: %s' % (' '.join(['%s=%d' % (job[1], len(job[3])) for job in jobs])))
      del jobs[1:]  # Save memory.

    output_size = len(jobs[0][3])
    logger.log_info('generated %d bytes (%s)' % (output_size, format_percent(output_size, self.file_size)))
    if output_size > self.file_size:
      logger.log_warning('optimized PDF larger than original')
    f = open(file_name, 'wb')
    try:
      f.write(jobs[0][3])
    finally:
      f.close()
    if do_update_file_meta:
      self.file_size = output_size
      self.file_name = file_name
