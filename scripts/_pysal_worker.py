"""Isolated PySAL adapter using an explicitly supplied existing backend environment."""
import json
from pathlib import Path
import sys
import numpy as np
import esda
import libpysal
import esda.crand as crand
import esda.moran as moran
# PySAL 2.3.1 imports a Numba dtype even with JIT disabled; use its NumPy equivalent.
if hasattr(crand, "boolean"):
    crand.boolean = np.bool_
# esda 2.3.1 cannot reshape an empty island neighborhood in its no-JIT path.
# Zero neighbors imply zero local spatial lag for every permutation.
if esda.__version__ == '2.3.1':
    original_local = moran._moran_local_crand
    def local_with_islands(i, z, permuted_ids, weights_i, scaling):
        if len(weights_i) == 0:return np.zeros(permuted_ids.shape[0])
        return original_local(i, z, permuted_ids, weights_i, scaling)
    moran._moran_local_crand = local_with_islands


def main():
    data=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'));y=np.array(data['values']);neighbors={int(k):v for k,v in data['neighbors'].items()};seed=data['seed'];perms=data['permutations']
    def w():return libpysal.weights.W(neighbors,id_order=list(range(len(y))),silence_warnings=True)
    np.random.seed(seed);global_m=esda.Moran(y,w(),transformation='r',permutations=perms)
    local=esda.Moran_Local(y,w(),transformation='r',permutations=perms,seed=seed,n_jobs=1)
    np.random.seed(seed);gi=esda.G_Local(y,w(),transform='B',star=True,permutations=perms)
    # Keep library raw statistics; p-values are explicitly doubled folded tails.
    result=dict(global_I=float(global_m.I),global_p=float(min(1,2*global_m.p_sim)),local_I=local.Is.tolist(),local_quadrant=local.q.tolist(),local_p=np.minimum(1,2*local.p_sim).tolist(),gi_star=gi.Gs.tolist(),gi_z=gi.Zs.tolist(),gi_p=np.minimum(1,2*gi.p_sim).tolist(),esda=esda.__version__,libpysal=libpysal.__version__,numpy=np.__version__)
    def clean(x):
        if isinstance(x,dict):return {k:clean(v) for k,v in x.items()}
        if isinstance(x,list):return [clean(v) for v in x]
        if isinstance(x,float) and not np.isfinite(x):return None
        return x
    Path(sys.argv[2]).write_text(json.dumps(clean(result),allow_nan=False),encoding='utf-8')


if __name__=='__main__':main()
