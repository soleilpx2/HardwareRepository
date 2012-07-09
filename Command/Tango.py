import logging
import Queue
import weakref
import new
import time
import types
import gevent

from ..CommandContainer import CommandObject, ChannelObject, ConnectionError
from .. import Poller
from .. import saferef

try:
    import PyTango
except ImportError:
    logging.getLogger('HWR').warning("Tango support is not available.")
else:
    # install our device proxy cache for PyTango
    _DeviceProxy = PyTango.DeviceProxy
    _DeviceProxy._subscribe_event = PyTango.DeviceProxy.subscribe_event
    _devices_cache = weakref.WeakValueDictionary()

    def DeviceProxy(device_name, *args):
      # return a proxy to a Tango device - if a proxy to the same
      # device already exists, it is returned from the cache
      device_name = device_name.lower()
      try:
        return _devices_cache[device_name]
      except KeyError:
        dev = _DeviceProxy(device_name, *args)
        dev._device_callbacks = {}
        class SuperCallback:
          def __init__(self, callbacks_dict):
            self.callbacks_dict = callbacks_dict
            self.last_events = {}
          def push_event(self, event):
            if event.attr_value is None:
                # an error occured, ignore bad event
                return
            attr_name = event.attr_value.name.lower()
            self.last_events[attr_name] = event
            callbacks = self.callbacks_dict[attr_name]
            for cb_ref in callbacks:
              cb = cb_ref()
              if cb is not None:
                try:
                  cb.push_event(event)
                except:
                  continue

        dev._super_callback = SuperCallback(dev._device_callbacks)

        _devices_cache[device_name] = dev

        return dev

    def good_subscribe_event(self, attribute_name, event_type, callback, *args):
      attribute_name = attribute_name.lower()
      if not attribute_name in self._device_callbacks:
        # first time registration
        self._device_callbacks[attribute_name] = [weakref.ref(callback)]
        self._subscribe_event(attribute_name, event_type, self._super_callback, *args)
        return
      self._device_callbacks[attribute_name].append(weakref.ref(callback))
      ev = self._super_callback.last_events.get(attribute_name)
      if ev is not None:
        callback.push_event(ev)

    _DeviceProxy.subscribe_event = new.instancemethod(good_subscribe_event, None, _DeviceProxy)
    PyTango.DeviceProxy = DeviceProxy
                    


class TangoCommand(CommandObject):
    def __init__(self, name, command, tangoname = None, username = None, **kwargs):
        CommandObject.__init__(self, name, username, **kwargs)
        
        self.command = command
        self.deviceName = tangoname
        self.device = None    
   
    def init_device(self): 
        try:
            self.device = PyTango.DeviceProxy(self.deviceName)
        except PyTango.DevFailed, traceback:
            last_error = traceback[-1]
            logging.getLogger('HWR').error("%s: %s", str(self.name()), last_error['desc'])
            self.device = None
        else:
            try:
                self.device.ping()
            except PyTango.ConnectionFailed:
                self.device = None
                raise ConnectionError
            

    def __call__(self, *args, **kwargs):
        self.emit('commandBeginWaitReply', (str(self.name()), ))
        if self.device is None:
            # TODO: emit commandFailed
            # beware of infinite recursion with Sample Changer
            # (because of procedure exception cleanup...)
            self.init_device()

        try:
            tangoCmdObject = getattr(self.device, self.command)
            ret = tangoCmdObject(*args) #eval('self.device.%s(*%s)' % (self.command, args))
        except PyTango.DevFailed, error_dict:
            logging.getLogger('HWR').error("%s: Tango, %s", str(self.name()), error_dict) 
        except:
            logging.getLogger('HWR').error("%s: an error occured when calling Tango command %s", str(self.name()), self.command)
        else:
            self.emit('commandReplyArrived', (ret, str(self.name())))
            return ret
        self.emit('commandFailed', (-1, self.name()))


    def abort(self):
        pass
        

    def isConnected(self):
        return self.device is not None

    
def processTangoEvents():
        while not TangoChannel._tangoEventsQueue.empty():
          try:
            ev = TangoChannel._tangoEventsQueue.get_nowait()
          except Queue.Empty:
            break
          else:
            try:
                receiverCbRef = TangoChannel._eventReceivers[id(ev)]
                receiverCb = receiverCbRef()
                if receiverCb is not None:
                    try:
                        receiverCb(ev.event.attr_value.value)
                    except AttributeError:
                        pass
            except KeyError:
                pass


class E:
          def __init__(self, event):
            self.event = event


class TangoChannel(ChannelObject):
    _tangoEventsQueue = Queue.Queue()
    _eventReceivers = {}
    _tangoEventsProcessingTimer = gevent.get_hub().loop.async()

    # start Tango events processing timer
    _tangoEventsProcessingTimer.start(processTangoEvents)
    
    def __init__(self, name, attribute_name, tangoname = None, username = None, polling=None, timeout=10000, **kwargs):
        ChannelObject.__init__(self, name, username, **kwargs)
 
        self.attributeName = attribute_name
        self.deviceName = tangoname
        self.device = None
        self.value = None
        self.polling = polling
        self.pollingTimer = None
        self.pollingEvents = False
        self.timeout = int(timeout)
        self.read_as_str = kwargs.get("read_as_str", False)
         
        logging.getLogger("HWR").debug("creating Tango attribute %s/%s, polling=%s, timeout=%d", self.deviceName, self.attributeName, polling, self.timeout)
        self.init_poller = Poller.poll(self.init_device,
                                       polling_period = 3000,
                                       value_changed_callback = self.continue_init,
                                       error_callback = self.init_poll_failed,
                                       start_delay=100)

    def init_poll_failed(self, e, poller_id):
        self.init_poller = self.init_poller.restart(3000)

    def continue_init(self, _):
        self.init_poller.stop()

        if type(self.polling) == types.IntType:
             Poller.poll(self.poll,
                         polling_period = self.polling,
                         value_changed_callback = self.update,
                         error_callback = self.pollFailed)
        else:
            if self.polling=="events":
                # try to register event
                try:
                    self.pollingEvents=True
                    #logging.getLogger("HWR").debug("subscribing to CHANGE event for %s", self.attributeName)
                    self.device.subscribe_event(self.attributeName, PyTango.EventType.CHANGE_EVENT, self, [], True)
                    #except PyTango.EventSystemFailed:            
                    #   pass
                except:
                    logging.getLogger("HWR").exception("could not subscribe event")


    def init_device(self):
        try:
            self.device = PyTango.DeviceProxy(self.deviceName)
        except PyTango.DevFailed, traceback:
            self.imported = False
            last_error = traceback[-1]
            logging.getLogger('HWR').error("%s: %s", str(self.name()), last_error['desc'])
        else:
            self.imported = True
            try:
                self.device.ping()
            except PyTango.ConnectionFailed:
                self.device = None
                raise ConnectionError
            else:
                self.device.set_timeout_millis(self.timeout)

                # check that the attribute exists (to avoid Abort in PyTango grrr)
                if not self.attributeName.lower() in [attr.name.lower() for attr in self.device.attribute_list_query()]:
                    logging.getLogger("HWR").error("no attribute %s in Tango device %s", self.attributeName, self.deviceName)
                    self.device = None
                
                   
    def push_event(self, event):
        #logging.getLogger("HWR").debug("%s | attr_value=%s, event.errors=%s, quality=%s", self.name(), event.attr_value, event.errors,event.attr_value is None and "N/A" or event.attr_value.quality)
        if event.attr_value is None or event.err or event.attr_value.quality != PyTango.AttrQuality.ATTR_VALID:
          #logging.getLogger("HWR").debug("%s, receving BAD event... attr_value=%s, event.errors=%s, quality=%s", self.name(), event.attr_value, event.errors, event.attr_value is None and "N/A" or event.attr_value.quality)
          return
        else:
          pass
          #logging.getLogger("HWR").debug("%s, receiving good event", self.name())
        ev = E(event)
        TangoChannel._eventReceivers[id(ev)] = saferef.safe_ref(self.update)
	TangoChannel._tangoEventsQueue.put(ev)
        TangoChannel._tangoEventsProcessingTimer.send()
       
 
    def poll(self):
        if self.read_as_str:
            value = self.device.read_attribute(self.attributeName, PyTango.DeviceAttribute.ExtractAs.String).value
            #value = self.device.read_attribute_as_str(self.attributeName).value
        else:
            value = self.device.read_attribute(self.attributeName).value
       
        return value


    def pollFailed(self, e, poller_id):
        try:
          raise e
        except:
          pass #logging.exception("Polling failed: %s", self.name())
        self.value = None
        self.emit('update', None)

        try:
            self.init_device()
        except:
            pass
        
        poller = Poller.get_poller(poller_id)
        if poller is not None:
            poller.restart(1000)


    def getInfo(self):
        return self.device.get_attribute_config(self.attributeName)

 
    def update(self, value = None):
        if value is None:
            value = self.getValue()
        if type(value) == types.TupleType:
          value = list(value)

        self.value = value
        self.emit('update', value)
        

    def getValue(self):
        with gevent.Timeout(3):
          while self.device is None:
            time.sleep(0.1) 

        if self.read_as_str:
           value = self.device.read_attribute(self.attributeName, PyTango.DeviceAttribute.ExtractAs.String).value
        else:
           value = self.device.read_attribute(self.attributeName).value
            
        return value

    
    def setValue(self, newValue):
        self.device.write_attribute(self.attributeName, newValue)
        #attr = PyTango.AttributeProxy(self.deviceName + "/" + self.attributeName)
        #a = attr.read()
        #a.value = newValue
        #attr.write(a)
       
 
    def isConnected(self):
        return self.device is not None

