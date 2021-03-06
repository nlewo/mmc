# -*- test-case-name: pulse2.msc.client.tests.control -*-
# -*- coding: utf-8; -*-
#
# (c) 2014 Mandriva, http://www.mandriva.com/
#
# This file is part of Pulse 2, http://pulse2.mandriva.org
#
# Pulse 2 is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# Pulse 2 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Pulse 2; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301, USA.

import os
import time
import logging
import platform
import urllib2
logging.basicConfig()

from threading import Thread
from Queue import Queue

from ptypes import CC
from ptypes import  Component, DispatcherFrame
from connect import ClientEndpoint
from connect import ConnectionTimeout, ConnectionRefused
from inventory import InventoryChecker, get_minimal_inventory
from vpn import VPNLaunchControl
from shell import Shell
from pexceptions import SoftwareCheckError



class SmartAgentQueues(object):
    """ Container storing all event queues """

    vpn = None
    stop = None
    awake = None
    inventory = None

    def __init__(self):

        self.vpn = Queue()
        self.stop = Queue()
        self.awake = Queue()
        self.inventory = Queue()


    def empty_all(self):
        for queue in self.__dict__.values():
            if isinstance(queue, Queue):
                queue.empty()


class SmartAgentThread(Thread):

    queue = None

    def __init__(self, config):
        super(self.__class__, self).__init__()

        self.queue = Queue()
        self.config = config



class Protocol(Component):
    # TODO - generalize and sychronize with server side
    __component_name__ = "protocol"

    def get_command(self, cmd):
        if cmd == "GET":
            return "packages.get_package"
        elif cmd == "TICK":
            return "scheduler.tick"
        elif cmd == "INVENTORY":
            return "inventory.process_inventory"


class InventorySender(Component):
    __component_name__ = "inventory_sender"

    def send(self):
        inventory = get_minimal_inventory()
        command = self.parent.protocol.get_command("INVENTORY")

        container = (command, inventory)
        response = self.parent.client.request(container)
        self.logger.debug("inventory: received response: %s" % response)
        return True


class InitialInstalls(Component):
    """Provides a simple downloader with following install etap"""

    __component_name__ = "initial_installs"

    def install(self, software):
        """
        Gets the requested from server and installs it.

        @param software: name of software
        @type software: str

        @return: True if download and install was successfull
        @rtype: bool
        """
        command = self.parent.protocol.get_command("GET")

        request = {"name": software,
                   "system": platform.system(),
                   "arch": platform.machine(),
                   }
        if platform.system() == "Linux":
            request["distro"] = platform.linux_distribution()[0]
            request["xserver"] = 1 if "DISPLAY" in os.environ else 0


        container = (command, request)

        commands = self.parent.client.request(container)
        self.logger.debug("received response: %s" % commands)

        for command in commands:
            self.logger.info("execute command: %s" % command)
            self.do_cmd(command)

        # TODO - include a delete phase



    def do_cmd(self, command):
        if "##server##" in command:
            command = command.replace("##server##",
                                      "http://%s" % self.config.server.host)
        if "##wget##" in command:
            url = command.replace("##wget##","")
            print "dwnld url: %s" % url
            self.download(url)
            return
        if "##tmp##" in command:

            command = command.replace("##tmp##", "").strip()
            command = os.path.join(self.temp_dir, command)
            self.logger.debug("execute command in temp: %s" % command)
            self.launch(command)
        else:
            self.launch(command)

    @property
    def temp_dir(self):
        if platform.system() == "Windows":
           return self.config.paths.package_tmp_dir_win
        else:
           return self.config.paths.package_tmp_dir_posix


    def download(self, url):
        filename = url.split('/')[-1]
        u = urllib2.urlopen(url)

        print "start download from url: %s" % url

        if not os.path.exists(self.temp_dir):
            os.mkdir(self.temp_dir)

        path = os.path.join(self.temp_dir, filename)
        f = open(path, 'wb')
        meta = u.info()
        filesize = int(meta.getheaders("Content-Length")[0])
        print "Downloading: %s bytes: %s" % (filename, filesize)

        file_size_dl = 0
        block_sz = 8192
        while True:
            buffer = u.read(block_sz)
            if not buffer:
                break

            file_size_dl += len(buffer)
            f.write(buffer)

        f.close()
        return path




    def unpack_to_tempfile(self, name, response):
        # TODO - distinct several serialisation backends
        import pickle

        path = os.path.join(self.config.paths.packages_temp_dir, name)
        f = open(name, "wb")

        pickle.dump(response, f)

        f.close()

        if os.path.exists(path):
            return path
        else:
            raise # something


    def launch(self, path):

        returncode = self.parent.shell.call(path)
        return True if returncode == 0 else False




class FirstRunEtap(Component):

    __component_name__ = "first_run_etap"

    def check_required(self):
        try:
            missing_software = self.parent.inventory_checker.check_missing()
        except SoftwareCheckError:
            self.logger.warn("Unable to continue, exit from first run step")
            return False

        except Exception, e:
            self.logger.warn("Unable to continue, another reason: %s" % str(e))
            return False

        for sw in missing_software:
            result = self.parent.initial_installs.install(sw)
            if not result:
                pass
                #raise SoftwareRequestError(sw)

        return True






class Dispatcher(DispatcherFrame):

    components = [Shell,
                  FirstRunEtap,
                  InitialInstalls,
                  InventoryChecker,
                  InventorySender,
                  VPNLaunchControl,
                  Protocol,
                  ]

    def __init__(self, config):
        """
        @param config: config container
        @type config: Config

        @param vpn_queue: queue to collect results from forked process
        @type vpn_queue: Queue.Queue
        """
        super(self.__class__, self).__init__(config)


    def _connect(self):
        """
        The client connection build.

        @return: True if connection was successfully
        @rtype: bool
        """

        try:
            self.client = ClientEndpoint(config)

            print "CLIENT: %s" % repr(self.client.socket)
            if not self.client.socket:

                return False
            return True

        except ConnectionRefused, exc:
            self.logger.error("Agent connection failed: %s" % repr(exc))
            return False

        except ConnectionTimeout, exc:
            self.logger.error("Agent connection failed: %s" % repr(exc))
            return False

        except Exception, exc:
            self.logger.error("Agent connection failed: %s" % repr(exc))
            return False


    def _threads_init(self):
        pass




    def start(self):
        if not self.config.vpn.enabled:
            # VPN connection not included, if server not available, exit
            return self._connect()
        else:
            if not self._connect():
                # server not available, try to establish a VPN connection
                #launch_vpn = VPNLaunchControl(self.config, self.queues.vpn)
                ret = self.vpn_launch_control.start()
                if ret == CC.VPN | CC.DONE:
                    # VPN established, try to contact the server
                    return self._connect()
                elif ret == CC.VPN | CC.FAILED:
                    # Unable to start VPN -> exit
                    self.logger.error("VPN client launching failed")
                    return False
                else:
                    self.logger.error("VPN client launching failed, another error")
            else:
                # first step succeed (direct connect without VPN)
                return True


    def mainloop(self):
        # connection establishing
        if not self.start():
            # TODO - stop queues, log something and exit
            return False

        if not self.first_run_etap.check_required():
            # TODO - stop queues, log something and exit
            return False


        if not self.inventory_sender.send():
            return False
        # looking for all needed softwares and install them if missing


        while True:
            self.logger.debug("waiting for next period")
            time.sleep(self.config.main.check_period)
            if not self.inventory_sender.send():
                return False

        #    try:
        #        if self.queues.stop.get(False):
        #            break
        #    except Empty:
        #        pass

if __name__ == "__main__":
    from config import Config

    config = Config()
    config.read("agent.ini")

    d = Dispatcher(config)
    d.mainloop()


