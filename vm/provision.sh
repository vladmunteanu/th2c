#!/bin/bash

apt-get update
apt-get install -y python python-dev python-pip
apt-get install -y golang-go

cd /opt/dev/th2c
pip install -r requirements.txt

# set GOPATH
GOPATH=/opt/dev/goworkspace
echo "GOPATH=$GOPATH" > "/etc/environment"

# get the http2 package
go get golang.org/x/net/http2
