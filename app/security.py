import hashlib
import hmac
import time
from . import config as c

def media_signature(clip_id, expires):
    return hmac.new(c.SECRET.encode(),(clip_id+':'+str(expires)).encode(),hashlib.sha256).hexdigest()

def media_url(clip_id):
    expires = int(time.time()+48*3600)
    return c.PUBLIC_URL+'/media/'+clip_id+'?expires='+str(expires)+'&signature='+media_signature(clip_id,expires)

def verify_media(clip_id, expires, signature):
    return expires > time.time() and hmac.compare_digest(media_signature(clip_id,expires),signature)
