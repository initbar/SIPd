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
# worker.py
#-------------------------------------------------------------------------------

import logging
import time

from src.debug import create_random_uuid
from src.debug import md5sum
from src.logger import ContextLogger
from src.optimizer import memcache
from src.parser import convert_to_sip_packet
from src.parser import parse_address
from src.parser import parse_sip_packet
from src.parser import validate_sip_signature
from src.rtp.server import SynchronousRTPRouter
from src.sip.static.ok import SIP_OK
from src.sip.static.ok import SIP_OK_NO_SDP
from src.sip.static.options import SIP_OPTIONS
from src.sip.static.ringing import SIP_RINGING
from src.sip.static.terminated import SIP_TERMINATE
from src.sip.static.trying import SIP_TRYING
from src.sockets import unsafe_allocate_random_udp_socket

logger = ContextLogger(logging.getLogger())

# SIP responses
#-------------------------------------------------------------------------------

SIPTemplates = {
    'DEFAULT': SIP_OK_NO_SDP,
    'OK +SDP': SIP_OK,
    'OK -SDP': SIP_OK_NO_SDP,
    'OPTIONS': SIP_OPTIONS,
    'RINGING': SIP_RINGING,
    'TERMINATE': SIP_TERMINATE,
    'TRYING': SIP_TRYING
}

@memcache(size=32)
def generate_response(datagram, method):
    ''' memcached SIP message generator.
    @datagram<dict> -- SIP response datagram.
    @method<str> -- SIP response method.
    '''
    return convert_to_sip_packet(
        template=SIPTemplates.get(method, SIPTemplates['DEFAULT']),
        datagram=datagram)

def send_response(shared_socket, endpoint, datagram, method):
    ''' SIP message response sender.
    @shared_socket<socket> -- shared UDP socket.
    @endpoint<tuple> -- server endpoint address.
    @datagram<dict> -- parsed SIP datagram.
    @method<str> -- SIP method.
    '''
    # generate response and send to the SIP server.
    logger.debug('<<< <worker>:<%s>', method)
    response = generate_response(datagram, method)
    try:
        shared_socket.sendto(response, endpoint)
    except socket.error:
        shared_socket = None # unset to re-initialize at next iteration.

# SIP worker
#-------------------------------------------------------------------------------

class LazyWorker(object):
    ''' worker implementation.
    '''
    def __init__(self, name=None, settings=None, gc=None):
        '''
        @name<str> -- worker name.
        @settings<dict> -- `sipd.json`
        @gc<SynchronousGarbageCollector> -- shared garbage collector.
        '''
        if name is None:
            name = create_random_uuid()
        self.name = 'worker-' + str(name)

        self.settings = settings
        self.gc = gc

        self.socket = None # lazy initialize.
        self.rtp = None # lazy initialize.
        self.handlers = {
            'DEFAULT': self.handle_default,
            'ACK': self.handle_ack,
            'BYE': self.handle_cancel,
            'CANCEL': self.handle_bye,
            'INVITE': self.handle_invite
        }

        self.is_ready = True # recyclable state.
        logger.info('<worker>:successfully initialized %s.', self.name)

    def reset(self):
        ''' reset worker.
        '''
        self.call_id =\
        self.method =\
        self.datagram =\
        self.endpoint =\
        self.work = None
        self.is_ready = True

    def handle(self, work, endpoint):
        ''' worker logic.
        @work<str> -- worker "work".
        @endpoint<tuple> -- worker response endpoint.
        '''
        self.is_ready = False # woker is busy.
        logger.refresh() # create new context.

        if not work and not endpoint:
            logger.warning('<worker>:reset from incomplete work assignment.')
            self.reset()
            return
        else: # prepare worker.
            self.endpoint = endpoint
            # self.work = work
            if self.socket is None:
                self.socket = unsafe_allocate_random_udp_socket(is_reused=True)
            if self.rtp is None:
                self.rtp = SynchronousRTPRouter(self.settings)

        # validate work.
        if not validate_sip_signature(work):
            logger.warning("<worker>:reset from invalid signature: '%s'", work)
            self.reset()
            return
        self.datagram = parse_sip_packet(work)
        try: # override parsed headers with loaded headers.
            self.call_id = self.datagram['sip']['Call-ID']
            self.method = self.datagram['sip']['Method']
        except KeyError:
            logger.warning("<worker>:reset from invalid format: '%s'", work)
            self.reset()
            return

        # load eligible SIP headers from the configuration.
        sip_headers = self.settings['sip']['worker']['headers']
        for (field, value) in sip_headers.items():
            self.datagram['sip'][field] = value

        # set 'Contact' header to delegate future communication.
        server_address = self.settings['sip']['server']['address']
        self.datagram['sip']['Contact'] = '<sip:%s:5060>' % server_address

        try:
            logger.debug('>>> <worker>:<%s>', self.method)
            self.handlers[self.method]()
        except KeyError:
            self.handlers['DEFAULT']()
        finally:
            self.reset()

    #
    # custom handlers
    #

    def handle_default(self):
        send_response(
            self.socket,
            self.endpoint,
            self.datagram,
            'OK -SDP')

    def handle_ack(self):
        pass

    def handle_bye(self):
        send_response(self.socket, self.endpoint, self.datagram, 'OK -SDP')
        self.gc.register_new_task( # remove terminated call from garbage collector.
            lambda: self.gc.consume_membership(call_id=self.call_id, forced=True))
        send_response(self.socket, self.endpoint, self.datagram, 'TERMINATE')

    def handle_cancel(self):
        send_response(self.socket, self.endpoint, self.datagram, 'OK -SDP')
        try:
            self.rtp.handle(datagram=self.datagram, action='stop')
        except AttributeError:
            logger.error('<rtp>:RTP handler is down.')
            self.rtp = None # unset to re-initialize at next iteration.
        send_response(self.socket, self.endpoint, self.datagram, 'TERMINATE')

    def handle_invite(self):
        if self.call_id in self.gc.calls.history:
            logger.warning('<worker>:received duplicate call: %s', self.call_id)
            send_response(self.socket, self.endpoint, self.datagram, 'OK -SDP')
            return

        # receive TX/RX ports to delegate RTP packets.
        send_response(self.socket, self.endpoint, self.datagram, 'TRYING')
        chances = max(1, self.settings['rtp'].get('max_retry', 1))
        while chances:
            send_response(self.socket, self.endpoint, self.datagram, 'RINGING')
            # if external RTP handler replies with one or more ports, rewrite
            # and update the existing datagram with new information and respond.
            datagram = self.rtp.handle(self.tag, self.datagram)
            if datagram:
                send_response(self.socket, self.endpoint, datagram, 'OK +SDP')
                self.datagram = sip_datagram
                break
            else:
                logger.warning('<worker>:RTP handler did not send RX/TX information.')
                send_response(self.socket, self.endpoint, self.datagram, 'OK -SDP')
            # self.update_gc_callid()
            chances -= 1

    #
    # deferred garbage collection
    #

    # def update_gc_callid(self):
    #     ''' index new/existing Call-ID to garbage collector.
    #     '''
    #     try:
    #         lifetime = self.settings['gc']['call_lifetime']
    #         assert lifetime > 0
    #     except AssertionError:
    #         lifetime = 60 * 60 # seconds
    #     def deferred_update():
    #         # register session to the garbage collection queue.
    #         self.gc.garbage.append({
    #             'Call-ID': self.call_id,
    #             'tag': self.tag,
    #             'ttl': lifetime + int(time.time())
    #         })
    #         # register the first unique Call-ID membership.
    #         if not self.gc.membership.get(self.call_id):
    #             self.gc.calls_history[self.call_id] = None
    #             self.gc.calls_stats += (self.call_id in self.gc.calls_history)
    #             self.gc.membership[self.call_id] = {
    #                 'state': self.method,
    #                 'tags': [self.tag],
    #                 'tags_cnt': 1
    #             }
    #         else: # register session only for existing Call-ID membership.
    #             self.gc.membership[self.call_id]['tags'].append(self.tag)
    #             self.gc.membership[self.call_id]['tags_cnt'] += 1
    #     self.gc.register_new_task(deferred_update)
