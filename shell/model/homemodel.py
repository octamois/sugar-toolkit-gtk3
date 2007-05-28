# Copyright (C) 2006, Owen Williams.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import logging

import gobject
import wnck
import dbus

from model.homeactivity import HomeActivity
from model.homerawwindow import HomeRawWindow
from model import bundleregistry

_SERVICE_NAME = "org.laptop.Activity"
_SERVICE_PATH = "/org/laptop/Activity"
_SERVICE_INTERFACE = "org.laptop.Activity"

class HomeModel(gobject.GObject):
    """Model of the "Home" view (activity management)
    
    The HomeModel is basically the point of registration
    for all running activities within Sugar.  It traps
    events that tell the system there is a new activity
    being created (generated by the activity factories),
    or removed, as well as those which tell us that the
    currently focussed activity has changed.
    
    The HomeModel tracks a set of HomeActivity instances,
    which are tracking the window to activity mappings
    the activity factories have set up.
    """
    __gsignals__ = {
        'activity-launched':       (gobject.SIGNAL_RUN_FIRST,
                                    gobject.TYPE_NONE, 
                                   ([gobject.TYPE_PYOBJECT])),
        'activity-added':          (gobject.SIGNAL_RUN_FIRST,
                                    gobject.TYPE_NONE, 
                                   ([gobject.TYPE_PYOBJECT])),
        'activity-removed':        (gobject.SIGNAL_RUN_FIRST,
                                    gobject.TYPE_NONE,
                                   ([gobject.TYPE_PYOBJECT])),
        'active-activity-changed': (gobject.SIGNAL_RUN_FIRST,
                                    gobject.TYPE_NONE,
                                   ([gobject.TYPE_PYOBJECT]))
    }
    
    def __init__(self):
        gobject.GObject.__init__(self)

        self._activities = {}
        self._bundle_registry = bundleregistry.get_registry()
        self._current_activity = None

        screen = wnck.screen_get_default()
        screen.connect('window-opened', self._window_opened_cb)
        screen.connect('window-closed', self._window_closed_cb)
        screen.connect('active-window-changed',
                       self._active_window_changed_cb)
        bus = dbus.SessionBus()
        bus.add_signal_receiver(
            self._dbus_name_owner_changed_cb, 
            'NameOwnerChanged', 
            'org.freedesktop.DBus', 
            'org.freedesktop.DBus')

    def get_current_activity(self):
        return self._current_activity

    def __iter__(self): 
        ordered_acts = self._get_ordered_activities()
        return iter(ordered_acts)
        
    def __len__(self):
        return len(self._activities)
        
    def __getitem__(self, i):
        ordered_acts = self._get_ordered_activities()
        return ordered_acts[i]
        
    def index(self, obj):
        ordered_acts = self._get_ordered_activities()
        return ordered_acts.index(obj)
        
    def _get_ordered_activities(self):
        ordered_acts = self._activities.values()
        ordered_acts.sort(key=lambda a: a.get_launch_time())
        return ordered_acts
    
    def _window_opened_cb(self, screen, window):
        if window.get_window_type() == wnck.WINDOW_NORMAL:
            self._add_activity(window)

    def _window_closed_cb(self, screen, window):
        if window.get_window_type() == wnck.WINDOW_NORMAL:
            self._remove_activity(window.get_xid())
        if not self._activities:
            self.emit('active-activity-changed', None)
            self._notify_activity_activation(self._current_activity, None)

    def _dbus_name_owner_changed_cb(self, name, old, new):
        """Detect new activity instances on the DBus

        Normally, new activities are detected by
        the _window_opened_cb callback. However, if the
        window is opened before the dbus service is up,
        a RawHomeWindow is created. In here we create
        a proper HomeActivity replacing the RawHomeWindow.
        """
        if name.startswith(_SERVICE_NAME) and new and not old:
            xid = name[len(_SERVICE_NAME):]
            if not xid.isdigit():
                return
            logging.debug("Activity instance launch detected: %s" % name)
            xid = int(xid)
            act = self._get_activity_by_xid(xid)
            if isinstance(act, HomeRawWindow):
                logging.debug("Removing bogus raw activity %s for window %i" 
                              % (act.get_activity_id(), xid))
                self._internal_remove_activity(act)
                self._add_activity(act.get_window())

    def _get_activity_by_xid(self, xid):
        for act in self._activities.values():
            if act.get_launched() and act.get_xid() == xid:
                return act
        return None

    def _notify_activity_activation(self, old_activity, new_activity):
        if old_activity == new_activity:
            return

        if old_activity:
            service = old_activity.get_service()
            if service:
                service.set_active(False)

        if new_activity:
            service = new_activity.get_service()
            if service:
                service.set_active(True)

    def _active_window_changed_cb(self, screen):
        window = screen.get_active_window()
        if window == None:
            self.emit('active-activity-changed', None)
            self._notify_activity_activation(self._current_activity, None)
            return
        if window.get_window_type() != wnck.WINDOW_NORMAL:
            return

        xid = window.get_xid()
        act = self._get_activity_by_xid(window.get_xid())
        if act:
            if act.get_launched() == True:
                self._notify_activity_activation(self._current_activity, act)
                self._current_activity = act
            else:
                self._notify_activity_activation(self._current_activity, None)
                self._current_activity = None
                logging.error('Activity for window %d was not yet launched.' % xid)
        else:
            self._notify_activity_activation(self._current_activity, None)
            self._current_activity = None
            logging.error('Model for window %d does not exist.' % xid)

        self.emit('active-activity-changed', self._current_activity)

    def _add_window(self, window):
        home_window = HomeRawWindow(window)
        self._activities[home_window.get_activity_id()] = home_window
        self.emit('activity-added', home_window)

    def _add_activity(self, window):
        """Add the window to the set of windows we track
        
        At the moment this requires that something somewhere
        have registered a dbus service with the XID of the 
        new window that is used to bind the requested activity 
        to the window.
        
        window -- gtk.Window instance representing a new 
            normal, top-level window
        """
        bus = dbus.SessionBus()
        xid = window.get_xid()
        try:
            service = dbus.Interface(
                bus.get_object(_SERVICE_NAME + '%d' % xid,
                               _SERVICE_PATH + "/%s" % xid),
                _SERVICE_INTERFACE)
            
        except dbus.DBusException:
            service = None

        if not service:
            self._add_window(window)
            return

        activity = None
        act_id = service.get_id()
        act_type = service.get_service_name()
        if self._activities.has_key(act_id):
            activity = self._activities[act_id]
        else:
            # activity got lost, took longer to launch than we allow,
            # or it was launched by something other than the shell
            act_type = service.get_service_name()
            bundle = self._bundle_registry.get_bundle(act_type)
            if not bundle:
                raise RuntimeError("No bundle for activity type '%s'." % act_type)
                return
            activity = HomeActivity(bundle, act_id)
            self._activities[act_id] = activity

        activity.set_service(service)
        activity.set_window(window)
        self.emit('activity-added', activity)

    def _internal_remove_activity(self, activity):
        if activity == self._current_activity:
            self._current_activity = None

        self.emit('activity-removed', activity)
        act_id = activity.get_activity_id()
        del self._activities[act_id]
        
    def _remove_activity(self, xid):
        activity = self._get_activity_by_xid(xid)
        if activity:
            self._internal_remove_activity(activity)
        else:
            logging.error('Model for window %d does not exist.' % xid)

    def _activity_launch_timeout_cb(self, activity):
        act_id = activity.get_activity_id()
        if not act_id in self._activities.keys():
            return
        self._internal_remove_activity(activity)

    def notify_activity_launch(self, activity_id, service_name):
        bundle = self._bundle_registry.get_bundle(service_name)
        if not bundle:
            raise ValueError("Activity service name '%s' was not found in the bundle registry." % service_name)
        activity = HomeActivity(bundle, activity_id)
        activity.connect('launch-timeout', self._activity_launch_timeout_cb)
        self._activities[activity_id] = activity
        self.emit('activity-launched', activity)

    def notify_activity_launch_failed(self, activity_id):
        if self._activities.has_key(activity_id):
            activity = self._activities[activity_id]
            logging.debug("Activity %s (%s) launch failed" % (activity_id, activity.get_type()))
            self._internal_remove_activity(activity)
        else:
            logging.error('Model for activity id %s does not exist.' % activity_id)
