#!/bin/bash

apt-get update
apt-get install -y python python-dev python-pip
apt-get install -y golang-go

cd /opt/dev/th2c
pip install -r requirements.txt
