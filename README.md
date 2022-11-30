Time Based MQTT Covers
======================

_Convert your dumb motorized covers to include position based on the speed of the motor_

This [AppDaemon](https://appdaemon.readthedocs.io/en/latest/#) app requires an MQTT broker to be configured in [Home Assistant](https://www.home-assistant.io/).

[![buy-me-a-coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/EvTheFuture)

## Quick Example

This is an example configuration that take two covers and convert them to smart(er) covers

**Please Note:** You need to change the entities to match your setup.
```
covers:
  module: timebasedmqttcover
  class: TimeBasedMQTTCover
  covers:
    - parent: cover.dumb_cover_1
      friendly_name: Smart Cover 1
      unique_id: tb_smart_over_1
      time_to_open: 22.9
      time_to_close: 23.61
      reaction_time: 0.15
    - parent: cover.dumb_cover_2
      friendly_name: Smart Cover 2
      unique_id: tb_smart_cover_2
      time_to_open: 31
      time_to_close: 30.4
      reaction_time: 0.15

```

