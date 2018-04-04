# Active recording Session Initiation Protocol daemon (sipd).
# Copyright (C) 2018  Herbert Shin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# https://github.com/initbar/sipd

#-------------------------------------------------------------------------------
# sockets.py -- synchronous and asynchronous socket handler module.
#-------------------------------------------------------------------------------

from random import randint

import logging
import socket

logger = logging.getLogger(__name__)

# network
#-------------------------------------------------------------------------------

def get_server_address():
    ''' https://stackoverflow.com/a/1267524
    '''
    try:
        return [
            (_socket.connect(('8.8.8.8', 53)),
             _socket.getsockname()[0],
             _socket.close())
            for _socket in [ socket.socket(socket.AF_INET, socket.SOCK_DGRAM) ]
        ][0][1] or [
            address
            for address in socket.gethostbyname_ex(socket.gethostname())[2]
            if not address.startswith("127.")
        ][0]
    except Exception as message:
        logger.error('failed to get server address: %s' % message)
        return '127.0.0.1'

# get random port number from unprivileged port range.
get_random_unprivileged_port = lambda: randint(1025, 65535)

# UDP sockets
#-------------------------------------------------------------------------------

def unsafe_allocate_udp_socket(host='127.0.0.1', port=None, timeout=1.0,
                               is_client=False,
                               is_reused=False):
    ''' create a UDP socket without automatic socket closure.
    '''
    # error checking for listening sockets.
    if not is_client and any([
            host not in ['127.0.0.1', 'localhost', '0.0.0.0'],
            not (1024 < port <= 65535 )]):
        logger.error('wrong socket allocation parameters: (%s,%s)' % (host, port))
        return

    # create a UDP socket.
    _socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    _socket.setblocking(False)
    if is_client: return _socket

    # reuse listening sockets.
    if is_reused:
        _socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
        _socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, True)

    try: # bind listening sockets.
        logger.debug('attempting to bind udp socket: %i.' % port)
        _socket.settimeout(timeout)
        _socket.bind((host, port))
        logger.debug('udp socket successfully binded.')
    except:
        logger.error('cannot bind udp socket to port: %i.' % port)
        return
    return _socket

# UDP clients
#-------------------------------------------------------------------------------

def unsafe_allocate_udp_client(timeout=1.0):
    ''' allocate a random UDP client that must be manually cleaned up.
    '''
    logger.info('attempting to create udp client.')
    try: return unsafe_allocate_udp_socket(is_client=True, timeout=timeout)
    finally: logger.info('successfully created udp client.')

class safe_allocate_udp_client(object):
    ''' allocate exception-safe random UDP client.
    '''
    def __init__(self, timeout=1.0):
        self.timeout = timeout
        self._socket = None

    def __enter__(self):
        self._socket = unsafe_allocate_udp_client(self.timeout)
        return self._socket

    def __exit__(self, type, value, traceback):
        try: self._socket.close()
        except:
            self._socket.shutdown()
            self._socket.close()
            del self._socket

# UDP server
#-------------------------------------------------------------------------------

def unsafe_allocate_random_udp_socket(is_reused=False):
    ''' allocate a random listening UDP socket that must be manually cleaned up.
    '''
    host, port = '0.0.0.0', get_random_unprivileged_port()
    while not locals().get('_socket'):
        try: _socket = unsafe_allocate_udp_socket(host=host, port=port, is_reused=is_reused)
        except: port = get_random_unprivileged_port()
    return _socket

class safe_allocate_random_udp_socket(object):
    ''' allocate exception-safe random listening UDP socket.
    '''
    def __init__(self, is_reused=False):
        self.is_reused = is_reused
        self._socket = None

    def __enter__(self):
        self._socket = unsafe_allocate_random_udp_socket(self.is_reused)
        return self._socket

    def __exit__(self, type, value, traceback):
        try: self._socket.close()
        except:
            self._socket.shutdown()
            self._socket.close()
            del self._socket
