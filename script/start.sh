#!/bin/sh
HERE=`dirname $0`

. ~/.virtualenvs/mlat/bin/activate
python3 $HERE/../mlat-server --work-dir ./output --client-listen 8123 --write-csv output/aircraft.csv