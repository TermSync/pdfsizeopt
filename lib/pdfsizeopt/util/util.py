import zlib
import struct


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