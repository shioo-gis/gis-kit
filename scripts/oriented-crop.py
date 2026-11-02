#!/usr/bin/env python3
"""Non-generative oriented sample crop with invertible original-pixel mapping."""
import argparse
import json
import math
from pathlib import Path
import numpy as np
from PIL import Image
from _delivery import bundle,digest,write_json


def execute(source,params,output):
    p=json.loads(Path(params).read_text(encoding='utf-8'));before=digest(source)
    cx,cy=map(float,p['center_xy']);width,height=map(int,p['size_px']);angle=float(p['angle_deg']);scale=float(p.get('source_pixels_per_output_pixel',1))
    if not np.isfinite([cx,cy,angle,scale]).all() or scale<=0 or min(width,height)<=0 or [width,height]!=p['size_px'] or width*height>int(p.get('max_pixels',25000000)):raise ValueError('Invalid crop dimensions or mapping')
    theta=math.radians(angle);c,s=math.cos(theta)*scale,math.sin(theta)*scale
    matrix=np.array([[c,-s,cx-c*width/2+s*height/2],[s,c,cy-s*width/2-c*height/2],[0,0,1.]])
    with Image.open(source) as src:
        src.load();size=src.size
        corners=np.array([[0,0,1],[width,0,1],[width,height,1],[0,height,1]])@matrix.T
        if (corners[:,:2]<-1e-9).any() or (corners[:,0]>src.width+1e-9).any() or (corners[:,1]>src.height+1e-9).any():raise ValueError('Crop exceeds original image; padding is not evidence')
        mode=p.get('resampling','nearest')
        if mode not in ('nearest','bilinear'):raise ValueError('Unknown crop resampling')
        crop=src.convert('RGB').transform((width,height),Image.Transform.AFFINE,tuple(matrix[:2].ravel()),resample=Image.Resampling.NEAREST if mode=='nearest' else Image.Resampling.BILINEAR)
        with bundle(output) as out:
            crop.save(out/'crop.png')
            with Image.open(out/'crop.png') as reread:
                if not np.array_equal(np.asarray(crop),np.asarray(reread)):raise ValueError('Crop readback differs')
            write_json(out/'record.json',{'source_sha256':before,'source_size':size,'parameters':p,'output_to_source':matrix.tolist(),'source_to_output':np.linalg.inv(matrix).tolist(),'coordinate_convention':'image edge coordinates, pixel centers at x+0.5,y+0.5; y down; positive angle clockwise','derived':True,'resampling':mode,'artifact_sha256':digest(out/'crop.png')})
            if digest(source)!=before:raise ValueError('Source changed')
    return {'output':str(output)}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input');p.add_argument('--params',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    try:print(json.dumps(execute(a.input,a.params,a.output)))
    except (ValueError,KeyError,TypeError,OSError) as e:p.exit(1,f'ERROR: {e}\n')
if __name__=='__main__':main()
