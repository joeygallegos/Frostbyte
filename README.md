Check logs:
sudo journalctl -u defrost.service -f

Check status:
sudo systemctl status defrost.service

MQTT lives on FruitSalad host

Test:
mosquitto_pub -h localhost -t home/ambient-audio -m '{"clip":"doorbell.wav","volume":95}'
mosquitto_pub -h fruitsalad -p 1883 -t home/ambient-audio -m '{"clip":"motion or pet.mp3","volume":100}'

Manually test speakers
speaker-test -D plughw:CARD=Headphones,DEV=0 -c 2
