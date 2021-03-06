import libvirt

import migrationmonitor.settings
import migrationmonitor.libvirt.watcher
import migrationmonitor.common.logger as log
from migrationmonitor.common.actor import defer
from migrationmonitor.libvirt import utils
from migrationmonitor.common.db import InfluxDBActor
from migrationmonitor.libvirt.watcher import LibvirtDomainsWatcher

EVENT_DETAILS = (
    ("Added", "Updated"),
    ("Removed",),
    ("Booted", "Migrated", "Restored", "Snapshot", "Wakeup"),
    ("Paused", "Migrated", "IOError", "Watchdog", "Restored",
     "Snapshot", "API error"),
    ("Unpaused", "Migrated", "Snapshot"),
    ("Shutdown", "Destroyed", "Crashed", "Migrated", "Saved",
     "Failed", "Snapshot"),
    ("Finished",),
    ("Memory", "Disk"),
    ("Panicked",),
)

EVENT_STRINGS = (
    "Defined",
    "Undefined",
    "Started",
    "Suspended",
    "Resumed",
    "Stopped",
    "Shutdown",
    "PMSuspended",
    "Crashed",
)

REASON_STRINGS = ("Error", "End-of-file", "Keepalive", "Client",)


class LibvirtMonitor(object):
    """libvirt live migration events monitoring actor
    """
    def __init__(self):
        self.connections = []
        self.migration_monitors = {}
        self.settings = migrationmonitor.settings
        self.db_actor = InfluxDBActor()
        self.db_actor.start()

    def start(self):
        """Start the actor
        """
        utils.start_event_loop()
        for uri in self.settings.LIBVIRT['URI']:
            self._start(uri)

    def _start(self, uri):
        conn = libvirt.openReadOnly(uri)
        log.info("Connecting to %s", uri)
        self.connections.append(conn)

        self._register_libvirt_callbacks(conn)

        doms_watcher_thread = LibvirtDomainsWatcher(
            conn,
            self.migration_monitors,
            self.db_actor)
        doms_watcher_thread.start()

    def _register_libvirt_callbacks(self, conn):
        conn.registerCloseCallback(self._conn_close_handler, None)
        conn.domainEventRegisterAny(
            None,
            libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
            self._domain_event_handler,
            None)
        conn.setKeepAlive(5, 3)

    def _domain_event_handler(self, conn, dom, event, detail, opaque):
        dom_id = dom.ID()
        dom_name = dom.name()
        log.info("====> (%s)%s %s %s",
                 dom_id,
                 dom_name,
                 EVENT_STRINGS[event],
                 EVENT_DETAILS[event][detail])

        # == Migration start events

        # Started Migrated (on dst)
        started_migrated = (event == 2 and detail == 1)

        # Suspended Paused (on src)
        # suspended_paused = (event == 3 and detail == 0)

        # == Migration end events

        # Resumed Migrated (on dst)
        # resumed_migrated = (event == 4 and detail == 1)

        # Stopped Migrated (on src)
        stopped_migrated = (event == 5 and detail == 3)

        # Stopped Failed (on dst)
        # stopped_failed = (event == 5 and detail == 5)

        boundary_event = stopped_migrated or started_migrated
        tags = {
            "domain_id": dom_id,
            "domain_name": dom_name,
            "event": EVENT_STRINGS[event],
            "event_detail": EVENT_DETAILS[event][detail]}
        values = {"value": 1 if boundary_event else 0}

        self.db_actor.tell((tags, values,
                            self.settings.INFLUXDB["EVENTS_MEASUREMENT"]))

    def _conn_close_handler(self, conn, reason, opaque):
        def _reconnect():
            uri = conn.getURI()
            log.debug("Reconnection %s" % (uri,))
            self.connections.remove(conn)
            self._start(uri)

        log.error("Closed connection: %s: %s",
                  conn.getURI(),
                  REASON_STRINGS[reason])

        if reason == 0:
            defer(_reconnect,
                  seconds=self.settings.INFLUXDB["RECONNECT"])

    def stop(self):
        """Stop the actor and release acquired resources
        """
        for dom_id in self.migration_monitors:
            dom_actor = self.migration_monitors[dom_id]
            dom_actor.stop()
            log.debug("Destroy DomJobMonitorActor for domain with id %s",
                      dom_id)

        self.db_actor.stop()
        for conn in self.connections:
            log.debug("Closing " + conn.getURI())
            conn.close()
