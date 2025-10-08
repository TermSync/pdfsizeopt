class Error(Exception):
  """Comon base class for exceptions defined in this module."""


class PdfOptimizeError(Error):
  """Raised if an expected optimization couldn't be performed."""


class PdfNoObjsError(Error):
  """Raised if a PDF contains no objects after loading."""


class PdfTokenParseError(Error):
  """Raised if a string cannot be parsed to a PDF token sequence."""


class PdfMissingRootError(Error):
  """Raised when the /Root reference is missing from the PDF trailer."""


class UnexpectedStreamError(Error):
  """Raised when ResolveReferences gets a ref to an obj with stream."""


class PdfReferenceTargetMissing(Error):
  """Raised if the target obj for an <x> <y> R is missing."""


class PdfReferenceRecursiveError(Error):
  """Raised if a PDF object reference is recursive."""


class PdfIndirectLengthError(PdfTokenParseError):
  """Raised if an obj stream /Length is an unresolvable indirect reference.

  The attribute length_obj_num might be set to the object number holding the
  length.
  """


class PdfUnexpectedIlStreamError(PdfTokenParseError):
  """Raised if a PDF obj with an indirect /Length is found where unexpected."""


class PdfTokenTruncated(PdfTokenParseError):
  """Raised if a string is only a prefix of a PDF token sequence."""


class PdfTokenNotString(Error):
  """Raised if a PDF token sequence is not a single string."""


class PdfTokenNotSimplest(Error):
  """Raised if ParseSimplestDict cannot parse the PDF token sequence."""


class FormatUnsupported(Error):
  """Raised if a file/data to be loaded is valid, but not supported."""


class PdfXrefError(Error):
  """Raised if the PDF file doesn't contain a valid cross-reference table."""


class PdfXrefStreamError(PdfXrefError):
  """Raised if the PDF file doesn't contain a valid cross-reference stream."""


class PdfXrefStreamWidthsError(PdfXrefStreamError):
  """Raised if the xref stream trailer does not contain a valid /W value."""


class FontsNotMergeable(Error):
  """Raised if the specified fonts cannot be merged.

  Please note that `Parsable' and `Mergeable' are correct spellings.
  """


class FilterNotImplementedError(Error):
  """Raised if a stream filter is not implemented."""


class FilterError(Error):
  """Raised if a stream filter failed to to process its input.

  A typical reason is corrupt compressed data in the input PDF.
  """


class PdfFileEncryptedError(Error):
  """Raised when an encrypted PDF file is encountered."""
