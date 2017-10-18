#!/bin/bash

apt-get update
apt-get install -y python python-dev python-pip

cd /opt/dev/tornado_http2_client
pip install -r requirements.txt