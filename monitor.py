from apscheduler.schedulers.background import BackgroundScheduler
import datetime
import importlib
import gatt
import logging
import yaml
import subprocess

logging.basicConfig(filename='/var/log/turntouch.log',
        filemode='a',
        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
        level=logging.INFO)

logger = logging.getLogger('monitor')

manager = gatt.DeviceManager(adapter_name='hci0')


class TurnTouch(gatt.Device):

    button_codes = {
        b'\xff\x00': 'Off',
        b'\xfe\x00': 'North Press',
        b'\xef\x00': 'North Double',
        b'\xfe\xff': 'North Hold',
        b'\xfd\x00': 'East Press',
        b'\xdf\x00': 'East Double',
        b'\xfd\xff': 'East Hold',
        b'\xfb\x00': 'West Press',
        b'\xbf\x00': 'West Double',
        b'\xfb\xff': 'West Hold',
        b'\xf7\x00': 'South Press',
        b'\x7f\x00': 'South Double',
        b'\xf7\xff': 'South Hold'
    }

    button_presses = []

    battery_notifications_sent = []

    def __init__(self, mac_address, manager, buttons, name, controllers):
        super().__init__(mac_address, manager)
        self.sched = BackgroundScheduler()
        self.sched.start()
        self.button_actions = buttons
        self.listening = False
        self.name = name
        self.controllers = controllers

    def connect_succeeded(self):
        super().connect_succeeded()
        logger.info("Connected!")

    def connect_failed(self, error):
        super().connect_failed(error)
        logger.info("Connect failed with error {}".format(error))

    def services_resolved(self):
        super().services_resolved()
        button_status_service = next(s for s in self.services
                if s.uuid == '99c31523-dc4f-41b1-bb04-4e4deb81fadd')

        self.button_status_characteristic = next(c for c in button_status_service.characteristics
                if c.uuid == '99c31525-dc4f-41b1-bb04-4e4deb81fadd')

        self.button_status_characteristic.enable_notifications()

        battery_status_service = next(s for s in self.services
                if s.uuid.startswith('0000180f'))

        self.battery_status_characteristic = next(c for c in battery_status_service.characteristics
                if c.uuid.startswith('00002a19'))

        self.battery_status_characteristic.read_value()
        self.sched.add_job(self.battery_status_characteristic.read_value,
                trigger='interval', minutes=1) #todo: reduce this

    def characteristic_enable_notifications_succeeded(self, characteristic):
        super().characteristic_enable_notifications_succeeded(characteristic)
        logger.info("Connected to {}!".format(self.name))

    def characteristic_value_updated(self, characteristic, value):
        super().characteristic_value_updated(characteristic, value)
        if characteristic == self.battery_status_characteristic:
            percentage = int(int.from_bytes(value, byteorder='big') * 100/ 255)
            key = 'battery_{}'.format(percentage)
            if self.button_actions.get(key, False) and key not in self.battery_notifications_sent:
                self.battery_notifications_sent.append(key)
                self.perform('battery', str(percentage))
            logger.info('Battery status: {}%'.format(percentage))
            return
        if value == b'\xff\x00': #off
            return
        self.button_presses.append(value)
        if not self.listening:
            self.listening = True
            time = datetime.datetime.now() + datetime.timedelta(seconds=1)
            self.sched.add_job(self.deduplicate_buttons, trigger='date', run_date=time)

    def deduplicate_buttons(self):
        self.listening = False
        actions = [self.button_codes[p] for p in self.button_presses]
        # work out what to do
        first_words = [s.split(' ')[0] for s in actions]
        second_words = [s.split(' ')[1] for s in actions]
        self.button_presses = []
        if len(set(first_words)) != 1:
            logger.info("Too many presses too quickly")
            return
        direction = first_words[0]
        if 'Double' in second_words:
            self.perform(direction, 'Double')
        elif 'Hold' in second_words:
            self.perform(direction, 'Hold')
        else:
            self.perform(direction, 'Press')

    def perform(self, direction, action):
        logger.info("Performing {} {}".format(direction, action))
        action = self.button_actions.get("{}_{}".format(direction.lower(), action.lower()), {'type': 'none'})
        if action['type'] == 'none':
            return
        elif action['type'] in self.controllers:
            self.controllers[action['type']].perform(action)
        else:
            logger.info("No controller found for action {}".format(action['type']))

if __name__ == '__main__':
    try:
        with open('config.yml') as f:
            config = yaml.load(f)
            logger.info('Config loaded: {}'.format(config))
    except Exception as e:
        config = []
        logger.info("Error loading config: {}".format(e))
    for c in config:
        controllers = {}
        for t in set([b['type'] for _, b in c['buttons'].items()]):
            logger.info("Found command of type {}, trying to load controller".format(t))
            m = importlib.import_module('controllers.{}_controller'.format(t))
            controller = [k for k in m.__dict__.keys() if 'Controller' in k][0]
            controllers[t] = getattr(m, controller)()
        device = TurnTouch(
                mac_address=c['mac'],
                manager=manager,
                buttons=c['buttons'],
                name=c['name'],
                controllers=controllers
        )
        logger.info("Trying to connect to {} at {}...".format(c['name'], c['mac']))
        device.connect()
    manager.run()
