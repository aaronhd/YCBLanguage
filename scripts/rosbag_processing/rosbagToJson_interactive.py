import os
import argparse
import rosbag
import numpy as np
import json
import cv2
from cv_bridge import CvBridge 
import torch
import torch.nn as nn
import datetime as dt

now = dt.datetime.now()
json_filename = now.strftime("%m%d%Y") + '_data.json'

radius = 70 #radius in pixels for margin around center point
centerPt = []
center_select = False

t0_frame_depth = 0.0
t1_frame_depth = 0.0
t0_frame_rgb = 0.0
t1_frame_rgb = 0.0
t0_pooled_d = np.ndarray((100,100), np.uint8)
t1_pooled_d = np.ndarray((100,100), np.uint8)
t0_pooled_rgb = np.ndarray((100,100,3), np.uint8)
t1_pooled_rgb = np.ndarray((100,100,3), np.uint8)

class MyEncoder(json.JSONEncoder):
  def default(self, obj):
    if isinstance(obj, np.integer):
      return int(obj)
    elif isinstance(obj, np.floating):
      return float(obj)
    elif isinstance(obj, np.ndarray):
      return obj.tolist()
    else:
      return super(MyEncoder, self).default(obj)

def onMouse(event, x, y, flags, param):
  global centerPt, center_select

  # if the left mouse button was clicked, record the starting
  # (x, y) coordinates
  if event == cv2.EVENT_LBUTTONDOWN:
    
    if center_select is False:
      centerPt = (x, y)
      print(centerPt)

  # check to see if the left mouse button was released
  elif event == cv2.EVENT_LBUTTONUP:
    center_select = True

def getFeats_sample(img, centerPt, radius):
  feats = [ img[ centerPt[1],        centerPt[0] ], \
            img[ centerPt[1]+radius, centerPt[0] ], \
            img[ centerPt[1]-radius, centerPt[0] ], \
            img[ centerPt[1],        centerPt[0]+radius ], \
            img[ centerPt[1],        centerPt[0]-radius ] ] 
  return feats

def getTrialInfo(filename):
  div = filename.split('#')

  objA_name = div[0]
  objB_name = div[1]
  trialNum = div[2]
  return (objA_name, objB_name, trialNum)

def onExit(t0_frame_depth,t1_frame_depth,t0_frame_rgb,t1_frame_rgb,rosbag_filename):
  # pool depthmaps
  pool_d = nn.AvgPool2d((10,10),10)
  print(t0_frame_depth.shape)
  t0_pooled_d = pool_d(torch.from_numpy(t0_frame_depth.astype(np.float32)).unsqueeze(0)).squeeze()
  t1_pooled_d = pool_d(torch.from_numpy(t1_frame_depth.astype(np.float32)).unsqueeze(0)).squeeze()
  print(torch.from_numpy(t0_frame_depth.astype(np.float32)).shape)
  print(t0_pooled_d.shape)

  # pool rgbmap
  # permute the arrays to get them in the right format for pytorch (3 channels necessitates this)
  t0_frame_rgb = np.transpose(t0_frame_rgb, (2,0,1))
  t1_frame_rgb = np.transpose(t1_frame_rgb, (2,0,1))
  pool_rgb = nn.AvgPool2d((10,10),10)
  t0_pooled_rgb = pool_rgb(torch.from_numpy(t0_frame_rgb.astype(np.float32)).unsqueeze(0)).squeeze()
  t1_pooled_rgb = pool_rgb(torch.from_numpy(t1_frame_rgb.astype(np.float32)).unsqueeze(0)).squeeze()
  print(torch.from_numpy(t0_frame_rgb.astype(np.float32)).shape)
  print(t0_pooled_rgb.shape)

  # package up data to add to json file
  objA_name, objB_name, trialNum = getTrialInfo(rosbag_filename)
  key = '('+objA_name+', '+objB_name+')'
  print(key+" : "+str(trialNum))
  to_append_data = { key:
                      {
                      str(trialNum):
                          {'t0_depthmap': t0_pooled_d.numpy(),
                          't1_depthmap': t1_pooled_d.numpy(),
                          't0_rgbmap': t0_pooled_rgb.numpy(),
                          't1_rgbmap': t1_pooled_rgb.numpy(),
                          'center_point':(centerPt[0],centerPt[1])
                          }
                      }
                  }

  # load json database file
  if os.path.isfile(json_filename):
    with open(json_filename, 'r') as fp:
      db = json.load(fp)
  else:
    db = {}


  # append the data
  # if the first level key is already present (has an existing trial):
  if to_append_data.keys()[0] in db:
    db[to_append_data.keys()[0]].update(to_append_data.values()[0])
  else:
    db.update(to_append_data)

  # save the new appended dictionary
  with open(json_filename, 'w') as fp:
    json.dump(db, fp, sort_keys=True, indent=4, cls=MyEncoder)

def main(args):
    bridge = CvBridge()
    inbag = rosbag.Bag(str(args.infile))
    num_depth_msgs = inbag.get_message_count('/camera/depth/image_rect_raw')
    num_rgb_msgs = inbag.get_message_count('/camera/color/image_raw')
    t0_depth_capture = False
    t1_depth_capture = False
    t0_rgb_capture = False
    t1_rgb_capture = False

    msg_cntr = num_depth_msgs
    for topic, msg, t in inbag.read_messages('/camera/depth/image_rect_raw'):
	if msg.header.frame_id == 'camera_depth_optical_frame':
	    if (t0_depth_capture is False):
	        t0_frame_depth = bridge.imgmsg_to_cv2(msg, "16UC1")
	        t0_frame_depth = np.array(t0_frame_depth, dtype=np.uint8) 
	        t0_depth_capture = True
	    elif (t1_depth_capture is False) and (msg_cntr < 2):
	        t1_frame_depth = bridge.imgmsg_to_cv2(msg, "16UC1")
	        t1_frame_depth = np.array(t1_frame_depth, dtype=np.uint8) 
	        t1_depth_capture = True
	    elif msg_cntr < 1:
	        break
            msg_cntr -= 1

    msg_cntr = num_rgb_msgs
    for topic, msg, t in inbag.read_messages('/camera/color/image_raw'):
	if msg.header.frame_id == 'camera_color_optical_frame':
	    if (t0_rgb_capture is False):
	        t0_frame_rgb = bridge.imgmsg_to_cv2(msg, "bgr8")
	        t0_frame_rgb = np.array(t0_frame_rgb, dtype=np.uint8) 
	        t0_rgb_capture = True
	    elif (t1_rgb_capture is False) and (msg_cntr < 2):
	        t1_frame_rgb = bridge.imgmsg_to_cv2(msg, "bgr8")
	        t1_frame_rgb = np.array(t1_frame_rgb, dtype=np.uint8) 
	        t1_rgb_capture = True
	    elif msg_cntr < 1:
	        break
            msg_cntr -= 1

    cv2.imshow("t0 depth", t0_frame_depth)
    cv2.setMouseCallback("t0 depth", onMouse)
    bExit = False
    while bExit == False:
        cv2.imshow("t0 depth", t0_frame_depth)
        cv2.imshow("t1 depth", t1_frame_depth)
        cv2.imshow("t0 rgb", t0_frame_rgb)
        cv2.imshow("t1 rgb", t1_frame_rgb)
        cv2.moveWindow("t0 depth", 0, 500)
        cv2.moveWindow("t1 depth", 0, 0)
        cv2.moveWindow("t0 rgb", 640, 500)
        cv2.moveWindow("t1 rgb", 640, 0)

        kb = cv2.waitKey(10) & 0xFF
        # if the 'x' key is pressed, append the trials data to json file; break from the loop
        if kb == ord('x'):

          onExit(t0_frame_depth, t1_frame_depth, t0_frame_rgb, t1_frame_rgb, str(args.infile))
          bExit = True
          break

        # if the 'q' key is pressed, discard this trial, do not append to json file 
        if kb == ord('q'):
          break

if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--infile', type=str, required=True, help="name of rosbag file")
  main(parser.parse_args())
