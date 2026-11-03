from pathlib import Path
import sys
import tempfile
import unittest
import importlib.util
import json

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('local_trace',SCRIPTS/'raster-local-trace.py')
T=importlib.util.module_from_spec(spec);spec.loader.exec_module(T)
import numpy as np
from PIL import Image,ImageDraw


class Trace(unittest.TestCase):
    def test_paths_cover_graph_edges_once(self):
        mask=np.zeros((20,20),bool)
        mask[3:15,7]=True;mask[8,3:15]=True
        g=T.graph(mask);p=T.paths(g)
        expected={tuple(sorted((a,b))) for a,bs in g.items() for b in bs}
        actual=[tuple(sorted((a,b))) for line in p for a,b in zip(line,line[1:])]
        self.assertEqual(set(actual),expected);self.assertEqual(len(actual),len(expected))

    def test_closed_loop_and_corner_shortcuts(self):
        mask=np.zeros((12,12),bool)
        mask[2,2:9]=True;mask[8,2:9]=True;mask[2:9,2]=True;mask[2:9,8]=True
        result=T.paths(T.graph(mask))
        self.assertEqual(len(result),1);self.assertEqual(result[0][0],result[0][-1])

    def test_route_refuses_bridge_and_ambiguous_seed(self):
        mask=np.zeros((10,10),bool);mask[2,1:8]=True;mask[6,1:8]=True
        g=T.graph(mask)
        with self.assertRaisesRegex(ValueError,'disconnected'):T.route(g,[1,2],[1,6],0)
        with self.assertRaisesRegex(ValueError,'Ambiguous'):T.route(g,[1,4],[7,2],3)
        line,info=T.route(g,[1,2],[7,2],0)
        self.assertEqual(len(line),7);self.assertEqual(info['start_distance'],0)

    def test_two_shores_pixel_origin_and_preservation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);image=root/'map.png';im=Image.new('RGB',(120,100),'white');draw=ImageDraw.Draw(im)
            draw.line([(30,30),(30,65)],fill='black',width=1)
            draw.line([(35,30),(35,65)],fill='black',width=1)
            im.save(image);raw=image.read_bytes()
            result=T.run(image,root/'output',[20,20,50,80],threshold=128,min_length=0,simplify=0)
            self.assertEqual(result['paths'],2)
            record=json.loads((root/'output/candidates.json').read_text(encoding='utf-8'))
            xs={p[0] for item in record['paths'] for p in item['geometry']['coordinates']}
            self.assertEqual(xs,{30,35});self.assertEqual(image.read_bytes(),raw)
            self.assertTrue((root/'output/labels-3x.png').exists())
            with self.assertRaises(ValueError):T.run(image,root/'bad',[20,20,50,80],start=[float('nan'),20],end=[30,40])

if __name__=='__main__':unittest.main()
