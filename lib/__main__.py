#!/usr/bin/python
import os
import os.path
import sys
import getopt

from lib.util.constants import DEFAULT_VERBOSITY
from lib.util.logging import Logger
from lib.util.util import get_version_spec, get_used_script_dir, prepend_to_path, get_libexec_dir, setup_tmp_prefix, rename

from lib.pdfsizeopt.Flags import Flags
from lib.pdfsizeopt.PdfObj import PdfObj
from lib.pdfsizeopt.PdfData import PdfData

# pdfsizeopt: PDF file size optimizer
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
# pdfsizeopt works with Python 2.4, 2.5, 2.6 and 2.7. It doesn't work with
# Python 3.x.
#
# This Python script implements some techniques for making PDF files smaller
# without any visual quality or interactivity loss.
# See also https://github.com/pts/pdfsizeopt for more information,
# including documentation, installation instructions, a white paper describing
# what optimizations are done in this script and why, and presentation slides
# about the same.
#
# This script needs a Unix system, with Ghostscript (gs), sam2p or imgdataopt,
# pngout (--use-pngout=no to disable), jbig2 (--use-jbig2=no to disable)
# Future versions may relax the system requirements.
#
# This script doesn't optimize the serialization of objects it doesn't modify.
# Use tool.pdf.Compress in Multivalent.jar from http://multivalent.sf.net/ for
# that. This script runs Multivalent if --use-multivalent=yes is specified.

__author__ = 'pts@fazekas.hu (Peter Szabo)'

# TODO(pts): Add a shorthand for (and then allow override later in the command-line -- or also earlier?): --do-optimize-images=no --do-optimize-fonts=no --do-optimize-objs=no --do-optimize-streams=no --do-decompress-most-streams=yes --do-generate-xref-stream=no --do-generate-object-stream=no
# TODO(pts): Add cleanup flag to os.remove temporary files even on an exception.
# TODO(pts): re.compile anywhere (no re.match or re.search).

# We don't want to have a '$' + 'Id' in this file, because downloading the
# script from http://pdfsizeopt.googlecode.com/svn/trunk/pdfsizeopt.py
# won't expand that to a useful version number.

__pychecker__ = 'maxlines=999 maxlocals=99 unusednames=self,cls maxreturns=99 maxbranches=9999'

bytearray_tostring = bytearray.__str__

logger = Logger(DEFAULT_VERBOSITY)


def optimize(argv, script_dir=None, zip_file=None):
  welcome_msg = 'This is %s.' % get_version_spec(zip_file)
  try:
    if not argv:
      argv = ['pdfsizeopt']
    f = Flags()
    # TODO(pts): Use `import win32api; print(win32api.GetCommandLine())' on
    # Windows to detect double quotes around file names, and thus accept a PDF
    # with double quotes in the file name.
    f.parse(argv)

    if f.mode == 'optimize':
      if not f.args:
        if not f.do_debug_gs and not f.do_debug_image_optimizers:
          raise getopt.GetoptError('missing input filename in command-line')
        output_file_name = file_name = None
      elif len(f.args) == 1:
        file_name = f.args[0]
        if file_name[-4:].lower() == '.pdf':
          output_file_name = file_name[:-4]
        else:
          output_file_name = file_name
        output_file_name += '.pso.pdf'
      elif len(f.args) == 2:
        file_name = f.args[0]
        output_file_name = f.args[1]
      else:
        raise getopt.GetoptError('too many command-line args')

      if f.do_generate_object_stream and not f.do_generate_xref_stream:
        raise getopt.GetoptError('--do-generate-object-stream=yes requires --do-generate-xref-stream=yes')

  except getopt.GetoptError as exc:
    logger.log_fatal('%s\nfatal: error in command line: %s' % (welcome_msg, exc), 1)

  logger.log_info(welcome_msg)
  assert f.mode == 'optimize'  # Implemented below.

  used_script_dir = get_used_script_dir(script_dir, zip_file)
  libexec_dir = get_libexec_dir(used_script_dir)
  if libexec_dir is not None:
    prepend_to_path(libexec_dir)  # Find external tools in libexec_dir first...
  else:
    prepend_to_path(used_script_dir)  # ... otherwise, find them in script dir.
  del used_script_dir  # Make sure it's not used.

  # Call it before the first call to GetGsCommand(...).
  PdfObj.tmp_prefix = setup_tmp_prefix(output_file_name, f.tmp_dir)

  if output_file_name is None:  # Just --do-debug-gs=yes.
    return

  # It's OK that file_name == output_file_name: we don't read and write them
  # at the same time.
  pdf = PdfData(do_ignore_generation_numbers=f.do_ignore_generation_numbers).load(file_name)
  pdf.remove_unused_objs()
  pdf.fix_all_bad_numbers()
  if f.do_optimize_streams:
    # We call this before pdf.OptimizeObjs, so pdf.OptimizeObjs can found
    # more duplicate objs (in case the same stream data was compressed
    # differently).
    pdf.optimize_streams(do_decompress_only=False)
  if f.do_optimize_objs or f.do_remove_generational_objs:
    # TODO(pts): Do only a simpler optimization with renumbering if
    # f.do_optimize_objs is false and f.do_remove_generational_objs is true.
    pdf.optimize_objs(do_unify_pages=f.do_unify_pages)
  elif f.do_optimize_obj_heads:
    pdf.trailer.head = PdfObj.compress_value(pdf.trailer.head)
    for obj in pdf.objs.values():
      obj.head = PdfObj.compress_value(obj.head)
  # TODO(pts): Better handle which of f.do_compress_uncompressed_streams
  #            and f.do_decompress_most_streams takes precedence. Maybe
  #            that which is specified on the command-line (?).
  if (f.do_compress_uncompressed_streams and not f.do_decompress_most_streams):
    pdf.compress_uncompressed_streams()
  pdf.save(
      output_file_name + '.tmp',
      display_file_name=output_file_name,
      do_update_file_meta=True,
      do_generate_xref_stream=f.do_generate_xref_stream,
      do_generate_object_stream=f.do_generate_object_stream,
      is_flate_ok=(f.do_compress_uncompressed_streams and not f.do_decompress_most_streams))
  rename(output_file_name + '.tmp', output_file_name)


def main():
  __file__ = globals()['__file__']
  script_dir = os.path.dirname(__file__)
  try:
    __file__ = os.path.join(script_dir, os.readlink(__file__))
    script_dir = os.path.dirname(__file__)
  except (OSError, AttributeError, NotImplementedError):
    pass
  if os.path.isfile(os.path.join(script_dir, 'lib', 'pdfsizeopt', 'main.py')):
    sys.path[0] = os.path.join(script_dir, 'lib')

  sys.exit(optimize(sys.argv, script_dir=script_dir))

if __name__ == '__main__':
  main()