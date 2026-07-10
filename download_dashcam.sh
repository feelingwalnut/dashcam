#!/bin/bash
nohup python3 ./dashcam/dashcamdownload.py \
  > ./dashcam/dashcam.log 2>&1 &
