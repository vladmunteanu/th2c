#!/bin/bash

apt-get update
apt-get install -y python python-dev python-pip
apt-get install -y golang-go

cd /opt/dev/th2c
pip install -r requirements.txt

# set GOPATH
echo "export GOPATH=/opt/dev/goworkspace/" > /etc/profile.d/gopath.sh

# get the http2 package
go get golang.org/x/net/http2
