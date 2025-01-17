#!/usr/bin/env python

import gimp
import gimpfu
from gimpfu import pdb
import gimpui
import gimpenums

import gtk
import gobject

import array
import ast
import colorsys
import datetime
import logging
import os
import os.path
import pprint
import re
import shutil
import string
import sys

import cProfile, pstats, StringIO
profiler = None
def startProfile():
  global profiler
  profiler = cProfile.Profile()
  profiler.enable()
def endProfile():
  global profiler
  profiler.disable()
  s = StringIO.StringIO()
  s.write('\n')
  sortby = 'cumulative'
  ps = pstats.Stats(profiler, stream=s).sort_stats(sortby)
  ps.print_stats()
  logging.debug(s.getvalue())

# Attempt to add default Python 2.7 install to allow GIMP's Python to find
# dependencies
if os.name == 'nt':
  sys.path.append(r'C:\Python27\Lib\site-packages')

# Try to import `appdirs` and set up the logging infrastructure. There *really*
# should be no reason `appdirs` does not import properly because the file is
# right there! However, just in case I'm going to assume Viet screwed up so
# we're on his computer and so we'll set up the output streams to his home
# directory.
try:
  import appdirs
  # Get a platform specific logging directory for application name
  # `gimp-label-toolbox`.
  log_file = appdirs.user_log_dir('gimp-label-toolbox')
  # The function specifies the log file as just `log`. We want two different
  # log files (one to act as `stderr` and one to act as `stdout`) so we'll grab
  # the path and specify our own file names.
  log_dir = os.path.split(log_file)[0]
except ImportError:
  log_dir = '/home/vnguyen'
if not os.path.exists(log_dir):
  os.makedirs(log_dir)
err_log_file = os.path.join(log_dir, 'err.log')
out_log_file = os.path.join(log_dir, 'out.log')
log_log_file = os.path.join(log_dir, 'log.log')
# Now we'll redirect our `stderr` and `stdout` to these files and initialize
# our logging infrastructure with a very verbose prefix.
sys.stderr = open(err_log_file, 'w', buffering=0)
sys.stdout = open(out_log_file, 'w', buffering=0)
logging.basicConfig(filename=log_log_file,
                    level=logging.DEBUG,
                    format='%(asctime)s : %(filename)s:%(lineno)d : %(funcName)s : %(levelname)s : %(message)s',
                    datefmt='%m/%d/%Y %I:%M:%S %p')
logging.info('Toolbox session started.')
logging.info('Python executable: %s' % sys.executable)
logging.info('Python paths: %s' % sys.path)

# NumPy and SciPy are required dependencies. We'll try to import them here. If
# we fail we'll set a flag that will trigger a message later and then close the
# toolbox.
try:
  import numpy as np
  from scipy.io import savemat, loadmat
  imports_succeeded = True
except ImportError:
  imports_succeeded = False

# Scikit-Image is an optional dependency that determines of the SLIC helper is
# available.
try:
  from skimage.segmentation import slic
  slic_available = True
except ImportError:
  slic_available = False

def makeValidVariableName(s):
  '''
  Cleans up a string so that it is a valid variable name.

  Take from <http://stackoverflow.com/questions/3303312/how-do-i-convert-a-string-to-a-valid-variable-name-in-python>.
  '''
  # The following regular expression is a special case for GIMP's default
  # pasted layer name.
  s = re.sub(r'Pasted Layer', r'LabelPasted', s)
  # The following two regular expressions are a special case for GIMP's default
  # GroupLayer name.
  s = re.sub(r'Layer Group', r'LabelGroup', s)
  s = re.sub(r' #(\d+)', r'\1', s)
  # The next three regular expressions are adopted from the URL listed in the
  # docstring above.
  s = re.sub(' ', '_', s)
  s = re.sub('[^0-9a-zA-Z_]', '', s)
  s = re.sub('^[^a-zA-Z_]+', '', s)
  return s

def makeColormap(n):
  '''
  Produces a color map that maps an integer from `0` to `n-1`. The map is
  actually just a NumPy array (`dtype=uint8`, `shape=(n, 3)`). The algorithm
  supports up to 2204 unique colors and raises a `ValueError`. It will also
  attempt to space the colors out across the hue-saturation-value spectrum.
  '''
  if n < 1 or n > 2204:
    raise ValueError('Cannot produce more than 2204 unique colors.')
  i = np.arange(n)
  iii = np.array(np.vstack((i, i, i)).T, dtype='float64')
  fff = iii / n
  # TODO: Explain where this 800 comes from (rough discretization threshold)
  if n <= 800:
    hsv = np.apply_along_axis(lambda x: (x[0], 1.0, 0.8), 1, fff)
  else:
    # TODO: Explain what's going on here
    # can produce 2204 unique colors
    period = 512.0 / float(n)
    freq = float(n) / 512.0
    hsv = np.apply_along_axis(lambda x: (np.fmod(x[0], period) / period,
                                        1.0,
                                        ( 1.0 - np.floor(x[0] / period) * period ) * 0.6 + 0.4),
                              1,
                              fff)
  rgbf = np.apply_along_axis(lambda x: colorsys.hsv_to_rgb(*x), 1, hsv)
  rgb = np.array(rgbf * 254 + 1, dtype='uint32')
  rgb[0, :] = 0 # hard coded mapping for transparent
  # verify that the mapping is unique
  if not len(np.unique(rgb[:, 0] * 256 * 256 + rgb[:, 1] * 256 + rgb[:, 2])) == n:
    gimp.message('Cannot produce a unique color map for {0} values!'.format(n))
    raise AssertionError('Cannot produce a unique color map for {0} values!'.format(n))
  return np.array(rgb, dtype='uint8')

def preorderRecurse(node, parent_data, dataFunc, childrenFunc, *args):
  data = dataFunc(node, parent_data, *args)
  child_list = [preorderRecurse(child, data, dataFunc, childrenFunc, *args) for child in childrenFunc(node)]
  return (data, child_list)

def layerHierarchyFromImage(image):
  dataFunc = lambda x, y: (x.name, x.ID, x)
  def childrenFunc(node):
    if type(node) == gimp.Layer:
      return []
    else:
      return node.layers
  return [preorderRecurse(root_layer, None, dataFunc, childrenFunc) for root_layer in image.layers]

def cleanLayerHierarchyNames(root_layers):
  def dataFunc(node, parent_data):
    data, children = node
    layer_name, layer_id, layer = data
    layer.name = makeValidVariableName(layer.name)
    return (layer.name, layer_id, layer)
  childrenFunc = lambda x: x[1]
  return [preorderRecurse(root_layer, None, dataFunc, childrenFunc) for root_layer in root_layers]

def layerHierarchiesEqualRecurse(a, b):
  (a_name, a_id, a_item), a_children = a
  (b_name, b_id, b_item), b_children = b
  if a_name != b_name or a_id != b_id or a_item != b_item:
    return False
  if len(a_children) != len(b_children):
    return False
  for a_child, b_child in zip(a_children, b_children):
    if not layerHierarchiesEqualRecurse(a_child, b_child):
      return False
  return True

def layerHierarchiesEqual(a_root, b_root):
  if len(a_root) != len(b_root):
    return False
  for a_root_item, b_root_item in zip(a_root, b_root):
    if not layerHierarchiesEqualRecurse(a_root_item, b_root_item):
      return False
  return True

class LabelToolbox(gtk.Window):

  def __init__ (self, image, *args):

    # toolbox settings
    self.comment_relative_path = '../comment'
    self.label_relative_path = '../label'
    self.map_relative_path = '..'

    # toolbox states
    self.is_image_open = False
    self.set_path = ''
    self.image_full_path = ''
    self.working_path = ''
    self.image_list = []
    self.image_index = 0
    self.image_filename = ''
    self.image_name = ''
    self.image_extension = ''

    # interface states
    self.last_layer_hierarchy = []
    self.last_foreground_color = None

    # internal representation
    self.image = image
    self.original_layer = None
    
    # widget groups
    self.only_available_with_open_image = []
    self.selection_interface = []
    self.slic_interface = []

    # TODO: Support multiple label layers
    # TODO: Change this according to number of labels
    self.num_of_labels = 1024 # TODO: needs to be number of labels + 1 so that zero is reserved for empty label
    if imports_succeeded:
      self.colormap = makeColormap(self.num_of_labels)
      self.shufflemap = np.arange(self.num_of_labels)
      self.shuffle()

    self.label_int_to_name_map = {}
    self.label_name_to_int_map = {}

    window = gtk.Window.__init__(self, *args)
    self.show()
    self.set_border_width(0)
    self.set_keep_above(True)
    self.set_resizable(True)
    self.connect('destroy', gtk.main_quit)

    container = [self]
    
    widget = gtk.ScrolledWindow()
    widget.set_border_width(0)
    widget.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
    self.set_geometry_hints(widget, 216, -1, -1, -1, -1, -1, -1, -1, -1, -1)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.set_border_width(8)
    widget.show()
    widget.set_size_request(200, -1)
    container[-1].add_with_viewport(widget)
    container.append(widget)

    widget = gtk.Label('Current Image')
    widget.show()
    container[-1].add(widget)

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.open_image_button = gtk.Button('Open Image')
    widget.show()
    widget.connect('clicked', self.openImageButtonClicked)
    container[-1].add(widget)

    widget = self.open_set_button = gtk.Button('Open Set')
    widget.show()
    widget.connect('clicked', self.openSetButtonClicked)
    container[-1].add(widget)

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.image_name_box = gtk.Entry()
    widget.set_editable(False)
    widget.show()
    container[-1].add(widget)

    widget = self.save_label_mat_button = gtk.Button('Save')
    widget.show()
    widget.connect('clicked', self.saveLabelMatButtonClicked)
    container[-1].pack_end(widget, False, False, 0)
    self.only_available_with_open_image.append(widget)

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.jump_to_box = gtk.Entry()
    widget.set_editable(True)
    widget.show()
    widget.connect('activate', self.jumpImageButtonClicked)
    container[-1].add(widget)

    widget = self.jump_image_button = gtk.Button('Jump To')
    widget.show()
    widget.connect('clicked', self.jumpImageButtonClicked)
    container[-1].pack_end(widget, False, False, 0)
    self.only_available_with_open_image.append(widget)

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.previous_image_button = gtk.Button('Previous')
    widget.show()
    widget.connect('clicked', self.previousImageButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    widget = self.next_image_button = gtk.Button('Next')
    widget.show()
    widget.connect('clicked', self.nextImageButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    container.pop()

    widget = gtk.HSeparator()
    widget.show()
    container[-1].add(widget)

    widget = gtk.Expander('Comment')
    widget.show()
    widget.set_expanded(True)
    widget.set_resize_mode(gtk.RESIZE_PARENT)
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.comment = gtk.TextView()
    self.comment_buffer = self.comment.get_buffer()
    widget.set_wrap_mode(gtk.WRAP_CHAR)
    widget.show()
    #widget.connect('changed', self.commentChanged)
    self.comment_buffer.connect('changed', self.commentChanged)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    container.pop()

    container.pop()

    widget = gtk.HSeparator()
    widget.show()
    container[-1].add(widget)

    widget = gtk.Label('Current Foreground Color Label')
    widget.show()
    container[-1].add(widget)

    widget = self.current_label = gtk.Label()
    widget.set_text('Hello world!')
    widget.show()
    container[-1].add(widget)

    def completionMatchFunc(widget, key, tree_iter):
      model = widget.get_model()
      text = model.get_value(tree_iter, 0)
      if text is None:
        return False
      return text.startswith(key) or text.find(' ' + key) > -1 or text.find('_' + key) > -1

    self.completion = gtk.EntryCompletion()
    self.liststore = gtk.ListStore(str)
    self.completion.set_model(self.liststore)
    self.completion.set_text_column(0)
    self.completion.set_match_func(completionMatchFunc)
    self.completion.connect('match-selected', self.completionMatchSelected)

    widget = gtk.Expander('Select/Shuffle Label')
    widget.show()
    widget.set_expanded(True)
    widget.set_resize_mode(gtk.RESIZE_PARENT)
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.label_name = gtk.Entry()
    widget.set_completion(self.completion)
    widget.connect('activate', self.labelNameActivated)
    widget.show()
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.select_label_button = gtk.Button('Select Label')
    widget.show()
    widget.connect('clicked', self.selectLabelButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    widget = self.shuffle_colors_button = gtk.Button('Shuffle Colors')
    widget.show()
    widget.connect('clicked', self.shuffleColorsButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    container.pop()

    container.pop()

    container.pop()

    widget = gtk.HSeparator()
    widget.show()
    container[-1].add(widget)

    widget = gtk.Expander('Layer List')
    widget.show()
    widget.set_expanded(True)
    widget.set_resize_mode(gtk.RESIZE_PARENT)
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.layers_select_all_button = gtk.Button('Select All')
    widget.show()
    widget.connect('clicked', self.layersSelectAllButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    widget = self.layers_select_none_button = gtk.Button('Select None')
    widget.show()
    widget.connect('clicked', self.layersSelectNoneButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.layers_select_labels_button = gtk.Button('Select Labels')
    widget.show()
    widget.connect('clicked', self.layersSelectLabelsButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    widget = self.layers_invert_label_selection_button = gtk.Button('Invert Selection')
    widget.show()
    widget.connect('clicked', self.layersInvertLabelSelectionButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    container.pop()

    # Adopted from <http://www.pygtk.org/pygtk2tutorial/examples/basictreeview.py>
    widget = self.layer_list = gtk.TreeView()
    widget.show()
    self.layer_list_store = gtk.TreeStore(str, int)
    self.layer_list.set_model(self.layer_list_store)
    self.layer_list_column = gtk.TreeViewColumn('Layer')
    self.layer_list.append_column(self.layer_list_column)
    self.layer_list_cell_renderer = gtk.CellRendererText()
    self.layer_list_column.pack_start(self.layer_list_cell_renderer)
    self.layer_list_column.add_attribute(self.layer_list_cell_renderer, 'text', 0)
    self.layer_list_column.set_clickable(False)
    self.layer_list_selection = self.layer_list.get_selection()
    self.layer_list_selection.set_mode(gtk.SELECTION_MULTIPLE)
    self.layer_list.set_search_column(0)
    self.layer_list.set_reorderable(False)
    self.layer_list.set_rubber_banding(True)
    container[-1].add(widget)

    container.pop()

    container.pop()

    widget = gtk.HSeparator()
    widget.show()
    container[-1].add(widget)

    widget = gtk.Expander('Visibility/Opacity')
    widget.show()
    widget.set_expanded(True)
    widget.set_resize_mode(gtk.RESIZE_PARENT)
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.label_opacity_slider = gtk.HScale()
    widget.set_range(0.0, 100.0)
    widget.set_value(100.0)
    widget.connect('change-value', self.labelOpacitySliderChange)
    widget.show()
    container[-1].add(widget)

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.toggle_label_button = gtk.Button('Toggle Visibility')
    widget.show()
    widget.connect('clicked', self.toggleLabelButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    widget = self.normal_blend_button = gtk.Button('Normal Blend')
    widget.show()
    widget.connect('clicked', self.normalBlendButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.grain_blend_button = gtk.Button('Grain Blend')
    widget.show()
    widget.connect('clicked', self.grainBlendButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    widget = self.color_blend_button = gtk.Button('Color Blend')
    widget.show()
    widget.connect('clicked', self.colorBlendButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)

    container.pop()

    container.pop()

    container.pop()

    widget = gtk.HSeparator()
    widget.show()
    container[-1].add(widget)

    widget = gtk.Expander('Selection Helper')
    widget.show()
    widget.set_expanded(True)
    widget.set_resize_mode(gtk.RESIZE_PARENT)
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.label_selection_button = gtk.Button('Fill Label')
    widget.show()
    widget.connect('clicked', self.labelSelectionButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)
    self.selection_interface.append(widget)

    widget = self.label_delete_button = gtk.Button('Delete Label')
    widget.show()
    widget.connect('clicked', self.labelDeleteButtonClicked)
    container[-1].add(widget)
    self.only_available_with_open_image.append(widget)
    self.selection_interface.append(widget)

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.layer_alpha_selection_button = gtk.Button('Select Mask')
    widget.show()
    widget.connect('clicked', self.layerAlphaSelectionButtonClicked)
    container[-1].add(widget)

    widget = self.clear_selection_button = gtk.Button('Select None')
    widget.show()
    widget.connect('clicked', self.clearSelectionButtonClicked)
    container[-1].add(widget)
    self.selection_interface.append(widget)

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.harden_selection_button = gtk.Button('Un-Antialias')
    widget.show()
    widget.connect('clicked', self.hardenSelectionButtonClicked)
    container[-1].add(widget)
    self.selection_interface.append(widget)

    widget = self.smooth_selection_button = gtk.Button('Smooth')
    widget.show()
    widget.connect('clicked', self.smoothSelectionButtonClicked)
    container[-1].add(widget)
    self.selection_interface.append(widget)

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.invert_selection_button = gtk.Button('Invert')
    widget.show()
    widget.connect('clicked', self.invertSelectionButtonClicked)
    container[-1].add(widget)
    self.selection_interface.append(widget)

    widget = self.grow_selection_button = gtk.Button('Grow')
    widget.show()
    widget.connect('clicked', self.growSelectionButtonClicked)
    container[-1].add(widget)
    self.selection_interface.append(widget)

    widget = self.shrink_selection_button = gtk.Button('Shrink')
    widget.show()
    widget.connect('clicked', self.shrinkSelectionButtonClicked)
    container[-1].add(widget)
    self.selection_interface.append(widget)

    container.pop()

    container.pop()

    container.pop()

    widget = gtk.HSeparator()
    widget.show()
    container[-1].add(widget)

    widget = gtk.Expander('Super Pixel Helper')
    widget.show()
    widget.set_expanded(False)
    widget.set_resize_mode(gtk.RESIZE_PARENT)
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.Label('# of regions')
    widget.show()
    container[-1].add(widget)

    widget = self.slic_n = gtk.Entry()
    widget.show()
    widget.set_text('100')
    widget.set_sensitive(slic_available)
    container[-1].add(widget)
    self.slic_interface.append(widget)

    container.pop()

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.Label('Compactness')
    widget.show()
    container[-1].add(widget)

    widget = self.slic_compactness = gtk.Entry()
    widget.show()
    widget.set_text('1.0')
    widget.set_sensitive(slic_available)
    container[-1].add(widget)
    self.slic_interface.append(widget)

    container.pop()

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.Label('Smoothing Sigma')
    widget.show()
    container[-1].add(widget)

    widget = self.slic_sigma = gtk.Entry()
    widget.show()
    widget.set_text('0.0')
    widget.set_sensitive(slic_available)
    container[-1].add(widget)
    self.slic_interface.append(widget)

    container.pop()

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.Label('Colorspace')
    widget.show()
    container[-1].add(widget)

    widget = self.slic_lab = gtk.ToggleButton('Using LAB')
    widget.show()
    widget.set_active(True)
    widget.set_sensitive(slic_available)
    widget.connect('toggled', self.slicColorSpaceButtonToggled)
    container[-1].add(widget)
    self.slic_interface.append(widget)

    container.pop()

    container.pop()

    widget = gtk.HBox(spacing=4, homogeneous=True)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    widget = self.create_segmentation_button = gtk.Button('Create')
    widget.show()
    widget.set_sensitive(slic_available)
    widget.connect('clicked', self.createSegmentationButtonClicked)
    container[-1].add(widget)
    self.slic_interface.append(widget)

    widget = self.clear_all_segmentations_button = gtk.Button('Clear All')
    widget.show()
    widget.set_sensitive(slic_available)
    widget.connect('clicked', self.clearAllSegmentationsButtonClicked)
    container[-1].add(widget)
    self.slic_interface.append(widget)

    container.pop()

    container.pop()

    container.pop()

    widget = gtk.HSeparator()
    widget.show()
    container[-1].add(widget)

    widget = gtk.Expander('Shortcuts Reference')
    widget.show()
    widget.set_expanded(False)
    widget.set_resize_mode(gtk.RESIZE_PARENT)
    container[-1].add(widget)
    container.append(widget)

    widget = gtk.VBox(spacing=4, homogeneous=False)
    widget.show()
    container[-1].add(widget)
    container.append(widget)

    shortcuts = [
      'Keyboard:',
      '  N: Pencil',
      '  Shift+E: Eraser',
      '  O: Color Picker',
      '  U: Fuzzy Select (Color Wand)',
      '  F: Poly/Free Select',
      '  1, 2, 3, 4: Zoom',
      '  Selection+Shift: Union',
      '  Selection+Ctrl: Subtract',
      '  Selection+Ctrl+Shift: Intersection',
      '',
      'Hints:',
      '  Avoid all anti-aliasing',
      '  Hold Ctrl with Pencil to color pick',
      '  Hold Shift with Pencil for lines',
      '  Use [ and ] to change brush size',
      '  Use middle click to pan',
      '  When focused, hold Space to pan',
      '  Use Pencil instead of Brush',
      '  Turn on Hard Edge for Eraser',
    ]

    widget = gtk.Label('\n'.join(shortcuts))
    widget.set_alignment(0.0, 0.0)
    widget.justify = gtk.JUSTIFY_LEFT
    widget.show()
    container[-1].add(widget)

    container.pop()

    container.pop()

    container.pop()

    self.show()
    self.resize(216, 700)

    gobject.timeout_add(100, self.update, self)  

    return window
  
  #### METHODS ####

  def alertDialog(self, message, logger=None):
    '''
    Produces a standard GTK, non-GIMP, modal alert dialog box with the provided
    message and also sends the error as a message to GIMP. This will also
    automatically log with the provided logging function.
    '''
    gimp.message(message)
    if logger:
      logger(message)
    alert = gtk.MessageDialog(self, gtk.DIALOG_DESTROY_WITH_PARENT, gtk.MESSAGE_ERROR, gtk.BUTTONS_CLOSE, message)
    alert.run()
    alert.destroy()

  def updateImagePaths(self, image_full_path, update_image_list=True):
    self.image_full_path = image_full_path
    self.working_path, self.image_filename = os.path.split(self.image_full_path)
    self.image_name, self.image_extension = os.path.splitext(self.image_filename)
    if update_image_list:
      self.image_list = sorted(filter(lambda x: x.endswith('.jpg'), os.listdir(self.working_path)))
      self.image_index = self.image_list.index(self.image_filename)
    self.image_name_box.set_text('%s' % self.image_name)
    self.jump_to_box.set_text('%d' % (self.image_index+1))

  def jumpImage(self, offset):
    self.image_index = ( self.image_index + offset + len(self.image_list)) % len(self.image_list)
    logging.info('Jumping image, offset=%d, to index=%d.' % (offset, self.image_index))
    self.image_filename = self.image_list[self.image_index]
    self.image_full_path = os.path.join(self.working_path, self.image_filename)
    self.image_name, self.image_extension = os.path.splitext(self.image_filename)
    self.image_name_box.set_text('%s' % self.image_name)
    self.jump_to_box.set_text('%d' % (self.image_index+1))

  def loadMetaData(self):
    label_map_filename = os.path.join(self.working_path, self.map_relative_path, 'map.txt')
    logging.info('Loading integer to name mapping from "%s"' % label_map_filename)
    self.liststore.clear()
    self.label_int_to_name_map = {}
    self.label_name_to_int_map = {}
    try:
      with open(label_map_filename, 'r') as f:
        for line in f.readlines():
          num, name = line[:-1].split(': ')
          self.liststore.append([name])
          self.label_int_to_name_map[int(num)] = name
          self.label_name_to_int_map[name] = int(num)
      logging.debug('Integer to name mapping:\n%s' % pprint.pformat(self.label_int_to_name_map))
      self.label_name_to_int_map['empty'] = 0
      self.label_int_to_name_map[0] = 'empty'
    except:
      self.alertDialog('Failed to load integer to name mapping from "%s"!' % label_map_filename, logging.error)

  def loadImage(self):
    # get paths
    # get original image file names
    original_filename = os.path.join(self.working_path, self.image_filename)
    logging.info('Loading original image from "%s"' % original_filename)
    # load the original image
    try:
      original_image = pdb.gimp_file_load(original_filename, original_filename)
    except:
      self.alertDialog('Could not load file "{0}".'.format(original_filename), logging.error)
      self.is_image_open = False
      self.resetInterface()
      return
    # clear layers
    while len(self.image.layers) > 0:
      pdb.gimp_image_remove_layer(self.image, self.image.layers[0])
    # resize image to fit original image
    pdb.gimp_image_resize(self.image, original_image.width, original_image.height, 0, 0)
    # insert an empty layer
    empty_layer = pdb.gimp_layer_new(self.image, self.image.width, self.image.height, gimpenums.RGBA_IMAGE, 'Original', 100, gimpenums.NORMAL_MODE)
    pdb.gimp_image_insert_layer(self.image, empty_layer, None, 0)
    # copy from the original image
    pdb.gimp_edit_copy(original_image.layers[0])
    # paste original image as a floating selection over the "background"
    background = self.image.layers[0]
    floating_selection = pdb.gimp_edit_paste(background, True)
    # convert floating selection to a layer
    pdb.gimp_floating_sel_to_layer(floating_selection)
    # align the layer with the image
    new_layer = self.image.layers[0]
    new_layer.set_offsets(0, 0)
    # merge the new layer to the "background"
    pdb.gimp_selection_none(self.image)
    pdb.gimp_image_merge_down(self.image, new_layer, 0)
    # update the new "background" layer
    self.original_layer = self.image.layers[0]
    self.original_layer.name = 'Original'
    self.original_layer.merge_shadow(True)
    pdb.gimp_layer_resize_to_image_size(self.original_layer)
    self.original_layer.update(0, 0, self.image.width, self.image.height)
    self.original_layer.flush()
    # delete original image
    pdb.gimp_image_delete(original_image)
    # update gimp
    pdb.gimp_displays_flush()
    self.is_image_open = True

  def loadLabelMat(self):
    # get paths
    try:
      # TODO: add alert to confirm discard of unsaved layers.
      mat_filename = os.path.join(self.working_path, self.label_relative_path, self.image_name+'.mat')
      logging.info('Loading label file from "%s"' % mat_filename)
      # Load the `.mat` file as a dictionary of variables.
      mat_contents = loadmat(mat_filename)
    except:
      logging.warn('Could not find label file %s, starting with empty label.' % mat_filename)
      self.mat_hierarchy = []
      self.mat_hierarchy.append(['Original', []])
      self.mat_leftover_contents = {}
      pdb.gimp_displays_flush()
    else:
      logging.debug('Contents of .mat file:\n%s' % pprint.pformat(mat_contents))

      # Create a copy of the variables inside the `.mat` file so that we can
      # preserve non-`Label*` and non-`Hierarchy` variables when we save the
      # `.mat` file back.
      keys = list(filter(lambda x: not x.startswith('__'), mat_contents.keys()))
      logging.debug('List of variables in .mat file:\n%s' % pprint.pformat(keys))

      # Try to load the hierarchy if it exists in the `.mat` file.
      if 'Hierarchy' in keys:

        # If it exists it should be in the form of a Matlab string which can be
        # turned into a cell array tree structure by using `eval()` in Matlab.
        # We can convert this into a Python list-style tree structure by
        # replace the braces with square brackets and evaluating it as a Python
        # literal using `ast.literal_eval`.
        # 
        # NOTE (Viet): As a design note I initially wanted to save and read the
        # cell array directly but the way `scipy.io` reads and writes cell
        # arrays is very unwieldy. Eventually I settled on this method which is
        # nice and simple but perhaps susceptible to bugs. Time will tell.
        # 
        # TODO: Make this conversion more robust.
        logging.info('Found "Hierarchy" variable in .mat file. Parsing saved hierarchy...')
        matlab_hierarchy_expression = mat_contents['Hierarchy'][0]
        logging.debug('Hierarchy expression in .mat file: %s' % matlab_hierarchy_expression)
        translation_table = string.maketrans('{}', '[]')
        python_hierarchy_expression = str(matlab_hierarchy_expression).translate(translation_table)
        logging.debug('Translated Python hierarchy expression: %s' % python_hierarchy_expression)
        self.mat_hierarchy = ast.literal_eval(python_hierarchy_expression)

      # If it doesn't exists then we'll load them flatly in alphabetical order..
      else:
        logging.info('Did not find "Hierarchy" variable in .mat file. Loading all label layers in flat, sorted hierarchy...')
        self.mat_hierarchy = [[x, []] for x in list(sorted(filter(lambda x: x.startswith('Label'), mat_contents.keys())))]
        self.mat_hierarchy.append(['Original', []])

      logging.info('Hierarchy after loading .mat file:\n%s' % pprint.pformat(self.mat_hierarchy))

      def dataFunc(node, parent_data):
        index, (data, children) = node
        layer_name = data
        parent_layer = parent_data

        node_is_a_group_layer = layer_name not in keys
        if layer_name == 'Original':
          logging.info('Encountered "Original" layer, repositioning existing "Original" layer accordingly')
          layer = self.original_layer
          pdb.gimp_image_reorder_item(self.image, layer, parent_layer, index)
        elif node_is_a_group_layer:
          logging.info('Encountered GroupLayer "%s" (e.g. layer without associated variable), creating group layer' % layer_name)
          # Create a new group layer.
          layer = pdb.gimp_layer_group_new(self.image)
          # Set the name of the layer.
          layer.name = layer_name
          # Finally put the layer in the right place.
          pdb.gimp_image_insert_layer(self.image, layer, parent_layer, index)
        else:
          logging.info('Encountered Layer "%s" (e.g. layer with associated variable), creating new layer and loading layer from .mat file' % layer_name)
          # Create a new layer.
          layer = pdb.gimp_layer_new(self.image, self.image.width, self.image.height, gimpenums.RGBA_IMAGE, layer_name, 100, gimpenums.NORMAL_MODE)
          # Insert the new layer into the image at the right location in the hierarchy.
          pdb.gimp_image_insert_layer(self.image, layer, parent_layer, index)
          # Grab integer label image from MAT contents.
          # NOTE (Viet): The variable `mat_contents` here should be accessible
          # to the function via the closure.
          int_label_image = mat_contents[layer_name]
          # Convert integer label image to RGB label image.
          rgb_label_image = self.intLabelImageToRgbLabelImage(int_label_image)
          # Push the RGB label image to new layer.
          self.rgbLabelImageToLayer(rgb_label_image, layer)

        # Remove this variable name from the list of keys so that we can have
        # a list of leftover variables by the end (to preserve).
        #
        # NOTE (Viet): The variable `keys` here should be accessible to the
        # function via the closure.
        if node in keys:
          keys.remove(node)

        return layer

      childrenFunc = lambda x: enumerate(x[1][1])

      logging.info('Recreating layer hierarchy...')
      [preorderRecurse(root_layer, None, dataFunc, childrenFunc) for root_layer in enumerate(self.mat_hierarchy)]

      # Store a dictionary of left over variables so that they may be written
      # to the MAT file later when saved.
      self.mat_leftover_contents = {}
      for key in keys:
        self.mat_leftover_contents[key] = mat_contents[key]
      logging.info('Leftover variables from .mat file: %s' % keys)

      # Update GIMP's display.
      pdb.gimp_displays_flush()

  def saveLabelMat(self):
    # get paths
    mat_filename = os.path.join(self.working_path, self.label_relative_path, self.image_name+'.mat')
    logging.info('Saving label as "%s".' % mat_filename)
    gimp.progress_init('Saving labels as "{0}"...'.format(mat_filename))
    # Create the workspace for the MAT file and prefill it with the left over
    # variables from the previous MAT file.
    mat_contents = {}
    mat_contents.update(self.mat_leftover_contents)
    #logging.debug('Leftover contents being added to .mat file:\n%s' % pprint.pformat(mat_contents))
    # Get the layer hierarchy.
    current_layer_hierarchy = layerHierarchyFromImage(self.image)
    logging.debug('State of layer hierarchy during label save:\n%s' % pprint.pformat(current_layer_hierarchy))
    # Create a flat list of layer names.
    flat_layer_list = []
    def dataFunc(node, parent_data):
      data, children = node
      layer_name, layer_id, layer = data
      flat_layer_list.append(layer)
      return str(layer_name)
    childrenFunc = lambda x: x[1]
    processed_layer_hierarchy = [preorderRecurse(root_layer, None, dataFunc, childrenFunc) for root_layer in current_layer_hierarchy]
    logging.debug('Flat layer list:\n%s' % pprint.pformat(flat_layer_list))
    logging.debug('Processed layer hierarchy:\n%s' % pprint.pformat(processed_layer_hierarchy))
    # Store the layer hierarchy.
    python_hierarchy_expression = repr(processed_layer_hierarchy)
    logging.debug('Python hierarchy string: %s' % python_hierarchy_expression)
    translation_table = string.maketrans('[]()"', '{}{}\'')
    mat_contents['Hierarchy'] = python_hierarchy_expression.translate(translation_table)
    logging.debug('Matlab hierarchy string: %s' % mat_contents['Hierarchy'])
    # Store each label layer flatly in the root workspace of the `.mat` dictionary.
    logging.info('Saving individual layers...')
    for layer in flat_layer_list:
      if type(layer) == gimp.GroupLayer:
        logging.info('Skipping layer "%s" because it is a GroupLayer' % layer)
        continue
      if layer.name == 'Original':
        logging.info('Skipping layer original layer')
        continue
      #if not layer.name.startswith('Label'):
      #  logging.info('Skipping layer "%s" because its name does not start with "Label"' % layer)
      #  continue
      logging.info('Saving layer "%s"' % layer)
      layer.resize_to_image_size()
      rgb_label_image = self.layerToRgbLabelImage(layer)
      int_label_image = self.rgbLabelImageToIntLabelImage(rgb_label_image)
      mat_contents[layer.name] = int_label_image
    # Make a backup of the `.mat` file.
    if os.path.exists(mat_filename):
      shutil.copyfile(mat_filename, mat_filename+'.old')
    # Save the actual `.mat` file.
    #logging.debug('Final .mat dictionary prior to saving:\n%s' % pprint.pformat(mat_contents))
    savemat(mat_filename, mat_contents, do_compression=True)
    # Update the GIMP interface.
    gimp.progress_update(100)
    pdb.gimp_progress_set_text('Saved labels as "{0}"!'.format(mat_filename))
    pdb.gimp_progress_end()
    return True
    # Should have it return False on any error or incomplete save.

  def intLabelImageToRgbLabelImage(self, int_label_image):
    return self.colormap[self.shufflemap[int_label_image]]

  def rgbLabelImageToIntLabelImage(self, rgb_label_image):
    unknown_color_encountered = False
    tmp_image = np.array(rgb_label_image, dtype='uint32')
    rgbint_label_image = tmp_image[:, :, 0]*256*256 + tmp_image[:, :, 1]*256 + tmp_image[:, :, 2]
    uniq_labels = np.unique(rgbint_label_image)
    int_label_image = np.zeros(rgbint_label_image.shape, dtype='uint32')
    for label in uniq_labels:
      try:
        int_label_image[rgbint_label_image == label] = self.int_reversemap[label]
      except KeyError:
        logging.error('Could not find %d in int_reversemap.' % label)
        int_label_image[rgbint_label_image == label] = 0
        unknown_color_encountered = True
    if unknown_color_encountered:
      self.alertDialog('An unknown color was found in the label image. This most likely occured some transparent area has some unknown color. In this case the transparent area will automatically be assigned the label "empty". Another possibility is because some operation were performed with anti-aliasing or the foreground color was not updated after a shuffle. In this case data corruption has occured and data may have been lost or corrupted. Please back up the label file before attempting to save.')
    return int_label_image

  def rgbLabelImageToLayer(self, rgb_label_image, label_layer):
    pdb.gimp_selection_none(self.image)
    pixel_region = label_layer.get_pixel_rgn(0, 0,
                                             self.image.width, self.image.height,
                                             True, True)
    rgba_label_image = np.dstack((rgb_label_image,
                                  (np.sum(rgb_label_image, axis=2) > 0 ) * 255))
                                  #np.ones((self.image.height, self.image.width),
                                  #        dtype='uint8') * 255))
    pixel_region[0:self.image.width, 0:self.image.height] = array.array('B', rgba_label_image.ravel()).tostring()
    label_layer.merge_shadow(True)
    label_layer.update(0, 0, self.image.width, self.image.height)
    #label_layer.flush()

  def layerToRgbLabelImage(self, label_layer):
    pdb.gimp_selection_none(self.image)
    pixel_region = label_layer.get_pixel_rgn(0, 0,
                                             self.image.width, self.image.height,
                                             False, False)
    byte_array = array.array('B', pixel_region[0:self.image.width, 0:self.image.height])
    byte_array = np.array(byte_array, dtype='uint8')
    byte_array = byte_array.reshape(len(byte_array)/4, 4)
    # NOTE THE SWITCH IN INDEX ORDER
    rgba_label_image = byte_array.reshape(self.image.height, self.image.width, 4)
    # Convert all transparent areas to black (0, 0, 0) which is hard coded to
    # be the "empty" label.
    rgba_label_image[rgba_label_image[:, :, 3] == 0] = 0
    return rgba_label_image[:, :, :3]

  def shuffle(self):
    all_but_zero = self.shufflemap[1:]
    np.random.shuffle(all_but_zero)
    self.shufflemap[1:] = all_but_zero
    self.tuple_reversemap = {}
    self.int_reversemap = {}
    tuple_to_int = lambda x: x[0]*256*256+x[1]*256+x[2]
    for i in range(self.num_of_labels):
      rgb_tuple = tuple(self.colormap[self.shufflemap[i]])
      self.tuple_reversemap[rgb_tuple] = i
      self.int_reversemap[tuple_to_int(rgb_tuple)] = i
    self.tuple_reversemap[(0, 0, 0)] = 0
    self.int_reversemap[0] = 0

  def setForegroundColorFromLabelName(self):
    label_name = self.label_name.get_text()
    if self.label_name_to_int_map.has_key(label_name):
      color = self.colormap[self.shufflemap[self.label_name_to_int_map[label_name]]]
      gimp.set_foreground(tuple(map(lambda x: int(x), color)))

  def loadComment(self):
    comment_filename = os.path.join(self.working_path, self.comment_relative_path, self.image_name+'.txt')
    try:
      with open(comment_filename, 'r') as f:
        self.comment.get_buffer().set_text(f.read())
    except IOError:
      pass
  
  def resetInterface(self):
    if self.is_image_open:
      self.label_opacity_slider.set_value(100.0)

  def updateInterface(self):

    # update the selection helper interface state based on whether there is a
    # selection or not
    is_selection_active = not pdb.gimp_selection_is_empty(self.image)
    for widget in self.selection_interface:
      widget.set_sensitive(is_selection_active)

    if self.is_image_open:

      # enable certain interface elements only if there is an image "open"
      for widget in self.only_available_with_open_image:
        widget.set_sensitive(True)
      for widget in self.slic_interface:
        widget.set_sensitive(slic_available)

      self.updateLayerList()

      # update the label of the selected foreground color
      foreground_color = tuple(gimp.get_foreground())[:3]
      if foreground_color != self.last_foreground_color:
        self.last_foreground_color = foreground_color
        # NOTE (Viet): There can actually be two key errors here: one for
        # `tuple_reversemap` and one for `label_int_to_name_map`. Instead of two
        # cascaded key checks I thought an exception catch here would be
        # cleaner.
        try:
          foreground_name = self.label_int_to_name_map[self.tuple_reversemap[foreground_color]]
          self.current_label.set_text(foreground_name)
        except KeyError:
          self.current_label.set_text('{0} not found'.format(str(foreground_color)))

    else:
      for widget in self.only_available_with_open_image:
        widget.set_sensitive(False)
      for widget in self.slic_interface:
        widget.set_sensitive(False)

  def updateLayerList(self):
    # Get the current hierarchy and compare it with the previous one.
    current_layer_hierarchy = layerHierarchyFromImage(self.image)
    current_layer_hierarchy = cleanLayerHierarchyNames(current_layer_hierarchy)
    layer_hierarchy_changed = not layerHierarchiesEqual(current_layer_hierarchy, self.last_layer_hierarchy)

    if layer_hierarchy_changed:
      # Gather a list of selected layer IDs (second column, index `1`).
      layer_selection = []
      tree_store, rows = self.layer_list_selection.get_selected_rows()
      for row in rows:
        layer_selection.append(tree_store[row][1])
      # Clear the layer list store.
      self.layer_list_store.clear()
      # Set up hierarchy recursion to add layers as they're traversed in
      # pre-order and to update the `layer_selection` list as it goes by
      # removing the ID and replacing it with the actual store entry
      # (`TreeIter`) which can be used to re-select the previous selection.
      def dataFunc(node, parent_data):
        data, children = node
        parent_store_entry = parent_data
        layer_name, layer_id, layer = data
        store_entry = self.layer_list_store.append(parent_store_entry, [layer_name, layer_id])
        if layer_id in layer_selection:
          layer_selection.remove(layer_id)
          layer_selection.append(store_entry)
        return store_entry
      childrenFunc = lambda x: x[1]
      # Rebuild the list store. This will also replace layer IDs in
      # `layer_selection` with the respective TreeIter.
      [preorderRecurse(root_layer, None, dataFunc, childrenFunc) for root_layer in current_layer_hierarchy]
      # Expand the tree (because collapsing it will unselect children and
      # thus potentially cause the original selection to be lost) and select
      # all of those rows again.
      self.layer_list.expand_all()
      if len(layer_selection) > 0:
        for tree_iter in layer_selection:
          if type(tree_iter) == gtk.TreeIter:
            self.layer_list_selection.select_iter(tree_iter)

    # Remember the hierarchy so we can compare with it later.
    self.last_layer_hierarchy = current_layer_hierarchy

  def selectLabelLayers(self):
    self.layer_list_selection.select_all()
    tree_store, rows = self.layer_list_selection.get_selected_rows()
    for row in rows:
      layer_name, layer_id = tree_store[row]
      if not layer_name.startswith('Label'):
        self.layer_list_selection.unselect_path(row)

  def applyToSelectedLayers(self, f):
    tree_store, rows = self.layer_list_selection.get_selected_rows()
    return [f(pdb.gimp_image_get_layer_by_name(self.image, tree_store[row][0])) for row in rows]

  def removeAllLayers(self):
    '''
    Removes all layers.
    '''
    while len(self.image.layers) > 0:
      pdb.gimp_image_remove_layer(self.image, self.image.layers[0])

  #### GUI CALLBACKS ####

  def saveUnsavedChangesDialog(self):
    if pdb.gimp_image_is_dirty(self.image):
      dialog = gtk.Dialog('Unsaved Changes Detected', None, gtk.DIALOG_MODAL, (gtk.STOCK_OK, gtk.RESPONSE_OK, gtk.STOCK_NO, gtk.RESPONSE_NO, gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL))
      label = gtk.Label('Save changes before opening another image?')
      dialog.vbox.pack_start(label, padding=4)
      label.show()
      dialog.set_default_response(gtk.RESPONSE_CANCEL)
      response = dialog.run()
      dialog.destroy()
      if response == gtk.RESPONSE_CANCEL:
        return False
      elif response == gtk.RESPONSE_OK:
        if not self.saveLabelMat():
          return False
    return True

  def loadImageFromInternalPaths(self):
    pdb.gimp_image_undo_disable(self.image)
    self.loadMetaData()
    self.loadImage()
    self.loadLabelMat()
    self.loadComment()
    self.updateLayerList()
    self.selectLabelLayers()
    pdb.gimp_image_undo_enable(self.image)
    pdb.gimp_image_clean_all(self.image);

  def openImageButtonClicked(self, widget):
    logging.info('Button clicked')
    if not self.saveUnsavedChangesDialog():
      return
    dialog = gtk.FileChooserDialog(
        'Open Image...',
        None, 
        gtk.FILE_CHOOSER_ACTION_OPEN,
        (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL, gtk.STOCK_OPEN, gtk.RESPONSE_OK))
    dialog.set_default_response(gtk.RESPONSE_OK)
    response = dialog.run()
    if response == gtk.RESPONSE_OK:
      self.updateImagePaths(dialog.get_filename())
      self.loadImageFromInternalPaths()
    dialog.destroy()

  def openSetButtonClicked(self, widget):
    logging.info('Button clicked')
    if not self.saveUnsavedChangesDialog():
      return
    dialog = gtk.FileChooserDialog(
        'Open Set...',
        None, 
        gtk.FILE_CHOOSER_ACTION_OPEN,
        (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL, gtk.STOCK_OPEN, gtk.RESPONSE_OK))
    dialog.set_default_response(gtk.RESPONSE_OK)
    response = dialog.run()
    if response == gtk.RESPONSE_OK:
      self.set_path = dialog.get_filename()
      # Custom update_image_paths
      set_folder, set_filename = os.path.split(self.set_path)
      # Populate image list
      self.image_list = []
      self.image_index = 0
      with open(self.set_path, 'r') as f:
        for line in f:
          match = re.search(r'([0-9]{4}_[0-9]{6})', line)
          if match:
            image_name = match.group(0)
            self.image_list.append(image_name+'.jpg')
      if len(self.image_list) == 0:
        self.alertDialog('Image set list produced an empty list!')
        dialog.destroy()
        return
      # Open first image in set
      self.updateImagePaths(os.path.join(set_folder, '..', 'image', self.image_list[self.image_index]), False)
      self.loadImageFromInternalPaths()
    dialog.destroy()

  def saveLabelMatButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    self.saveLabelMat()
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_image_clean_all(self.image);
    
  def jumpImageButtonClicked(self, widget):
    logging.info('Button clicked')
    if not self.saveUnsavedChangesDialog():
      return
    try:
      dest_image_idx = int(self.jump_to_box.get_text())-1
      self.image_list[dest_image_idx]
    except ValueError:
      self.alertDialog('Could not convert %s into an integer.' % self.jump_to_box.get_text(), logging.error)
      return
    except IndexError:
      self.alertDialog('Image does not exist at index %d.' % (dest_image_idx+1), logging.error)
      return
    self.jumpImage(dest_image_idx - self.image_index)
    self.loadImageFromInternalPaths()

  def previousImageButtonClicked(self, widget):
    logging.info('Button clicked')
    if not self.saveUnsavedChangesDialog():
      return
    self.jumpImage(-1)
    self.loadImageFromInternalPaths()

  def nextImageButtonClicked(self, widget):
    logging.info('Button clicked')
    if not self.saveUnsavedChangesDialog():
      return
    self.jumpImage(+1)
    self.loadImageFromInternalPaths()

  def completionMatchSelected(self, completion, model, iterator):
    logging.info('Label name completion selected: %s' % self.label_name.get_text())
    self.setForegroundColorFromLabelName()

  def labelNameActivated(self, widget):
    logging.info('Label name completion selected: %s' % self.label_name.get_text())
    self.setForegroundColorFromLabelName()

  def selectLabelButtonClicked(self, widget):
    logging.info('Button clicked, label: %s' % self.label_name.get_text())
    self.setForegroundColorFromLabelName()

  def shuffleColorsButtonClicked(self, widget):
    logging.info('Button clicked')
    dialog = gtk.Dialog('Shuffle Colors?', None, gtk.DIALOG_MODAL, (gtk.STOCK_OK, gtk.RESPONSE_OK, gtk.STOCK_NO, gtk.RESPONSE_NO))
    label = gtk.Label('Are you sure you want to shuffle colors?\nThe undo history will be cleared.')
    dialog.vbox.pack_start(label, padding=4)
    label.show()
    dialog.set_default_response(gtk.RESPONSE_NO)
    response = dialog.run()
    dialog.destroy()
    if response == gtk.RESPONSE_NO:
      return
    pdb.gimp_image_undo_disable(self.image)
    temp_int_label_images_store = {}
    def storeIntLabelImages(layer):
      if not pdb.gimp_item_is_group(layer):
        layer.resize_to_image_size()
        rgb_label_image = self.layerToRgbLabelImage(layer)
        int_label_image = self.rgbLabelImageToIntLabelImage(rgb_label_image)
        temp_int_label_images_store [layer.name] = int_label_image
    def restoreRgbLabelImages(layer):
      if (not pdb.gimp_item_is_group(layer)) and temp_int_label_images_store.has_key(layer.name):
        int_label_image = temp_int_label_images_store[layer.name]
        rgb_label_image = self.intLabelImageToRgbLabelImage(int_label_image)
        self.rgbLabelImageToLayer(rgb_label_image, layer)
    self.selectLabelLayers()
    self.applyToSelectedLayers(storeIntLabelImages)
    self.shuffle()
    self.applyToSelectedLayers(restoreRgbLabelImages)
    pdb.gimp_image_undo_enable(self.image)
    pdb.gimp_displays_flush()

  def layersSelectAllButtonClicked(self, widget):
    logging.info('Button clicked')
    self.layer_list_selection.select_all()

  def layersSelectNoneButtonClicked(self, widget):
    logging.info('Button clicked')
    self.layer_list_selection.unselect_all()

  def layersSelectLabelsButtonClicked(self, widget):
    logging.info('Button clicked')
    self.selectLabelLayers()

  def layersInvertLabelSelectionButtonClicked(self, widget):
    logging.info('Button clicked')
    tree_store, rows = self.layer_list_selection.get_selected_rows()
    selected_names = [tree_store[row][0] for row in rows]
    self.layer_list_selection.select_all()
    tree_store, rows = self.layer_list_selection.get_selected_rows()
    for row in rows:
      layer_name, layer_id = tree_store[row]
      if layer_name in selected_names:
        self.layer_list_selection.unselect_path(row)

  def labelOpacitySliderChange(self, widget, scroll, value):
    logging.info('Opacity slider changed')
    pdb.gimp_image_undo_group_start(self.image)
    def updateOpacity(layer):
      layer.opacity = min(100.0, max(0.0, value))
      layer.flush()
    self.applyToSelectedLayers(updateOpacity)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def toggleLabelButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    selected_layer_visibility = self.applyToSelectedLayers(lambda x: x.visible)
    if len(selected_layer_visibility) == 0:
      return
    mixed_visibility = max(selected_layer_visibility) != min(selected_layer_visibility)
    target_visibility = mixed_visibility or (not selected_layer_visibility[0])
    def toggleLayerVisibility(layer):
      layer.visible = target_visibility
      if layer.visible:
        pdb.gimp_image_set_active_layer(self.image, layer)
      else:
        pdb.gimp_image_set_active_layer(self.image, self.original_layer)
      layer.flush()
    self.applyToSelectedLayers(toggleLayerVisibility)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def normalBlendButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    def setLayerMode(layer):
      layer.mode = gimpenums.NORMAL_MODE
      layer.flush()
    self.applyToSelectedLayers(setLayerMode)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def grainBlendButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    def setLayerMode(layer):
      layer.mode = gimpenums.GRAIN_MERGE_MODE
      layer.flush()
    self.applyToSelectedLayers(setLayerMode)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def colorBlendButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    def setLayerMode(layer):
      layer.mode = gimpenums.COLOR_MODE
      layer.flush()
    self.applyToSelectedLayers(setLayerMode)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def labelSelectionButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    layer = pdb.gimp_image_get_active_layer(self.image)
    pdb.gimp_selection_sharpen(self.image)
    pdb.gimp_edit_fill(layer, gimpenums.FOREGROUND_FILL)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_selection_none(self.image)
    layer.flush()
    pdb.gimp_displays_flush()

  def labelDeleteButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    layer = pdb.gimp_image_get_active_layer(self.image)
    pdb.gimp_selection_sharpen(self.image)
    pdb.gimp_edit_clear(layer)
    pdb.gimp_image_undo_group_end(self.image)
    layer.flush()
    pdb.gimp_displays_flush()

  def layerAlphaSelectionButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    CHANNEL_OP_REPLACE = 2
    pdb.gimp_image_select_item(self.image, CHANNEL_OP_REPLACE, pdb.gimp_image_get_active_layer(self.image))
    pdb.gimp_selection_sharpen(self.image)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def clearSelectionButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_selection_none(self.image)
    pdb.gimp_displays_flush()

  def hardenSelectionButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_selection_sharpen(self.image)
    pdb.gimp_displays_flush()

  def smoothSelectionButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    pdb.gimp_selection_grow(self.image, 3)
    pdb.gimp_selection_shrink(self.image, 3)
    pdb.gimp_selection_sharpen(self.image)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def invertSelectionButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    pdb.gimp_selection_invert(self.image)
    pdb.gimp_selection_sharpen(self.image)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def growSelectionButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    pdb.gimp_selection_grow(self.image, 1)
    pdb.gimp_selection_sharpen(self.image)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def shrinkSelectionButtonClicked(self, widget):
    logging.info('Button clicked')
    pdb.gimp_image_undo_group_start(self.image)
    pdb.gimp_selection_shrink(self.image, 1)
    pdb.gimp_selection_sharpen(self.image)
    pdb.gimp_image_undo_group_end(self.image)
    pdb.gimp_displays_flush()

  def slicColorSpaceButtonToggled(self, widget):
    logging.info('Button clicked')
    widget.set_label('Using LAB' if widget.get_active() else 'Using RGB')

  def createSegmentationButtonClicked(self, widget):
    logging.info('Button clicked')
    try:
      slic_n = max(2, min(self.num_of_labels, int(self.slic_n.get_text())))
    except ValueError:
      self.alertDialog('# of regions is not a valid integer.')
    try:
      slic_sigma = float(self.slic_sigma.get_text())
      if slic_sigma < 0.0:
        raise ValueError()
    except ValueError:
      self.alertDialog('Smooth sigma is not a valid, non-negative floating point number.')
    try:
      slic_compactness = float(self.slic_compactness.get_text())
      if slic_compactness <= 0.0:
        raise ValueError()
    except ValueError:
      self.alertDialog('Compactness is not a valid, positive, non-zero floating point number.')
    slic_lab = self.slic_lab.get_active()
    pixel_region = self.original_layer.get_pixel_rgn(0, 0,
                                                     self.image.width, self.image.height,
                                                     False, False)
    byte_array = array.array('B', pixel_region[0:self.image.width, 0:self.image.height])
    byte_array = np.array(byte_array, dtype='uint8')
    byte_array = byte_array.reshape(len(byte_array)/4, 4)
    rgba_original_image = byte_array.reshape(self.image.height, self.image.width, 4)
    rgb_original_image = rgba_original_image[:, :, :3]
    segments_int_image = slic(rgb_original_image,
                              n_segments=slic_n,
                              compactness=slic_compactness,
                              sigma=slic_sigma,
                              convert2lab=slic_lab)
    # turn into rgb image
    segments_rgb_image = self.colormap[self.shufflemap[segments_int_image]]
    # create a new label layer
    new_layer = pdb.gimp_layer_new(self.image, self.image.width, self.image.height, gimpenums.RGBA_IMAGE, 'Superpixel Helper', 100, gimpenums.NORMAL_MODE)
    pdb.gimp_image_insert_layer(self.image, new_layer, None, 0)
    pixel_region = new_layer.get_pixel_rgn(0, 0,
                                           self.image.width, self.image.height,
                                           True, True)
    segments_rgba_image = np.dstack((segments_rgb_image,
                                     np.ones((self.image.height, self.image.width),
                                             dtype='uint8') * 255))
    pixel_region[0:self.image.width, 0:self.image.height] = array.array('B', segments_rgba_image.ravel()).tostring()
    new_layer.merge_shadow(True)
    new_layer.update(0, 0, self.image.width, self.image.height)
    new_layer.flush()
    pdb.gimp_displays_flush()

  def clearAllSegmentationsButtonClicked(self, widget):
    logging.info('Button clicked')
    segmentation_layer = None
    while True:
      for layer in self.image.layers:
        if layer.name.startswith('Superpixel Helper'):
          segmentation_layer = layer
          break
      if segmentation_layer == None:
        break
      else:
        pdb.gimp_image_remove_layer(self.image, segmentation_layer)
        segmentation_layer = None
    pdb.gimp_displays_flush()

  def commentChanged(self, buffer):
    logging.info('Comment changed')
    comment_filename = os.path.join(self.working_path, self.comment_relative_path, self.image_name+'.txt')
    with open(comment_filename, 'w') as f:
      f.write(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter()))

  def update(self, *args):
    self.updateInterface()
    gobject.timeout_add(200, self.update, self)

def toolboxMain(image, drawable):
  gimp.message('Log files being saved in %s' % log_dir)
  window = LabelToolbox(image)
  if not imports_succeeded:
    window.alertDialog('This plugin requires NumPy and SciPy. For segmentation, Scikit-Image is required.')
    window.destroy()
  else:
    gtk.main()

gimpfu.register(
     'label_toolbox',
     'Toolbox for labeling images.',
     'Toolbox for labeling images.',
     'Viet Nguyen',
     'Viet Nguyen',
     '2014',
     '<Image>/Toolbox/Labeling',
     '*',
     [],
     [],
     toolboxMain)

gimpfu.main()
