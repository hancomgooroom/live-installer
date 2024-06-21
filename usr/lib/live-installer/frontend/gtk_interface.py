#!/usr/bin/env python3

from installer import InstallerEngine, Setup, NON_LATIN_KB_LAYOUTS
from slideshow import Slideshow
from dialogs import MessageDialog, QuestionDialog, ErrorDialog, WarningDialog
import timezones
import partitioning
from widgets import PictureChooserButton

import gettext
import os
import re
import subprocess
import sys
import PIL
import threading
import time
import parted

import re

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GObject, WebKit2

gettext.install("live-installer", "/usr/share/gooroom/locale")

LOADING_ANIMATION = '/usr/share/live-installer/loading.gif'

# Used as a decorator to run things in the background
async def func():
    def wrapper(*args, **kwargs):
        thread = threading.Thread(target=func, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
        return thread
    return wrapper

# Used as a decorator to run things in the main loop, from another thread
def idle(func):
    def wrapper(*args, **kwargs):
        GObject.idle_add(func, *args, **kwargs)
    return wrapper


class WizardPage:
    def __init__(self, title, description):
        self.title = title
        self.desc = description

class InstallerWindow:
    # Cancelable timeout for keyboard preview generation, which is
    # quite expensive, so avoid drawing it if only scrolling through
    # the keyboard layout list
    kbd_preview_generation = -1

    def __init__(self):

        #Disable the screensaver
        if not __debug__:
            os.system("killall light-locker")

        #Build the Setup object (where we put all our choices)
        self.setup = Setup()

        self.resource_dir = '/usr/share/live-installer/'
        glade_file = os.path.join(self.resource_dir, 'interface.ui')
        self.builder = Gtk.Builder()
        self.builder.add_from_file(glade_file)

        # should be set early
        self.done = False
        self.fail = False
        self.paused = False
        self.showing_last_dialog = False

        # here comes the installer engine
        self.installer = InstallerEngine()

        # load the window object
        self.window = self.builder.get_object("main_window")
        self.window.connect("delete-event", self.quit_cb)
        self.button_next = self.builder.get_object("button_next")
        self.button_back = self.builder.get_object("button_back")

        # Wizard pages
        (self.PAGE_INTRO,
         self.PAGE_SETTING,
         self.PAGE_PARTITIONS,
         self.PAGE_OVERVIEW,
         self.PAGE_INSTALL,
         self.PAGE_FINISH) = range(6)
        self.wizard_pages = list(range(6))

        # set the button events (wizard_cb)
        self.builder.get_object("start_gooroom_btn").connect("clicked", self.wizard_cb, False)
        self.builder.get_object("button_next").connect("clicked", self.wizard_cb, False)
        self.builder.get_object("button_back").connect("clicked", self.wizard_cb, True)
        self.builder.get_object("button_quit").connect("clicked", self.quit_cb)
        self.builder.get_object("button_min").connect("clicked", self.min_cb)
        self.builder.get_object("btn_cancel").connect("clicked", self.quit_cb)
        self.builder.get_object("restart_gooroom_btn").connect("clicked", self.button_reboot_cb)

        self.button_key_test = Gtk.Button.new()
        self.button_key_test.set_name("button_key_test")
        self.builder.get_object("move_key_test").pack_end(self.button_key_test,False,False,0)
        self.button_key_test.connect("clicked", self.show_test_keyboard)

        self.button_show_consent = Gtk.Button.new();
        self.button_label = Gtk.Label.new("keyboard");
        self.button_show_consent.add(self.button_label)
        self.button_show_consent.set_name("button_show_consent")
        self.builder.get_object("move_consent").pack_end(self.button_show_consent,False,False,0)
        self.button_show_consent.connect("clicked", self.show_consent_form)

        self.is_consent (self.builder.get_object("button_agree"))
        self.builder.get_object("button_agree").connect("toggled", self.is_consent)

        col = Gtk.TreeViewColumn("", Gtk.CellRendererPixbuf(), pixbuf=2)
        self.builder.get_object("treeview_language_list").append_column(col)
        ren = Gtk.CellRendererText()
        self.language_column = Gtk.TreeViewColumn(_("Language"), ren, text=0)
        self.language_column.set_sort_column_id(0)
        self.builder.get_object("treeview_language_list").append_column(self.language_column)
        self.country_column = Gtk.TreeViewColumn(_("Country"), ren, text=1)
        self.country_column.set_sort_column_id(1)
        self.builder.get_object("treeview_language_list").append_column(self.country_column)

        self.builder.get_object("treeview_language_list").connect("cursor-changed", self.assign_language)

        self.builder.get_object("combobox_language").connect("changed", self.assign_lang)

        # build the language list
        combobox = self.builder.get_object("combobox_language")
        renderer_text = Gtk.CellRendererText()
        combobox.pack_start(renderer_text,True)
        combobox.add_attribute(renderer_text, "text", 0)

        self.build_lang_list()

        # build timezones
        model = timezones.build_timezones(self)
        lang_country_code = self.setup.language.split('_')[-1]
        for value in (self.cur_timezone,      # timezone guessed from IP
                        self.cur_country_code,  # otherwise pick country from IP
                        lang_country_code):     # otherwise use country from language selection
            if not value:
                continue
            for row in timezones.timezones:
                if value in row:
                    timezones.select_timezone(row)
                    break
            break
        self.builder.get_object("combobox_timezones").connect("changed",self.combobox_timezones_changed_cb) 

        # kb models
        cell = Gtk.CellRendererText()
        self.builder.get_object("combobox_kb_model").pack_start(cell, True)
        self.builder.get_object("combobox_kb_model").add_attribute(cell, 'text', 0)
        self.builder.get_object("combobox_kb_model").connect("changed", self.assign_keyboard_model)

        # kb layouts
        ren = Gtk.CellRendererText()
        self.column10 = Gtk.TreeViewColumn(_("Layout"), ren)
        self.column10.add_attribute(ren, "text", 0)
        self.builder.get_object("treeview_layouts").append_column(self.column10)
        self.builder.get_object("treeview_layouts").connect("cursor-changed", self.assign_keyboard_layout)

        ren = Gtk.CellRendererText()
        self.column11 = Gtk.TreeViewColumn(_("Variant"), ren)
        self.column11.add_attribute(ren, "text", 0)
        self.builder.get_object("treeview_variants").append_column(self.column11)
        #self.builder.get_object("treeview_variants").connect("cursor-changed", self.assign_keyboard_variant)
        rentext = Gtk.CellRendererText()
        cell = Gtk.CellRendererText()
        self.builder.get_object("combobox_layout").pack_start(rentext, True)
        self.builder.get_object("combobox_layout").add_attribute(rentext, 'text', 0)
        self.builder.get_object("combobox_layout").connect("changed", self.assign_keyboard_variant)
 
        self.build_kb_lists()
        self.build_kb_variants()

        self.builder.get_object("label_key_test").set_label(_("Please enter a test letter."))

        # partitions
        #self.builder.get_object("button_custommount").connect("clicked", self.show_customwarning)
        self.builder.get_object("button_edit").connect("clicked", partitioning.manually_edit_partitions)
        self.builder.get_object("button_refresh").connect("clicked", lambda _: partitioning.build_partitions(self))
        self.builder.get_object("treeview_disks").get_model()
        self.builder.get_object("treeview_disks").get_selection().connect("changed", partitioning.update_html_preview)
        self.builder.get_object("treeview_disks").connect("row_activated", partitioning.edit_partition_dialog)
        self.builder.get_object("treeview_disks").connect("button-release-event", partitioning.partitions_popup_menu)
        text = Gtk.CellRendererText()
        for i in (partitioning.IDX_PART_PATH,
                  partitioning.IDX_PART_TYPE,
                  partitioning.IDX_PART_DESCRIPTION,
                  partitioning.IDX_PART_MOUNT_AS,
                  partitioning.IDX_PART_FORMAT_AS,
                  partitioning.IDX_PART_SIZE,
                  partitioning.IDX_PART_FREE_SPACE):
            col = Gtk.TreeViewColumn("", text, markup=i)  # real title is set in i18n()
            self.builder.get_object("treeview_disks").append_column(col)

        # link the checkbutton to the combobox
        grub_check = self.builder.get_object("checkbutton_grub")
        grub_box = self.builder.get_object("combobox_grub")
        grub_check.connect("toggled", self.assign_grub_install, grub_box)
        grub_box.connect("changed", self.assign_grub_device)

        # Install Grub by default
        grub_check.set_active(True)
        grub_box.set_sensitive(True)
        
        # 'about to install' aka overview
        ren = Gtk.CellRendererText()
        self.column12 = Gtk.TreeViewColumn(_("Overview"), ren)
        self.column12.add_attribute(ren, "markup", 0)
        self.builder.get_object("treeview_overview").append_column(self.column12)
        # install page
        self.builder.get_object("label_install_progress").set_markup("<i>%s</i>" % _("Calculating file indexes ..."))

        #i18n
        if __debug__:
            self.window.set_title("%s" % self.installer.get_distribution_name() + ' (debug)')
        else:
            self.window.set_title("%s" % self.installer.get_distribution_name())

        self.i18n()

        # build partition list
        self.should_pulse = False

        # Initiate the slide show
        self.slideshow_path = "/usr/share/live-installer/slideshow"
        if os.path.exists(self.slideshow_path):
            self.slideshow_browser = WebKit2.WebView()
            self.slideshow_browser.connect('context_menu',self.disable_right_cb)
            s = self.slideshow_browser.get_settings()
            s.set_property('allow-file-access-from-file-urls', True)
            #s.set_property('enable-default-context-menu', False)
            self.slideshow_browser.load_uri("file://" + os.path.join(self.slideshow_path, 'template.html'))
            #self.builder.get_object("vbox_install").add(self.slideshow_browser)
            self.builder.get_object("vbox_install").pack_start(self.slideshow_browser, True, True, 0)
            self.builder.get_object("vbox_install").show_all()

        self.partitions_browser = WebKit2.WebView()
        s = self.partitions_browser.get_settings()
        self.partitions_browser.connect('context_menu', self.disable_right_cb)
        s.set_property('allow-file-access-from-file-urls', True)
        #s.set_property('enable-default-context-menu', False)
        #self.partitions_browser.set_transparent(True)
        self.partitions_browser.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        self.builder.get_object("scrolled_partitions").add(self.partitions_browser)

        self.assign_lang(self.builder.get_object("combobox_language"))
        self.activate_page(self.PAGE_INTRO)

        self.window.maximize()

        #to prevent duplication of partitioning
        self.PARTITIONING_DONE = False

        self.window.show_all()

    def update_preview_cb(self, dialog, preview):
        filename = dialog.get_preview_filename()
        dialog.set_preview_widget_active(False)
        try:
            if os.path.isfile(filename):
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(filename, 128, 128)
                if pixbuf:
                    preview.set_from_pixbuf(pixbuf)
                    dialog.set_preview_widget_active(True)
        except Exception:
            pass
    def _on_face_browse_menuitem_activated(self, menuitem):
        dialog = Gtk.FileChooserDialog(None, None, Gtk.FileChooserAction.OPEN, (_("Cancel"), Gtk.ResponseType.CANCEL, _("Open"), Gtk.ResponseType.OK))

        filter = Gtk.FileFilter()
        filter.set_name(_("Images"))
        filter.add_mime_type("image/*")
        dialog.add_filter(filter)

        preview = Gtk.Image()
        dialog.set_preview_widget(preview)
        dialog.connect("update-preview", self.update_preview_cb, preview)
        dialog.set_use_preview_label(False)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            image = PIL.Image.open(path)
            width, height = image.size
            if width > height:
                new_width = height
                new_height = height
            elif height > width:
                new_width = width
                new_height = width
            else:
                new_width = width
                new_height = height
            left = (width - new_width)/2
            top = (height - new_height)/2
            right = (width + new_width)/2
            bottom = (height + new_height)/2
            image = image.crop((left, top, right, bottom))
            image.thumbnail((96, 96), PIL.Image.ANTIALIAS)
            face_path = "/tmp/live-installer-face.png"
            image.save(face_path, "png")
#            self.face_button.set_picture_from_file(face_path)

        dialog.destroy()

    def _on_face_menuitem_activated(self, path):
        if os.path.exists(path):
            os.system("cp %s /tmp/live-installer-face.png" % path)
            print(path)
            return True

    def _on_face_take_picture_button_clicked(self, menuitem):
        # streamer takes -t photos
        if 0 != os.system('streamer -j90 -t8 -s800x600 -o /tmp/live-installer-face00.jpeg'):
            return  # Error, no webcam
        # Convert and resize the 7th frame (the webcam takes a few frames to "lighten up")
        os.system('convert /tmp/live-installer-face07.jpeg -crop 600x600+100+0 -resize 96x96 /tmp/live-installer-face.png')
#        self.face_button.set_picture_from_file("/tmp/live-installer-face.png")

    def i18n(self):

        self.language_column.set_title(_("Language"))
        self.country_column.set_title(_("Country"))

        desc = _("Thank you for choosing Hancom Gooroom. Several settings are required to install Hancom Gooroom.\nInstallation proceeds after setting up the environment, user information, and partition.\nDo you want to install Hancom Gooroom on this PC?")
        self.wizard_pages[self.PAGE_INTRO] = WizardPage(_("Install Hancom Gooroom"),desc)
        desc = _("Select the language, time zone, and keyboard environment required for Hancom Gooroom installation.")
        self.wizard_pages[self.PAGE_SETTING] = WizardPage(_("Installatiaion First Stage")
                ,desc)
        desc = _("If you edit the partition, refresh the information, and select the partition to be installed, you can proceed to the next step.")
        title = _("Installation Second Stage")
        self.wizard_pages[self.PAGE_PARTITIONS] = WizardPage(title,desc)
        desc = _("The preparation for Hancom Gooroom installation is completed. Please check your settings and install Hancom Gooroom.")
        self.wizard_pages[self.PAGE_OVERVIEW] = WizardPage(_("Ready to Install"), desc)
        desc = _("Maintain the power of the computer while installing Hancom Gooroom.")
        self.wizard_pages[self.PAGE_INSTALL] = WizardPage(_("Installing Hancom Gooroom"), desc)
        title = _("Hancom Gooroom is installed")
        desc = _("The installation has been successfully completed.\nDo you want to restart your computer to use a new Hancom Gooroom?")
        self.wizard_pages[self.PAGE_FINISH] = WizardPage(title, desc)

        self.builder.get_object("button_cancel").set_label(_("Cancel"))
        self.builder.get_object("btn_cancel").set_label(_("Cancel"))
        self.builder.get_object("button_ok").set_label(_("OK"))
        #self.builder.get_object("button_quit").set_label(_("Quit"))
        self.builder.get_object("button_back").set_label(_("Back"))
        self.builder.get_object("button_next").set_label(_("Next"))
        self.builder.get_object("restart_gooroom_btn").set_label(_("Reboot"))
        self.builder.get_object("start_gooroom_btn").set_label(_("Let's get started"))

        self.builder.get_object("label_lang").set_label(_("Language"))
        self.button_key_test.set_label(_("Test Keyboard"))
        self.builder.get_object("key_test_window").set_title(_("Test Keyboard"))
        self.builder.get_object("label_key_test").set_label(_("Please enter a test letter."))

        self.button_label.set_markup(_("<span fgcolor='#000000'>I agree with</span> <span fgcolor='#0251ff'>the Software License.</span>"))

        self.builder.get_object("button_edit").set_label(_("Edit partitions"))
        self.builder.get_object("button_refresh").set_label(_("Refresh"))

        # timezones
        self.builder.get_object("label_timezones").set_label(_("Timezone"))
        self.builder.get_object("label_kb_layout").set_label(_("Keyboard layout"))
        self.builder.get_object("label_kb_model").set_label(_("Keyboard Model"))

        # grub
        self.builder.get_object("label_grub").set_markup("<b>%s</b>" % _("Bootloader"))
        self.builder.get_object("checkbutton_grub").set_label(_("Install GRUB"))
        self.builder.get_object("label_grub_help").set_label(_("GRUB is a bootloader used to load the Linux kernel."))

        # Columns
        for col, title in zip(self.builder.get_object("treeview_disks").get_columns(),
                              (_("Device"),
                               _("Type"),
                               _("Operating system"),
                               _("Mount point"),
                               _("Format as"),
                               _("Size"),
                               _("Free space"))):
            col.set_title(title)

        self.column10.set_title(_("Layout"))
        self.column11.set_title(_("Variant"))
        self.column12.set_title(_("Overview"))

    def assign_hostname(self, entry, prop):
        self.setup.hostname = entry.props.text
        self.setup.print_setup()

    def min_cb (self, widget):
        self.window.iconify()

    def quit_cb(self, widget, data=None):
        if QuestionDialog(_("Quit"), _("Are you sure you want to quit the installer?")):
            Gtk.main_quit()
            return False
        else:
            return True

    def show_customwarning(self, widget):
        self.activate_page(self.PAGE_CUSTOMWARNING)

    def build_lang_list(self):

        # Try to find out where we're located...
        try:
            from urllib import urlopen
        except ImportError:  # py3
            from urllib.request import urlopen
        try:
            lookup = str(urlopen('http://geoip.ubuntu.com/lookup').read())
            cur_country_code = re.search('<CountryCode>(.*)</CountryCode>', lookup).group(1)
            cur_timezone = re.search('<TimeZone>(.*)</TimeZone>', lookup).group(1)
            if cur_country_code == 'None': cur_country_code = None
            if cur_timezone == 'None': cur_timezone = None
        except:
            cur_country_code, cur_timezone = None, None  # no internet connection

        self.cur_country_code = cur_country_code or os.environ.get('LANG', 'US').split('.')[0].split('_')[-1]  # fallback to LANG location or 'US'
        self.cur_timezone = cur_timezone
       
        #Load countries into memory
        countries = {}
        iso_standard = "3166"
        if os.path.exists("/usr/share/xml/iso-codes/iso_3166-1.xml"):
            iso_standard = "3166-1"
        for line in subprocess.getoutput("isoquery --iso %s | cut -f1,4-" % iso_standard).split('\n'):
            ccode, cname = line.split(None, 1)
            countries[ccode] = cname

        #Load languages into memory
        languages = {}
        iso_standard = "639"
        if os.path.exists("/usr/share/xml/iso-codes/iso_639-2.xml"):
            iso_standard = "639-2"
        for line in subprocess.getoutput("isoquery --iso %s | cut -f3,4-" % iso_standard).split('\n'):
            cols = line.split(None, 1)
            if len(cols) > 1:
                name = cols[1].replace(";", ",")
                languages[cols[0]] = name
        for line in subprocess.getoutput("isoquery --iso %s | cut -f1,4-" % iso_standard).split('\n'):
            cols = line.split(None, 1)
            if len(cols) > 1:
                if cols[0] not in languages.keys():
                    name = cols[1].replace(";", ",")
                    languages[cols[0]] = name

        # Construct language selection model
        model = Gtk.ListStore(str, str, GdkPixbuf.Pixbuf, str)
        language_store = Gtk.ListStore(str, str, GdkPixbuf.Pixbuf, str)

        set_iter = None
        tree_iter = None
        flag_path = lambda ccode: self.resource_dir + '/flags/16/' + ccode.lower() + '.png'
        from utils import memoize
        flag = memoize(lambda ccode: GdkPixbuf.Pixbuf.new_from_file(flag_path(ccode)))
        for locale in subprocess.getoutput("awk -F'[@ .]' '/UTF-8/{ print $1 }' /usr/share/i18n/SUPPORTED | uniq").split('\n'):
            if '_' in locale:
                lang, ccode = locale.split('_')
                language = lang
                country = ccode
                try:
                    language = languages[lang]
                except:
                    pass
                try:
                    country = countries[ccode]
                except:
                    pass
            else:
                lang = locale
                try:
                    language = languages[lang]
                except:
                    pass
                country = ''
            pixbuf = flag(ccode) if not lang in 'eo ia' else flag('_' + lang)

            iter = model.append((language, country, pixbuf, locale))
            
            if ((lang == 'en' and country == 'United States')or lang == 'ko'):
                i = language_store.append((language, country, pixbuf, locale))
                if(lang == 'ko'):
                    set_language_init = i;

            if (ccode == self.cur_country_code and
                (not set_iter or
                 set_iter and lang == 'en' or  # prefer English, or
                 set_iter and lang == ccode.lower())):  # fuzzy: lang matching ccode (fr_FR, de_DE, es_ES, ...)
                set_iter = iter
                tree_iter = i

        # Sort by Country, then by Language
        model.set_sort_column_id(0, Gtk.SortType.ASCENDING)
        model.set_sort_column_id(1, Gtk.SortType.ASCENDING)
        # Set the model and pre-select the correct language
        treeview = self.builder.get_object("treeview_language_list")
        treeview.set_model(model)
        if set_iter:
            path = model.get_path(set_iter)
            treeview.set_cursor(path)
            treeview.scroll_to_cell(path)

        combobox = self.builder.get_object("combobox_language")
        combobox.set_model(language_store)
        combobox.set_active_iter(set_language_init);
    
    def build_kb_variants (self):
        if ("_" in self.setup.language):
            country_code = self.setup.language.split("_")[1]
        else:
            country_code = self.setup.language
            
        treeview = self.builder.get_object("treeview_layouts")
        model = treeview.get_model()
        iter = model.get_iter_first()
        while iter is not None:
            iter_country_code = model.get_value(iter, 1)
            if iter_country_code.lower() == country_code.lower():
                self.setup.keyboard_layout = iter_country_code.lower()
                column = treeview.get_column(0)
                path = model.get_path(iter)
                treeview.set_cursor(path)
                treeview.scroll_to_cell(path, column=column)
                break
            iter = model.iter_next(iter)

        # Set the correct variant list model ...
        model = self.layout_variants[iter_country_code]
        self.builder.get_object("treeview_variants").set_model(model)
        # ... and select the first variant (standard)
        #self.builder.get_object("treeview_variants").set_cursor(0)

        self.builder.get_object("combobox_layout").set_model(model)
        self.builder.get_object("combobox_layout").set_active(0)

    def build_kb_lists(self):
        ''' Do some xml kung-fu and load the keyboard stuffs '''
        # Determine the layouts in use
        (keyboard_geom,
         self.setup.keyboard_layout) = subprocess.getoutput("setxkbmap -query | awk '/^(model|layout)/{print $2}'").split()

        # Build the models
        from collections import defaultdict
        def _ListStore_factory():
            model = Gtk.ListStore(str, str)
            model.set_sort_column_id(0, Gtk.SortType.ASCENDING)
            return model
        models = _ListStore_factory()
        layouts = _ListStore_factory()
        variants = defaultdict(_ListStore_factory)
        try:
            import xml.etree.cElementTree as ET
        except ImportError:
            import xml.etree.ElementTree as ET
        xml = ET.parse('/usr/share/X11/xkb/rules/xorg.xml')
        for node in xml.iterfind('.//modelList/model/configItem'):
            name, desc = node.find('name').text, node.find('description').text
            desc = desc[:37]+ (desc[37:] and '..')
            iterator = models.append((desc, name))
            if name == keyboard_geom:
                set_keyboard_model = iterator
        for node in xml.iterfind('.//layoutList/layout'):
            name, desc = node.find('configItem/name').text, node.find('configItem/description').text
            nonedesc = desc
            if name in NON_LATIN_KB_LAYOUTS:
                nonedesc = "English (US) + %s" % nonedesc
            variants[name].append((nonedesc, None))
            for variant in node.iterfind('variantList/variant/configItem'):
                var_name, var_desc = variant.find('name').text, variant.find('description').text
                var_desc = var_desc if var_desc.startswith(desc) else '{} - {}'.format(desc, var_desc)
                if name in NON_LATIN_KB_LAYOUTS and "Latin" not in var_desc:
                    var_desc = "English (US) + %s" % var_desc
                var_desc = var_desc[:42]+ (var_desc[42:] and '..')
                variants[name].append((var_desc, var_name))
            if name in NON_LATIN_KB_LAYOUTS:
                desc = desc + " *"

            #if( ('English' in desc) or desc == 'Korean'):
            if( desc == 'English (US)' or desc == 'Korean'):
                iterator = layouts.append((desc, name))

            if name == self.setup.keyboard_layout:
                set_keyboard_layout = iterator

        # Set the models
        self.builder.get_object("combobox_kb_model").set_model(models)
        self.builder.get_object("treeview_layouts").set_model(layouts)
        self.layout_variants = variants
        # Preselect currently active keyboard info
        try:
            self.builder.get_object("combobox_kb_model").set_active_iter(set_keyboard_model)
        except NameError: pass  # set_keyboard_model not set
        try:
            treeview = self.builder.get_object("treeview_layouts")
            path = layouts.get_path(set_keyboard_layout)
            treeview.set_cursor(path)
            treeview.scroll_to_cell(path)
        except NameError: pass  # set_keyboard_layout not set

    def assign_language(self, treeview, data=None):
        ''' Called whenever someone updates the language '''
        model = treeview.get_model()
        selection = treeview.get_selection()
        if selection.count_selected_rows:
            (model, iter) = selection.get_selected()
            if iter is not None:
                self.setup.language = model.get_value(iter, 3)
                self.setup.print_setup()
                gettext.translation('live-installer', "/usr/share/gooroom/locale",
                                languages=[self.setup.language, self.setup.language.split('_')[0]],
                                fallback=True).install()  # Try e.g. zh_CN, zh, or fallback to hardcoded English
                try:
                    self.i18n()
                except:
                    pass # Best effort. Fails the first time as self.column1 doesn't exist yet.

    def assign_lang(self, combobox, data=None):
        tree_iter = combobox.get_active_iter()

        if tree_iter is not None:
            model = combobox.get_model()
            self.setup.language = model[tree_iter][3]
            self.setup.print_setup()
            self.set_agreement()
            gettext.translation('live-installer', "/usr/share/gooroom/locale",
                        languages=[self.setup.language, self.setup.language.split('_')[0]],
                        fallback=True).install() 
            try:
                self.i18n()
                self.activate_page(self.PAGE_SETTING)
            except:
                pass # Best effort. Fails the first time as self.column1 doesn't exist yet.

        self.build_kb_variants()

    def assign_autologin(self, checkbox, data=None):
        self.setup.autologin = checkbox.get_active()
        self.setup.print_setup()

    def assign_grub_install(self, checkbox, grub_box, data=None):
        grub_box.set_sensitive(checkbox.get_active())
        if checkbox.get_active():
            self.assign_grub_device(grub_box)
        else:
            self.setup.grub_device = None
        self.setup.print_setup()

    def assign_grub_device(self, combobox, data=None):
        ''' Called whenever someone updates the grub device '''
        model = combobox.get_model()
        active = combobox.get_active()
        if(active > -1):
            row = model[active]
            self.setup.grub_device = row[0]
        self.setup.print_setup()

    def show_test_keyboard (self,button):
        key_test_win = self.builder.get_object("key_test_window")
        key_test_win.run()
        key_test_win.hide()
        self.builder.get_object("entry_test_kb").set_text("")

    def show_consent_form (self,button):
        win = self.builder.get_object("consent_window")
        win.set_title (_("Hancom Gooroom Software License Agreement"))
        win.run()
        win.hide()

    def assign_keyboard_model(self, combobox):
        ''' Called whenever someone updates the keyboard model '''
        model = combobox.get_model()
        active = combobox.get_active()
        (self.setup.keyboard_model_description, self.setup.keyboard_model) = model[active]
        if not __debug__:
            os.system('setxkbmap -model ' + self.setup.keyboard_model)
        self.setup.print_setup()

    def combobox_timezones_changed_cb(self,combobox):
        active_iter = combobox.get_active_iter()
        model = combobox.get_model()
        self.cur_timezone = model[active_iter]
        for value in (self.cur_timezone):
            if not value:
                continue
            for row in timezones.timezones:
                if value in row:
                    timezones.select_timezone(row)
                    break
            break

    def assign_keyboard_layout(self, treeview):
        ''' Called whenever someone updates the keyboard layout '''
        model, active = treeview.get_selection().get_selected_rows()
        if not active: return
        (self.setup.keyboard_layout_description,
         self.setup.keyboard_layout) = model[active[0]]
        # Set the correct variant list model ...
        model = self.layout_variants[self.setup.keyboard_layout]
        self.builder.get_object("treeview_variants").set_model(model)
        # ... and select the first variant (standard)
        self.builder.get_object("treeview_variants").set_cursor(0)

    def assign_keyboard_variant(self, combobox):
        ''' Called whenever someone updates the keyboard layout or variant '''
        #GObject.source_remove(self.kbd_preview_generation)  # stop previous preview generation, if any
        active_iter = combobox.get_active_iter()
        treeview = self.builder.get_object("treeview_variants")
        c_model = treeview.get_model()
        if active_iter:
            path = c_model.get_path(active_iter)
            treeview.set_cursor(path)
            treeview.scroll_to_cell(path)

        model, active = treeview.get_selection().get_selected_rows()
        if not active: return
        (self.setup.keyboard_variant_description,
         self.setup.keyboard_variant) = model[active[0]]

        if self.setup.keyboard_variant is None:
            self.setup.keyboard_variant = ""

        if self.setup.keyboard_layout in NON_LATIN_KB_LAYOUTS:
            # Add US layout for non-latin layouts
            self.setup.keyboard_layout = 'us,%s' % self.setup.keyboard_layout

        if "Latin" in self.setup.keyboard_variant_description:
            # Remove US layout for Latin variants
            self.setup.keyboard_layout = self.setup.keyboard_layout.replace("us,", "")

        if "us," in self.setup.keyboard_layout:
            # Add None variant for US layout
            self.setup.keyboard_variant = ',%s' % self.setup.keyboard_variant

        self.builder.get_object("label_non_latin").set_text(_("* Your username, hostname and password should only contain Latin characters. In addition to your selected layout, English (US) is set as the default. You can switch layouts by pressing both Ctrl keys together."))
        if "us," in self.setup.keyboard_layout:
            self.builder.get_object("label_non_latin").show()
        else:
            self.builder.get_object("label_non_latin").hide()

        command = "setxkbmap -layout '%s' -variant '%s' -option grp:ctrls_toggle" % (self.setup.keyboard_layout, self.setup.keyboard_variant)
        print(command)
        if not __debug__:
            os.system(command)
            self.setup.print_setup()

        # Set preview image
        #self.builder.get_object("image_keyboard").set_from_file(LOADING_ANIMATION)
        self.kbd_preview_generation = GObject.timeout_add(500, self._generate_keyboard_layout_preview)

        # Set preview image
        self.builder.get_object("image_keyboard").set_from_file(LOADING_ANIMATION)
        self.kbd_preview_generation = GObject.timeout_add(500, self._generate_keyboard_layout_preview)

    def _generate_keyboard_layout_preview(self):
        filename = "/tmp/live-install-keyboard-layout.png"
        layout = self.setup.keyboard_layout.split(",")[-1]
        variant = self.setup.keyboard_variant.split(",")[-1]
        if variant == "":
            variant = None
        print("python3 /usr/lib/live-installer/frontend/generate_keyboard_layout.py {} {} {}".format(layout, variant, filename))
        os.system("python3 /usr/lib/live-installer/frontend/generate_keyboard_layout.py {} {} {}".format(layout, variant, filename))
        self.builder.get_object("image_keyboard").set_from_file(filename)
        return False

    def activate_page(self, index):
        title = _(self.wizard_pages[index].title)
        description = _(self.wizard_pages[index].desc)

        self.builder.get_object("title").set_markup("<span font='24px'><b>%s</b></span>" % title)
        self.builder.get_object("desc").set_markup("%s" % description)

        def current_page(idx):
            switch = {
                self.PAGE_INTRO:'intro_page',
                self.PAGE_SETTING:'setting_page',
                self.PAGE_PARTITIONS:'partitions_page',
                self.PAGE_OVERVIEW:'overview_page',
                self.PAGE_INSTALL:'install_page',
                self.PAGE_FINISH:'finish_page'
            }
            return switch.get(idx,-1)

        small_right = self.builder.get_object('small_right').get_style_context();
        right = self.builder.get_object('right').get_style_context();

        if(current_page(index) == 'intro_page' or 
                current_page(index) == 'install_page' or
                current_page(index) == 'finish_page'):
            self.builder.get_object('button_next').hide()
            self.builder.get_object('button_back').hide()
            right.remove_class("main_window")
            small_right.add_class("main_window")
        else:
            self.builder.get_object('button_next').show()
            self.builder.get_object('button_back').show()
            right.add_class("main_window")
            small_right.remove_class("main_window")

        self.builder.get_object('installer_stack').set_visible_child_name(current_page(index))

        # TODO: move other page-depended actions from the wizard_cb into here below
        if index == self.PAGE_PARTITIONS:
            self.setup.skip_mount = False
        elif index == self.PAGE_INSTALL:
            import threading
            thr1 = threading.Thread(target=self.do_install)
            thr1.daemon = True
            thr1.start()

    def wizard_cb(self, widget, goback, data=None):
        ''' wizard buttons '''
        stack = self.builder.get_object("installer_stack")
        self.builder.get_object("button_back").set_sensitive(True)
        current_page = stack.get_visible_child_name()
        height = 0;

        if (not goback):
            if(current_page == "intro_page"):
                self.activate_page(self.PAGE_SETTING)
            elif (current_page == "setting_page"):
                height = self.builder.get_object("first_page").get_allocated_height ()
                self.builder.get_object("vbox4").set_size_request (-1, height)
                self.builder.get_object("vbox12").set_size_request (-1, height)

                if self.setup.language is None:
                    WarningDialog(_("Installation Tool"), _("Please choose a language"))
                self.activate_page(self.PAGE_PARTITIONS)
                if not self.PARTITIONING_DONE:
                    partitioning.build_partitions(self)

                partitioning.build_grub_partitions()

            elif (current_page == "partitions_page"):

                model = self.builder.get_object("treeview_disks").get_model()

                # Check for root partition
                found_root_partition = False
                for partition in self.setup.partitions:
                    if(partition.mount_as == "/"):
                        found_root_partition = True
                        if partition.format_as is None or partition.format_as == "":
                            ErrorDialog(_("Installation Tool"), _("Please indicate a filesystem to format the root (/) partition with before proceeding."))
                            return
                if not found_root_partition:
                    ErrorDialog(_("Installation Tool"), "<b>%s</b>" % _("Please select a root (/) partition."), _("A root partition is needed to install Gooroom Platform on.\n\n - Mount point: /\n - Recommended size: 30GB\n - Recommended filesystem format: ext4\n "))
                    return

                if self.setup.gptonefi:
                    # Check for an EFI partition
                    found_efi_partition = False
                    for partition in self.setup.partitions:
                        if(partition.mount_as == "/boot/efi"):
                            found_efi_partition = True
                            if not partition.partition.getFlag(parted.PARTITION_BOOT):
                                ErrorDialog(_("Installation Tool"), _("The EFI partition is not bootable. Please edit the partition flags."))
                                return
                            if int(float(partition.partition.getLength('MB'))) < 100:
                                ErrorDialog(_("Installation Tool"), _("The EFI partition is too small. It must be at least 100MB."))
                                return
                            if partition.format_as == None or partition.format_as == "":
                                # No partitioning
                                if partition.type != "vfat" and partition.type != "fat32" and partition.type != "fat16":
                                    ErrorDialog(_("Installation Tool"), _("The EFI partition must be formatted as vfat."))
                                    return
                            else:
                                if partition.format_as != "vfat":
                                    ErrorDialog(_("Installation Tool"), _("The EFI partition must be formatted as vfat."))
                                    return

                    if not found_efi_partition:
                        ErrorDialog(_("Installation Tool"), "<b>%s</b>" % _("Please select an EFI partition."),_("An EFI system partition is needed with the following requirements:\n\n - Mount point: /boot/efi\n - Partition flags: Bootable\n - Size: Larger than 100MB\n - Format: vfat or fat32\n\nTo ensure compatibility with Windows we recommend you use the first partition of the disk as the EFI system partition.\n "))
                        return

                self.activate_page(self.PAGE_OVERVIEW)
                self.show_overview()
                self.builder.get_object("treeview_overview").expand_all()
                self.button_next.set_label(_("Install"))
 
            elif (current_page == "overview_page"):
                self.activate_page(self.PAGE_INSTALL)
        else:
            if (current_page == "install_page"):
                self.activate_page(self.PAGE_OVERVIEW)
            elif (current_page == "overview_page"):
                self.button_next.set_label(_("Next"))
                self.activate_page(self.PAGE_PARTITIONS)
            elif (current_page == "partitions_page"):
                #to prevent duplication of partition
                found_root_partition = False
                for partition in self.setup.partitions:
                    if(partition.mount_as == "/"):
                        found_root_partition = True
                if found_root_partition:
                    self.PARTITIONING_DONE = True
                self.activate_page(self.PAGE_SETTING)
            elif (current_page == "setting_page"):
                self.activate_page(self.PAGE_INTRO)

    def show_overview(self):
        bold = lambda str: '<b>' + str + '</b>'
        model = Gtk.TreeStore(str)
        self.builder.get_object("treeview_overview").set_model(model)
        top = model.append(None, (_("Localization"),))
        model.append(top, (_("Language: ") + bold(self.setup.language),))
        model.append(top, (_("Timezone: ") + bold(self.setup.timezone),))
        model.append(top, (_("Keyboard layout: ") +
                           "<b>%s - %s %s</b>" % (self.setup.keyboard_model_description, self.setup.keyboard_layout_description,
                                                  '(%s)' % self.setup.keyboard_variant_description if self.setup.keyboard_variant_description else ''),))
        top = model.append(None, (_("System settings"),))
        model.append(top, (_("Hostname: ") + bold(self.setup.hostname),))
        top = model.append(None, (_("Filesystem operations"),))
        model.append(top, (bold(_("Install bootloader on %s") % self.setup.grub_device) if self.setup.grub_device else _("Do not install bootloader"),))
        if self.setup.skip_mount:
            model.append(top, (bold(_("Use already-mounted /target.")),))
            return
        for p in self.setup.partitions:
            if p.format_as:
                model.append(top, (bold(_("Format %(path)s as %(filesystem)s") % {'path':p.path, 'filesystem':p.format_as}),))
        for p in self.setup.partitions:
            if p.mount_as:
                model.append(top, (bold(_("Mount %(path)s as %(mount)s") % {'path': p.path, 'mount':p.mount_as}),))

    @idle
    def show_error_dialog(self, message, detail):
        ErrorDialog(message, detail)
        if self.showing_last_dialog:
            self.showing_last_dialog = False

    @idle
    def show_reboot_dialog(self):
        reboot = QuestionDialog(_("Installation finished"), _("The installation is now complete.\nDo you want to restart your computer to use the new system?"))
        if self.showing_last_dialog:
            self.showing_last_dialog = False
        if reboot:
            os.system('reboot')

    @idle
    def pause_installation(self):
        self.activate_page(self.PAGE_CUSTOMPAUSED)
        self.builder.get_object("button_next").show()
        self.builder.get_object("button_next").set_sensitive(True)
        self.builder.get_object("button_back").set_sensitive(True)
        self.builder.get_object("button_quit").set_sensitive(True)
        MessageDialog(_("Installation paused"), _("The installation is now paused. Please read the instructions on the page carefully before clicking Forward to finish the installation."))
        self.builder.get_object("button_next").set_sensitive(True)

    def do_install(self):
        print(" ## INSTALLATION ")
        ''' Actually perform the installation .. '''

        if __debug__:
            print(" ## DEBUG MODE - INSTALLATION PROCESS NOT LAUNCHED")
            time.sleep(10)
            Gtk.main_quit()
            sys.exit(0)

        inst = self.installer
        inst.set_progress_hook(self.update_progress)
        inst.set_error_hook(self.error_message)

        # do we dare? ..
        self.critical_error_happened = False

        # Start installing
        do_try_finish_install = True

        try:
            inst.init_install(self.setup)
        except Exception as detail1:
            print(detail1)
            do_try_finish_install = False
            self.show_error_dialog(_("Installation error"), str(detail1))

        if self.critical_error_happened:
            self.show_error_dialog(_("Installation error"), self.critical_error_message)
            do_try_finish_install = False

        if do_try_finish_install:
            if(self.setup.skip_mount):
                self.paused = True
                self.pause_installation()
                while(self.paused):
                    time.sleep(0.1)

            try:
                inst.finish_install(self.setup)
            except Exception as detail1:
                print(detail1)
                self.show_error_dialog(_("Installation error"), str(detail1))

            # show a message dialog thingum
            while(not self.done):
                time.sleep(0.1)

            self.showing_last_dialog = True
            if self.critical_error_happened:
                self.show_error_dialog(_("Installation error"), self.critical_error_message)
            else:
                #self.show_reboot_dialog()
                self.activate_page(self.PAGE_FINISH)

            while(self.showing_last_dialog):
                time.sleep(0.1)

            print(" ## INSTALLATION COMPLETE ")

        Gtk.main_quit()
        sys.exit(0)

    def error_message(self, message=""):
        self.critical_error_happened = True
        self.critical_error_message = message

    @idle
    def update_progress(self, current, total, pulse, done, message):
        if(pulse):
            self.builder.get_object("label_install_progress").set_label(message)
            self.do_progress_pulse(message)
            return
        if(done):
            self.should_pulse = False
            self.done = done
            self.builder.get_object("progressbar").set_fraction(1)
            self.builder.get_object("label_install_progress").set_label(message)
            return
        self.should_pulse = False
        _total = float(total)
        _current = float(current)
        pct = float(_current/_total)
        szPct = int(pct)
        self.builder.get_object("progressbar").set_fraction(pct)
        self.builder.get_object("label_install_progress").set_label(message)

    @idle
    def do_progress_pulse(self, message):
        def pbar_pulse():
            if(not self.should_pulse):
                return False
            self.builder.get_object("progressbar").pulse()
            return self.should_pulse
        if(not self.should_pulse):
            self.should_pulse = True
            GObject.timeout_add(100, pbar_pulse)
        else:
            # asssume we're "pulsing" already
            self.should_pulse = True
            pbar_pulse()

    def disable_right_cb(self,web_view, context_menu, event, hit_test_result):
        context_menu.remove_all()

    def set_round_edges (self, window):
        screen = window.get_screen();

        if(screen.is_composited()):
            visual = screen.get_rgba_visual();
            
            if(visual is None):
                visual = screen.get_system_visual (screen)
                
            window.set_visual (visual);

    def set_agreement(self):
        scrolledwindow = self.builder.get_object("scrolled_agreement")
        if(scrolledwindow.get_child()!= None):
            scrolledwindow.remove(scrolledwindow.get_child())

        languages=self.setup.language.split('_')[0]
        self.agree_path = "/usr/share/live-installer/contract/%s/"%languages
        if os.path.exists(self.agree_path):
            agreement = WebKit2.WebView.new()
            agreement.connect('context_menu',self.disable_right_cb)
            agreement.load_uri("file://" + os.path.join(self.agree_path, 'contract_form.html'))
            scrolledwindow.add_with_viewport(agreement)
            scrolledwindow.show_all()

    def is_consent (self, button):
        if button.get_active():
            self.button_next.set_sensitive(True)
        else:
            self.button_next.set_sensitive(False)

    def button_reboot_cb(self, button):
        os.system('reboot')
