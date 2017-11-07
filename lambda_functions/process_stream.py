from __future__ import print_function

import base64
import json
import io
import os
from datetime import datetime
import colorsys
from itertools import product
import boto3
from botocore.vendored import requests
from PIL import Image
import twitter


rekognition = boto3.client('rekognition')
s3 = boto3.resource('s3')
ssm = boto3.client('ssm')
unprocessed_bucket = s3.Bucket(os.getenv("UNPROCESSED_BUCKET"))
processed_bucket = s3.Bucket(os.getenv("PROCESSED_BUCKET"))

api = twitter.Api(*ssm.get_parameters(Names=[os.getenv("SSM_PARAMETER_NAME")])['Parameters'][0]['Value'].split(','))
TWITTER_SN = api.VerifyCredentials().screen_name

POSITIVE_STATUS = "JeffBarrized! {0}"
NEGATIVE_STATUS = "@{0} sorry I couldn't figure out how to jeffbarrize it :("
NSFW_STATUS = "@{0} sorry but that doesn't look SFW (sorry if I'm wrong)"


class InvalidPayloadException(Exception):
    pass
class NSFWException(Exception):
    pass
class MissingFaceException(Exception):
    pass

def build_s3_obj(item):
    """ Build a standard S3Object structure """
    return {
        'S3Object': {
            'Bucket': os.getenv("UNPROCESSED_BUCKET"),
            'Name': item['s3_key'],
        }
    }

def verify_nsfw(item):
    """ Raise if nudity is detected (via Rekognition) """
    s3obj = build_s3_obj(item)
    resp = rekognition.detect_moderation_labels(Image=s3obj, MinConfidence=50.)
    for label in resp['ModerationLabels']:
        if 'Explicit Nudity' in [label['Name'], label['Parent']]:
            raise NSFWException("NSFW!")


def get_faces(image):
    """ Detect faces via Rekognition """
    resp = rekognition.detect_faces(Image=image)
    if 'FaceDetails' in resp and len(resp['FaceDetails']):
        return resp['FaceDetails']
    else:
        raise MissingFaceException("No face detection")


def get_face_boxes(faces, source_size):
    """ Build a list of face bounding boxes """
    return [
        {
            'left': int(f['BoundingBox']['Left'] * source_size[0]),
            'top': int(f['BoundingBox']['Top'] * source_size[1]),
            'right': int((f['BoundingBox']['Left'] + f['BoundingBox']['Width']) * source_size[0]),
            'bottom': int((f['BoundingBox']['Top'] + f['BoundingBox']['Height']) * source_size[1]),
        }
        for f in faces
    ]

def center_faces(im, boxes):
    """ Crop the given image so that faces are centered """
    # find smallest box that contains faces
    new_box = [
        min(map(lambda box: box['left'], boxes)),
        min(map(lambda box: box['top'], boxes)),
        max(map(lambda box: box['right'], boxes)),
        max(map(lambda box: box['bottom'], boxes)),
    ]
    # compute minimum expansion (in px), applicable in every direction
    min_expansion = min([
        new_box[0],
        new_box[1],
        im.size[0] - new_box[2],
        im.size[1] - new_box[3],
    ])
    # adjust/expand new box (will keep faces centered)
    new_box[0] -= min_expansion
    new_box[1] -= min_expansion
    new_box[2] += min_expansion
    new_box[3] += min_expansion
    return im.crop(new_box)

def read_image_from_s3(source):
    """ Read a given image from S3 (return a PIL Image) """
    data = io.BytesIO()
    s3_obj = s3.Object(source['S3Object']['Bucket'], source['S3Object']['Name'])
    s3_obj.download_fileobj(data)
    return Image.open(data)

def img2str(im, output_format='JPEG'):
    """ Transform a given image into a string (in-memory) """
    buf = io.BytesIO()
    im.save(buf, output_format)
    return buf.getvalue()

def jeffbarrize(item):
    """
        A few things:
         - verify that at least 1 face is detected (w/ Rekognition)
         - read the original image (from S3)
         - extract face bounding boxes from Rekognition's response
         - center the image around faces, colorize, and paste into the default frame
    """
    source = build_s3_obj(item)
    faces = get_faces(source)  # may raise
    source_img = read_image_from_s3(source)
    boxes = get_face_boxes(faces, source_img.size)
    new_img = paste_into_frame(colorize(center_faces(source_img, boxes)))
    return img2str(new_img)

def colorize(im):
    """ Change HSV values to make the whole image look purple-ish """
    im = im.convert('RGBA')
    ld = im.load()
    width, height = im.size
    for x, y in product(range(width), range(height)):
        r, g, b, a = ld[x, y]
        h, s, v = colorsys.rgb_to_hsv(r / 255., g / 255., b / 255.)
        h = 0.82  # fixed hue
        s = s ** 0.65  # higher saturation
        v = v ** 1.3  # lower brightness
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        ld[x, y] = (int(r * 255.9999), int(g * 255.9999), int(b * 255.9999), a)
    return im

def paste_into_frame(im):
    """ Past the given image into the default frame (scaled accordingly) """
    frame = Image.open('frame.png')
    w, h = im.size
    w_new = 234  # new width is always 234
    h_new = w_new * h / w  # w:h=w_new:h_new
    new_top = 262 + (362 - h_new) / 2
    new_left = 206
    frame.paste(im.resize((w_new, h_new)), (new_left, new_top))
    return frame

def publish_positive_response(item, new_image):
    """ Tweet a positive status with the new image """
    processed_bucket.put_object(
        Body=new_image,
        Key=item['s3_key'],
        ACL='public-read',
    )
    s3_url = "{}/{}/{}".format(
        s3.meta.client.meta.endpoint_url,
        os.getenv("PROCESSED_BUCKET"),
        item['s3_key'],
    )
    mentions_str = ' '.join(item['mentions'])
    print("Publishing positive response on Twitter")
    print("URL: %s" % s3_url)
    api.PostUpdate(
        POSITIVE_STATUS.format(mentions_str),
        media=s3_url,
        in_reply_to_status_id=item['tid'],
    )


def publish_negative_response(item, status=NEGATIVE_STATUS):
    """ Tweet a negative status """
    print("Publishing negative response on Twitter")
    api.PostUpdate(status.format(item['sn']), in_reply_to_status_id=item['tid'])


def validate_record(payload):
    """ Will raise if anything's wrong with the payload """
    if not TWITTER_SN.lower() in payload.get('text', '').lower():
        raise InvalidPayloadException("Invalid text")
    if not payload.get('entities', {}).get('media'):
        raise InvalidPayloadException("Missing image")
    if 'RT' in payload.get('text'):
        raise InvalidPayloadException("Invalid RT")

def process_record(payload):
    """ Validate and transform each payload into a standard item object """
    validate_record(payload)  # may raise
    # build mentions list
    mentions = [
        '@' + mention['screen_name']
        for mention in payload['entities']['user_mentions']
        if TWITTER_SN[1:] not in mention['screen_name']
    ]
    # add tweet's user too
    mentions.append('@' + payload['user']['screen_name'])
    media = payload['entities']['media'][0]
    # build a unique s3 key based on date, user and tweet id
    now = datetime.utcnow()
    s3_key = "%s/%s/%s/%s/%s.jpg" % (
        now.year, now.month, now.day,
        payload['user']['screen_name'],
        str(media['id']),
    )
    # build item object
    item = {
        'mid': str(media['id']),
        'tid': payload['id'],
        'media': media['media_url'],
        'text': payload['text'],
        'sn': payload['user']['screen_name'],
        'mentions': set(mentions),  # unique mentions
        's3_key': s3_key,
    }
    # grab image from twitter
    resp = requests.get(item['media'] + ":large")
    # store unprocessed image in s3 bucket for rekognition to process
    unprocessed_bucket.put_object(
        Body=resp.content,
        Key=item['s3_key'],
        ACL='public-read',
    )
    return item


def lambda_handler(event, context):
    """ Lambda's entry point """
    for record in event['Records']:
        payload = json.loads(base64.b64decode(record['kinesis']['data']))
        print("Processing payload: %s" % payload)
        try:
            item = process_record(payload)
            verify_nsfw(item)
            new_image = jeffbarrize(item)
            publish_positive_response(item, new_image)
        except InvalidPayloadException as e:
            print(e.message)
            continue  # just skip it
        except NSFWException:
            publish_negative_response(item, NSFW_STATUS)
        except MissingFaceException:
            publish_negative_response(item)
