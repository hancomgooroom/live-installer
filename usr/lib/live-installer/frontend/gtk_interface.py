#!/usr/bin/env python

from installer import InstallerEngine, Setup, NON_LATIN_KB_LAYOUTS
from slideshow import Slideshow
from dialogs import MessageDialog, QuestionDialog, ErrorDialog, WarningDialog
import timezones
import partitioning
from widgets import PictureChooserButton

import gettext
import os
import re
import commands
import sys
import PIL
import threading
import time
import parted
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GObject, WebKit2

gettext.install("live-installer", "/usr/share/gooroom/locale")

LOADING_ANIMATION = '/usr/share/live-installer/loading.gif'

# Used as a decorator to run things in the background
def async(func):
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
        self.description = description 

class InstallerWindow:
    # Cancelable timeout for keyboard preview generation, which is
    # quite expensive, so avoid drawing it if only scrolling through
    # the keyboard layout list
    kbd_preview_generation = -1

    def __init__(self, fullscreen=False):

        #Disable the screensaver
        if not __debug__:
            os.system("killall light-locker")

        #Build the Setup object (where we put all our choices)
        self.setup = Setup()

        self.resource_dir = '/usr/share/live-installer/'
        glade_file = os.path.join(self.resource_dir, 'interface.ui')
        self.builder = Gtk.Builder()
        self.builder.add_from_file(glade_file)

        #image
        self.max_icon = Gtk.Image.new_from_file(self.resource_dir+"own/rest (1).svg")

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

        # Wizard pages
        (self.PAGE_SETTING,
         self.PAGE_USER,
         self.PAGE_PARTITIONS,
         self.PAGE_OVERVIEW,
         self.PAGE_INSTALL,
         self.PAGE_FINISH   ) = range(6)
        self.wizard_pages = range(6)

        # set the button events (wizard_cb)
        self.button_next = Gtk.Button.new()
        self.button_next.set_name("button_next")
        self.builder.get_object("move_page").pack_end(self.button_next,False,False,0)
        self.button_back = Gtk.Button.new()
        self.button_back.set_name("button_back")
        self.button_go_setting = Gtk.Button.new()
        self.button_go_setting.set_name("button_go_setting")
        self.builder.get_object("move_page").pack_start(self.button_back,False,False,0)
        self.button_go_back = Gtk.Button.new()
        self.button_go_back.set_name("button_go_back")
        self.builder.get_object("box_agree").pack_start(self.button_go_back,False,False,0)
        self.button_next.connect("clicked", self.wizard_cb, False)
        self.button_back.connect("clicked", self.wizard_cb, True)
        self.button_go_setting.connect("clicked", self.button_go_setting_cb)
        self.button_go_back.connect("clicked", self.button_go_setting_cb)
        self.builder.get_object("button_quit").connect("clicked", self.quit_cb)
        self.builder.get_object("button_stop").connect("clicked", self.quit_cb)
        self.builder.get_object("button_maximize").connect("clicked", self.set_window_cb, True)
        self.builder.get_object("button_maximize").set_image(self.max_icon)
        self.builder.get_object("button_iconify").connect("clicked", self.set_window_cb, True)
        self.builder.get_object("button_reboot").connect("clicked", self.button_reboot_cb)

        self.button_key_test = Gtk.Button.new()
        self.button_key_test.set_name("button_key_test")
        self.builder.get_object("move_key_test").pack_end(self.button_key_test,False,False,0)
        self.button_key_test.connect("clicked", self.show_test_keyboard)

        self.button_show_consent = Gtk.Button.new();
        self.button_label = Gtk.Label.new("Hello");
        self.button_show_consent.add(self.button_label)
        self.button_show_consent.set_name("button_show_consent")
        self.builder.get_object("move_consent").pack_end(self.button_show_consent,False,False,0)
        self.button_show_consent.connect("clicked", self.show_consent_form)

        # button_agree make button_next sensitive
        self.is_consent(self.builder.get_object("button_agree"))
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

        self.builder.get_object("combobox_language").connect("changed", self.assign_lang)

        # build the language list
        combobox = self.builder.get_object("combobox_language")
        renderer_text = Gtk.CellRendererText()
        combobox.pack_start(renderer_text,True)
        combobox.add_attribute(renderer_text, "text", 0)

        self.build_lang_list()

        # build timezones
        model = timezones.build_timezones(self)
        self.builder.get_object("button_timezones").set_label(_('Select timezone'))
        self.builder.get_object("event_timezones").connect('button-release-event', timezones.cb_map_clicked, model)
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
        self.builder.get_object("button_edit").connect("clicked", partitioning.manually_edit_partitions)
        self.builder.get_object("button_refresh").connect("clicked", lambda _: partitioning.build_partitions(self))
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

        self.builder.get_object("entry_your_name").connect("notify::text", self.assign_realname)
        self.builder.get_object("entry_username").connect("notify::text", self.assign_username)
        self.builder.get_object("entry_hostname").connect("notify::text", self.assign_hostname)

        # events for detecting password mismatch..
        self.builder.get_object("entry_userpass1").connect("changed", self.assign_password)
        self.builder.get_object("entry_userpass2").connect("changed", self.assign_password)

        # link the checkbutton to the combobox
        grub_check = self.builder.get_object("checkbutton_grub1")
        grub_box = self.builder.get_object("combobox_grub1")
        grub_check.connect("toggled", self.assign_grub_install, grub_box)
        grub_box.connect("changed", self.assign_grub_device)

        # Install Grub by default
        grub_check.set_active(True)
        grub_box.set_sensitive(True)

        # encrypt_home
        ecryptfs_check = self.builder.get_object("radiobutton_ecryptfs")
        ecryptfs_check.connect("toggled", self.assign_ecryptfs_install)
        encfs_check = self.builder.get_object("radiobutton_encfs")
        encfs_check.connect("toggled", self.assign_encfs_install)

       # 'about to install' aka overview
        ren = Gtk.CellRendererText()
        self.column12 = Gtk.TreeViewColumn(_("Overview"), ren)
        self.column12.add_attribute(ren, "markup", 0)
        self.builder.get_object("treeview_overview").append_column(self.column12)
        # install page
        self.builder.get_object("label_install_progress").set_markup("<i>%s</i>" % _("Calculating file indexes ..."))

        if __debug__:
            self.window.set_title("%s" % self.installer.get_distribution_name() + ' (debug)')
        else:
            self.window.set_title("%s" % self.installer.get_distribution_name())

        # Pre-fill user details in debug mode
        if __debug__:
            self.builder.get_object("entry_your_name").set_text("John Boone")
            self.builder.get_object("entry_username").set_text("john")
            self.builder.get_object("entry_hostname").set_text("mars")
            self.builder.get_object("entry_userpass1").set_text("dummy_password")
            self.builder.get_object("entry_userpass2").set_text("dummy_password")

        # build partition list
        self.should_pulse = False

        # make sure we're on the right page (no pun.)

        if(fullscreen):
            # dedicated installer mode thingum
            self.window.maximize()
            self.window.fullscreen()

        # Initiate the slide show
        self.slideshow_path = "/usr/share/gooroom-guide/guide/"
        if os.path.exists(self.slideshow_path):
            self.slideshow_browser = WebKit2.WebView()
            self.slideshow_browser.connect('context_menu',self.disable_right_cb)
            s = self.slideshow_browser.get_settings()
            s.set_property('allow-file-access-from-file-urls', True)
            #s.set_property('enable-default-context-menu', False)
            self.builder.get_object("vbox_install").pack_start(self.slideshow_browser, True, True, 0)
            self.builder.get_object("vbox_install").show_all()

        self.partitions_browser = WebKit2.WebView()
        s = self.partitions_browser.get_settings()
        self.partitions_browser.connect('context_menu',self.disable_right_cb)
        s.set_property('allow-file-access-from-file-urls', True)
        #s.set_property('enable-default-context-menu', False)
        #self.partitions_browser.set_transparent(True)
        self.partitions_browser.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        self.builder.get_object("scrolled_partitions").add(self.partitions_browser)

        #to support 800x600 resolution
        #self.window.set_geometry_hints(
        #                            min_width=750, 
        #                            min_height=500, 
        #                            max_width=750, 
        #                            max_height=500, 
        #                            base_width=750, 
        #                            base_height=500)

        self.window.set_decorated(False)
        self.set_round_edges (self.window)
        self.window.set_app_paintable(True)
        self.set_window_size()
        self.window.show_all()

        self.assign_lang(self.builder.get_object("combobox_language"))
        self.button_back.hide()
        #self.fix_text_wrap()

        #to prevent duplication of partitioning
        self.PARTITIONING_DONE = False

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
        dialog.set_preview_widget(preview);
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
            self.face_button.set_picture_from_file(face_path)

        dialog.destroy()

    def _on_face_menuitem_activated(self, path):
        if os.path.exists(path):
            os.system("cp %s /tmp/live-installer-face.png" % path)
            print path
            return True

    def _on_face_take_picture_button_clicked(self, menuitem):
        # streamer takes -t photos
        if 0 != os.system('streamer -j90 -t8 -s800x600 -o /tmp/live-installer-face00.jpeg'):
            return  # Error, no webcam
        # Convert and resize the 7th frame (the webcam takes a few frames to "lighten up")
        os.system('convert /tmp/live-installer-face07.jpeg -crop 600x600+100+0 -resize 96x96 /tmp/live-installer-face.png')
        self.face_button.set_picture_from_file("/tmp/live-installer-face.png")

    def fix_text_wrap(self):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

        treeview_language_list_width = self.language_column.get_width()+ self.country_column.get_width()
        self.language_column.set_fixed_width(treeview_language_list_width/2)

        # this looks bad on resize, but to handle it on resize gracefully requires quite a bit of code (to keep from lagging)
        width = self.window.get_size()[0] - 75

        # custom install warning
        self.builder.get_object("label_custom_install_directions_1").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_directions_1").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_directions_2").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_directions_3").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_directions_4").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_directions_5").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_directions_6").set_size_request(width, -1)

        # custom install installation paused directions
        self.builder.get_object("label_custom_install_paused_1").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_paused_2").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_paused_3").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_paused_4").set_size_request(width, -1)
        self.builder.get_object("label_custom_install_paused_5").set_size_request(width, -1)

    def i18n(self):

        self.language_column.set_title(_("Language"))
        self.country_column.set_title(_("Country"))

        desc = _("We need several settings to install Hancom Gooroom. This will proceed to the environment, user information, and partition settings.As a first step, select the language, time zone, and keyboard environment you need for your environment.")
        self.wizard_pages[self.PAGE_SETTING] = WizardPage(_("Hancom Gooroom Installatiaion First Stage")
                ,desc)
        desc = _("Enter the user's information for logging in to the Hancom Gooroom. You can change user information in Settings after login.")
        self.wizard_pages[self.PAGE_USER] = WizardPage(_("Hancom Gooroom Installation Second Stage")
                , desc)
        desc = _("Set up a partition to install Hancom Gooroom. Edit the partition, refresh the information, and select the partition to be installed so you can proceed to the next step.")
        self.wizard_pages[self.PAGE_PARTITIONS] = WizardPage(_("Hancom Gooroom Installation Third Stage")
                , desc)
        desc = _("Ready for Hancom Gooroom installation. Please check the settings and install Hancom Gooroom.")
        self.wizard_pages[self.PAGE_OVERVIEW] = WizardPage(_("Ready to Install"), desc)
        self.wizard_pages[self.PAGE_INSTALL] = WizardPage(_("  "), "  ")
        self.wizard_pages[self.PAGE_FINISH] = WizardPage(_("  "), "  ")

        self.builder.get_object("button_cancel").set_label(_("Cancel"))
        self.builder.get_object("button_ok").set_label(_("OK"))
        #self.builder.get_object("button_quit").set_label(_("Quit"))
        self.button_back.set_label(_("Back"))
        self.button_next.set_label(_("Next"))
        self.button_key_test.set_label(_("Test Keyboard"))
        self.builder.get_object("button_stop").set_label(_("Cancel"))
        self.builder.get_object("button_reboot").set_label(_("Reboot"))

        self.builder.get_object("label_lang").set_label(_("Language"))
        self.builder.get_object("label_title1").set_markup("<span font='24px'><b>%s</b></span>" % _("Installing Hancom Gooroom"))
        desc = _("Keep your PC powered while installing Hancom Gooroom.")
        self.builder.get_object("label_description1").set_label(desc)
        self.builder.get_object("label_title2").set_markup("<span font='24px'><b>%s</b></span>" %_("Hancom Gooroom Installation Complete"))
        desc = _("Installation completed successfully.\nDo you want to restart your PC to use the new Hancom Gooroom?")
        self.builder.get_object("label_description2").set_label(desc)

        self.builder.get_object("key_test_window").set_title(_("Test Keyboard"))
        self.builder.get_object("label_key_test").set_label(_("Please enter a test letter."))

        self.button_label.set_markup(_("<span fgcolor='#000000'>I agree with</span> <span fgcolor='#0251ff'>the Software License.</span>"))

        self.builder.get_object("button_edit").set_label(_("Edit partitions"))
        self.builder.get_object("button_refresh").set_label(_("Refresh"))
        #self.builder.get_object("button_custommount").set_label(_("Expert mode"))
        self.label_your_name_help = "<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("Please enter your full name.")
        self.label_your_name_help2 = "<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("You cannot use 'root' as your full name.")
        self.label_your_name_help3 = "<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("Your full name must not be more than 32 characters.")
        self.builder.get_object("label_your_name").set_markup("<b>%s</b>" % _("Your full name"))
        self.builder.get_object("label_your_name_help").set_markup("<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("Please enter your full name."))
        self.label_username_help = "<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("This is the name you will use to log in to your computer.")
        self.label_username_help2 = "<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("It's recommended at least 1 to 32 letters which starts with lowcase alphabet and combined of lowcase alphabets, numbers and special character(-).")
        self.label_username_help3 = "<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("You cannot use 'root' as user name.")
        self.label_username_help4 = "<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("User name must not be more than 32 characters.")
        self.builder.get_object("label_username").set_markup("<b>%s</b>" % _("Your username"))
        self.builder.get_object("label_username_help").set_markup("<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("This is the name you will use to log in to your computer."))
        self.builder.get_object("label_choose_pass").set_markup("<b>%s</b>" % _("Your password"))
        self.builder.get_object("label_userpass2").set_markup("<b>%s</b>" % _("Confirm Password"))
        self.builder.get_object("label_pass_help").set_markup("<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("Please enter your password twice to ensure it is correct."))
        self.builder.get_object("label_hostname").set_markup("<b>%s</b>" % _("Hostname"))
        self.builder.get_object("label_hostname_help").set_markup("<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("This hostname will be the computer's name on the network."))

       # timezones # keyboard page
        self.builder.get_object("label_timezones").set_label(_("Timezone"))
        self.builder.get_object("label_kb_layout").set_label(_("Keyboard layout"))
        self.builder.get_object("label_kb_model").set_label(_("Keyboard Model"))

        # grub
        self.builder.get_object("label_grub1").set_markup("<b>%s</b>" % _("Bootloader"))
        self.builder.get_object("checkbutton_grub1").set_label(_("Install GRUB"))
        self.builder.get_object("label_grub_help1").set_label(_("GRUB is a bootloader used to load the Linux kernel."))

        # encrypt home
#self.builder.get_object("label_encrypt_home").set_markup("<b>%s</b>" % _("Encrypt home"))
        self.builder.get_object("radiobutton_ecryptfs").set_label(_("Ecryptfs (Kernel Level Encryption for Gooroom Platform recommendations)"))
        self.builder.get_object("radiobutton_encfs").set_label(_("Encfs (User Level Encryption for advanced users)"))
        self.builder.get_object("label_encfs").set_markup("<b>%s</b>" % _("Note: Because encfs encryption can cause unexpected errors,\n Installation is not recommended except for research purpose to verify the encryption function.\n"))


        # custom install warning
        self.builder.get_object("label_custom_install_directions_1").set_label(_("You have selected to manage your partitions manually, this feature is for ADVANCED USERS ONLY."))
        self.builder.get_object("label_custom_install_directions_2").set_label(_("Before continuing, please mount your target filesystem(s) at /target."))
        self.builder.get_object("label_custom_install_directions_3").set_label(_("Do NOT mount virtual devices such as /dev, /proc, /sys, etc on /target/."))
        self.builder.get_object("label_custom_install_directions_4").set_label(_("During the install, you will be given time to chroot into /target and install any packages that will be needed to boot your new system."))
        self.builder.get_object("label_custom_install_directions_5").set_label(_("During the install, you will be required to write your own /etc/fstab."))
        self.builder.get_object("label_custom_install_directions_6").set_label(_("If you aren't sure what any of this means, please go back and deselect manual partition management."))

        # custom install installation paused directions
        self.builder.get_object("label_custom_install_paused_1").set_label(_("Please do the following and then click Forward to finish installation:"))
        self.builder.get_object("label_custom_install_paused_2").set_label(_("Create /target/etc/fstab for the filesystems as they will be mounted in your new system, matching those currently mounted at /target (without using the /target prefix in the mount paths themselves)."))
        self.builder.get_object("label_custom_install_paused_3").set_label(_("Install any packages that may be needed for first boot (mdadm, cryptsetup, dmraid, etc) by calling \"sudo chroot /target\" followed by the relevant apt-get/aptitude installations."))
        self.builder.get_object("label_custom_install_paused_4").set_label(_("Note that in order for update-initramfs to work properly in some cases (such as dm-crypt), you may need to have drives currently mounted using the same block device name as they appear in /target/etc/fstab."))
        self.builder.get_object("label_custom_install_paused_5").set_label(_("Double-check that your /target/etc/fstab is correct, matches what your new system will have at first boot, and matches what is currently mounted at /target."))

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

    def assign_realname(self, entry, prop):
        self.setup.real_name = entry.props.text
        # Try to set the username (doesn't matter if it fails)
        try:
            text = entry.props.text.strip().lower()
            if " " in entry.props.text:
                elements = text.split()
                text = elements[0]
            self.setup.username = text
            self.builder.get_object("entry_username").set_text(text)
        except:
            pass

        if (self.setup.real_name == 'root'):
            self.builder.get_object("label_your_name_help").set_markup(self.     label_your_name_help2)
        elif (len(self.setup.real_name) > 32):
            self.builder.get_object("label_your_name_help").set_markup(self.     label_your_name_help3)
        else:
            self.builder.get_object("label_your_name_help").set_markup(self.     label_your_name_help)


        self.setup.print_setup()

    def assign_username(self, entry, prop):
        self.setup.username = entry.props.text
        if not re.match(r'^[a-z][-a-z0-9]*$', self.setup.username):
            self.builder.get_object("label_username_help").set_markup(self.label_username_help2)
        elif (self.setup.username == 'root'):
            self.builder.get_object("label_username_help").set_markup(self.label_username_help3)
        elif (len(self.setup.username) > 32):
            self.builder.get_object("label_username_help").set_markup(self.label_username_help4)
        else:
            self.builder.get_object("label_username_help").set_markup(self.label_username_help)
        self.setup.print_setup()

    def assign_hostname(self, entry, prop):
        self.setup.hostname = entry.props.text
        self.setup.print_setup()

    def quit_cb(self, widget, data=None):
        if QuestionDialog(_("Quit"), _("Are you sure you want to quit the installer?")):
            Gtk.main_quit()
            return False
        else:
            return True

    def set_window_cb(self, widget, maximize, data=None):
                
        if (widget.get_name()=="button_maximize"):
            if (maximize):
                self.window.maximize ();
                self.max_icon.set_from_file(self.resource_dir+"own/rest (3).svg")
                self.builder.get_object("button_maximize").connect_after("clicked", self.set_window_cb, False)
            else:
                self.window.unmaximize ();
                self.max_icon.set_from_file(self.resource_dir+"own/rest (1).svg")
                self.builder.get_object("button_maximize").connect_after("clicked", self.set_window_cb, True)
        else:
            self.window.iconify ();

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
        for line in commands.getoutput("isoquery --iso %s | cut -f1,4-" % iso_standard).split('\n'):
            ccode, cname = line.split(None, 1)
            countries[ccode] = cname

        #Load languages into memory
        languages = {}
        iso_standard = "639"
        if os.path.exists("/usr/share/xml/iso-codes/iso_639-2.xml"):
            iso_standard = "639-2"
        for line in commands.getoutput("isoquery --iso %s | cut -f3,4-" % iso_standard).split('\n'):
            cols = line.split(None, 1)
            if len(cols) > 1:
                name = cols[1].replace(";", ",")
                languages[cols[0]] = name
        for line in commands.getoutput("isoquery --iso %s | cut -f1,4-" % iso_standard).split('\n'):
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
        for locale in commands.getoutput("awk -F'[@ .]' '/UTF-8/{ print $1 }' /usr/share/i18n/SUPPORTED | uniq").split('\n'):
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
        
    def set_window_size(self):
        window = self.window.get_root_window()
        screen = window.get_screen()

        if ( screen.width() > 800 and screen.height() > 600):
            right_box = self.builder.get_object ("right_box")
            vbox = self.builder.get_object ("vbox20")
            progressbar = self.builder.get_object("progressbar")
            label = self.builder.get_object("label_install_progress")
            
            right_box.set_size_request (750,550);
            vbox.set_size_request (650,-1);
            progressbar.set_size_request (650,-1);
            label.set_width_chars (80);


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
         self.setup.keyboard_layout) = commands.getoutput("setxkbmap -query | awk '/^(model|layout)/{print $2}'").split()

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

    def assign_ecryptfs_install(self, radiobutton, data=None):
        if radiobutton.get_active():
            self.setup.ecryptfs = True
            self.setup.encfs = False

    def assign_encfs_install(self, radiobutton, data=None):
        if radiobutton.get_active():
            self.setup.encfs = True
            self.setup.ecryptfs = False

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
        treeview = self.builder.get_object("treeview_layouts")
        ''' Called whenever someone updates the keyboard layout '''
        model, active = treeview.get_selection().get_selected_rows()
        if not active:
            return
            (self.setup.keyboard_layout_description,
            self.setup.keyboard_layout) = model[active[0]]
        # Set the correct variant list model ...
        model = self.layout_variants[self.setup.keyboard_layout]
        self.builder.get_object("treeview_variants").set_model(model)
        # ... and select the first variant (standard)
        #self.builder.get_object("treeview_variants").set_cursor(0)

        self.builder.get_object("combobox_layout").set_model(model)
        self.builder.get_object("combobox_layout").set_active(0)

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
            self.setup.print_setup()

    def _generate_keyboard_layout_preview(self):
        filename = "/tmp/live-install-keyboard-layout.png"
        layout = self.setup.keyboard_layout.split(",")[-1]
        variant = self.setup.keyboard_variant.split(",")[-1]
        if variant == "":
            variant = None
        print("python /usr/lib/live-installer/frontend/generate_keyboard_layout.py %s %s %s" % (layout, variant, filename))
        os.system("python /usr/lib/live-installer/frontend/generate_keyboard_layout.py %s %s %s" % (layout, variant, filename))
        self.builder.get_object("image_keyboard").set_from_file(filename)
        return False

    def check_password(self, passwd):
        """ check password """

        #length
        if not passwd or len(passwd) < 8:
            return (-1, _("Password is short."))

        char_be = False
        digit_be = False
        special_be = False

        password_chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&()'

        for p in passwd:
            #valid char
            if not p in password_chars:
                return (-1, _("Invalid character is in password."))
            #security
            ord_p = ord(p)
            if (ord_p >=65  and ord_p <= 90) or (ord_p >= 97 and ord_p <= 122):
                char_be = True
            elif ord_p >= 48 and ord_p <= 57:
                digit_be = True
            else:
                special_be = True

        if char_be and digit_be and special_be:
            #success
            return (0, None)
        else:
            return (-1, _("Password security level is low."))

    def assign_password(self, widget):
        ''' Someone typed into the entry '''
        self.setup.password1 = self.builder.get_object("entry_userpass1").get_text()
        self.setup.password2 = self.builder.get_object("entry_userpass2").get_text()
        self.builder.get_object("label_pass_help").set_markup("<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("Please enter your password twice to ensure it is correct."))
        if(self.setup.password1 == "" and self.setup.password2 == ""):
            self.builder.get_object("image_mismatch").hide()
            self.builder.get_object("label_mismatch").hide()
        else:
            self.builder.get_object("image_mismatch").show()
            self.builder.get_object("label_mismatch").show()
            if(self.setup.password1 != self.setup.password2):
                self.builder.get_object("image_mismatch").set_from_stock(Gtk.STOCK_NO, Gtk.IconSize.BUTTON)
                self.builder.get_object("label_mismatch").set_markup("<span fgcolor='#3c3c3c'><sub><i>%s</i></sub></span>"%_("Passwords do not match."))
            else:
                p_res, err_msg = self.check_password(self.setup.password1)
                if p_res == 0:
                    self.builder.get_object("image_mismatch").set_from_stock(Gtk.STOCK_OK, Gtk.IconSize.BUTTON)
                    self.builder.get_object("label_mismatch").set_markup("<span fgcolor='#3c3c3c'><sub><i>%s</i></sub></span>"%_("Passwords match."))
                else:
                    self.builder.get_object("image_mismatch").set_from_stock(Gtk.STOCK_OK, Gtk.IconSize.BUTTON)
                    self.builder.get_object("label_mismatch").set_markup("<span fgcolor='#3c3c3c'><sub><i>%s</i></sub></span>"%_(err_msg))
                    self.builder.get_object("label_pass_help").set_markup("<span fgcolor='#3C3C3C'><sub><i>%s</i></sub></span>" % _("It should be more than 8 letters as a combination of alphabets, numbers and special characters(!@#$%^&amp;())."))
        self.setup.print_setup()

    def activate_page(self, index):
        title = _(self.wizard_pages[index].title)
        description = _(self.wizard_pages[index].description)

        self.builder.get_object("label_title").set_markup("<span font='24px'><b>%s</b></span>" % title)
        self.builder.get_object("label_description").set_markup("%s" % description)

        def current_page(idx):
            switch = {
                self.PAGE_SETTING:'setting_page',
                self.PAGE_USER:'user_page',
                self.PAGE_PARTITIONS:'partitions_page',
                self.PAGE_OVERVIEW:'overview_page',
                self.PAGE_INSTALL:'install_page',
                self.PAGE_FINISH:'finish_page'
            }
            return switch.get(idx,-1)

        self.builder.get_object('installer_stack').set_visible_child_name(current_page(index))

        # TODO: move other page-depended actions from the wizard_cb into here below
        if index == self.PAGE_USER:
            self.button_back.show()
        elif index == self.PAGE_PARTITIONS:
            self.setup.skip_mount = False
        elif index == self.PAGE_INSTALL:
            self.button_next.hide()
            self.button_back.hide()
            self.do_install()

    def wizard_cb(self, widget, goback, data=None):
        ''' wizard buttons '''
        sel = self.builder.get_object("notebook1").get_current_page()

        stack = self.builder.get_object('installer_stack')
        current_page = stack.get_visible_child_name()
        
        if (not goback):
            if (current_page == "setting_page"):
                if self.setup.language is None:
                    WarningDialog(_("Installation Tool"), _("Please choose a language"))
                self.activate_page(self.PAGE_USER)
                self.builder.get_object("entry_your_name").grab_focus()
            elif (current_page == "user_page"):
                errorFound = False
                errorMessage = ""
                p_res, err_msg = self.check_password(self.setup.password1)
                if(self.setup.real_name is None or self.setup.real_name == ""):
                    errorFound = True
                    errorMessage = _("Please provide your full name.")
                elif(self.setup.real_name == 'root'):
                    errorFound = True
                    errorMessage = _("Your full name is invalid.")
                elif(len(self.setup.real_name) > 32):
                    errorFound = True
                    errorMessage = _("Your full name is invalid.")
                elif(self.setup.username is None or self.setup.username == ""):
                    errorFound = True
                    errorMessage = _("Please provide a username.")
                elif not re.match(r'^[a-z][-a-z0-9]*$', self.setup.username):
                    errorFound = True
                    errorMessage = _("UserId is invalid.")
                elif (self.setup.username == 'root'):
                    errorFound = True
                    errorMessage = _("UserId is invalid.")
                elif (len(self.setup.username)> 32):
                    errorFound = True
                    errorMessage = _("UserId is invalid.")
                elif(self.setup.password1 is None or self.setup.password1 == ""):
                    errorFound = True
                    errorMessage = _("Please provide a password for your user account.")
                elif(self.setup.password1 != self.setup.password2):
                    errorFound = True
                    errorMessage = _("Your passwords do not match.")
                elif p_res != 0:
                    errorFound = True
                    errorMessage = err_msg
                elif(self.setup.hostname is None or self.setup.hostname == ""):
                    errorFound = True
                    errorMessage = _("Please provide a hostname.")
                else:
                    for char in self.setup.username:
                        if(char.isupper()):
                            errorFound = True
                            errorMessage = _("Your username must be lower case.")
                            break
                        elif(char.isspace()):
                            errorFound = True
                            errorMessage = _("Your username may not contain whitespace characters.")

                    for char in self.setup.hostname:
                        if(char.isupper()):
                            errorFound = True
                            errorMessage = _("The hostname must be lower case.")
                            break
                        elif(char.isspace()):
                            errorFound = True
                            errorMessage = _("The hostname may not contain whitespace characters.")

                if (errorFound):
                    WarningDialog(_("Installation Tool"), errorMessage)
                else:
                    self.activate_page(self.PAGE_PARTITIONS)
                    #to prevent duplication of partition
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
                self.activate_page(self.PAGE_USER)
            elif (current_page == "user_page"):
                self.button_back.hide()
                self.activate_page(self.PAGE_SETTING)

    def show_overview(self):
        bold = lambda str: '<b>' + str + '</b>'
        model = Gtk.TreeStore(str)
        self.builder.get_object("treeview_overview").set_model(model)
        top = model.append(None, (_("Localization"),))
        model.append(top, (_("Language: ") + bold(self.setup.language),))
        model.append(top, (_("Timezone: ") + bold(self.setup.timezone),))
        model.append(top, (_("Keyboard layout: ") +
                           "<b>%s - %s</b>" % (self.setup.keyboard_model_description, 
                                                  '(%s)' % self.setup.keyboard_variant_description if self.setup.keyboard_variant_description else ''),))
        top = model.append(None, (_("User settings"),))
        model.append(top, (_("Real name: ") + bold(self.setup.real_name),))
        model.append(top, (_("Username: ") + bold(self.setup.username),))
        #model.append(top, (_("Automatic login: ") + bold(_("enabled") if self.setup.autologin else _("disabled")),))
        top = model.append(None, (_("System settings"),))
        model.append(top, (_("Hostname: ") + bold(self.setup.hostname),))
        #top = model.append(None, (_("Encrypted home settings"),))
        #model.append(top, (_("Encrypted home: ") + bold(_("ecryptfs (Kernel Level Encryption)") if self.setup.ecryptfs else _("encfs (User Level Encryption)")),))
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
        self.button_next.show()
        self.button_back.show()
        self.button_next.set_sensitive(True)
        self.button_back.set_sensitive(True)
        self.builder.get_object("button_quit").set_sensitive(True)
        MessageDialog(_("Installation paused"), _("The installation is now paused. Please read the instructions on the page carefully before clicking Forward to finish the installation."))
        self.button_next.set_sensitive(True)

    @async
    def do_install(self):
        print " ## INSTALLATION "
        ''' Actually perform the installation .. '''
        inst = self.installer

        if __debug__:
            print " ## DEBUG MODE - INSTALLATION PROCESS NOT LAUNCHED"
            time.sleep(200)
            Gtk.main_quit()
            sys.exit(0)

        inst.set_progress_hook(self.update_progress)
        inst.set_error_hook(self.error_message)

        # do we dare? ..
        self.critical_error_happened = False

        # Start installing
        do_try_finish_install = True

        try:
            inst.init_install(self.setup)

        except Exception, detail1:
            print detail1
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
            except Exception, detail1:
                print detail1
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

            print " ## INSTALLATION COMPLETE "

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

    def is_consent(self, button):
        if button.get_active():
            self.button_next.set_sensitive(True)
            self.builder.get_object('agreement_stack').set_visible_child_name("show_agreement")
        else:
            self.button_next.set_sensitive(False)
            self.builder.get_object('agreement_stack').set_visible_child_name("hide_agreement")

    def button_go_setting_cb (self, button):
        self.builder.get_object('agreement_stack').set_visible_child_name("hide_agreement")

    def button_reboot_cb(self, button):
        os.system('reboot')
