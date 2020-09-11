import numpy as np
from scipy import sparse

from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator

from tqdm import tqdm
import bitstring

import n2
from sknetwork.hierarchy import Paris
from sknetwork.hierarchy import cut_balanced, cut_straight

class Evaluator(object):
    """Handles all the evaluation stuff for a given fingerprint setting."""
    def __init__(self, fingerprint, fpsize, smifile):
        """This class just wraps all the analysis together so that it's easier later to 
        evaluate multiple fingerprint types and regressors/classifiers using a common interface. 


        Parameters
        -----------
        fingerprint: str. One of: 'morgan'
        fpsize: int. Dimensions of the fingerprint. Rdkit will do the folding down to this size.
        smifile: str. A text file with a single column and a header. Each line below the header 
        is a single smiles code for a ligand. This comes from parse_data.py"""
        
        self.fingerprint_kind=fingerprint
        self.fpsize=fpsize
        self.fingerprint_function = self.get_fingerprint_function()
        self.smifile = smifile+'_short.smi'
        self.scorefile = smifile+'_short.npy'
        self.num_ligs = sum(1 for line in open(self.smifile))-1 #it comes in handy a few times to know how many ligands we have
        
    def get_fingerprint_function(self):
        """RDKit has lots of different ways to make fingerprits. 
        So this just returns the correct function for a given FP.

        No input since the fingerprint type is set during init"""
        if self.fingerprint_kind=='morgan':
            gen_mo = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=self.fpsize)
        return gen_mo.GetFingerprint
 
    
    def write_fingerprints(self):
        """Writes one of the rdkit fingerprints to a binary file (to save storage space)
        Probably a more modern option is HDF5 but this works too."""
        
        binfile = open('../processed_data/fingerprints.bin', 'wb') #writing to this file.
        smifile = open(self.smifile, 'r') #file containing the smiles codes.
        smifile.readline() #read past the header.

        #iterate through file, 
        for line in tqdm(smifile, total=self.num_ligs, smoothing=0):
            mol = Chem.MolFromSmiles(line[:-1])
            fp = self.fingerprint_function(mol)
            bs = bitstring.BitArray(bin=fp.ToBitString()) #turn it into binary.
            binfile.write(bs.bytes)

        binfile.close()
        smifile.close()


    def load_fingerprints(self):
        """Turns the fingerprints binary file into a numpary array of size (num_ligs, fpsize)
        It uses the bitstring library to read chunks of bits, where each chunk corresponds to one fp.
        Then you just read the long string of bits with numpy and reshape! Hacky but it works well."""
        
        fpfile = open('../processed_data/fingerprints.bin', 'rb')
        bits = ''
        for _ in range(self.num_ligs):
            fp_bytes = fpfile.read(int(self.fpsize/8)) 
            bits += bitstring.BitArray(bytes=fp_bytes).bin
        fpfile.close()

        self.fingerprints = (np.fromstring(bits,'u1') - ord('0')).reshape(self.num_ligs, -1)

    def build_ann_index(self, nthreads=1):
        """WARNING: set threads correctly! I set it to 1 so you don't run out of memory.
        This builds an approximate nearest neighbors index, used to build a kNN graph.

        n2 is a good choice because it is fast and also allows streaming upload. Further,
        it outperforms many other libraries according to ann_benchmarks. n2 is awesome.
        It does not, however, offer dice, jaccard, or tanimoto. In practice cosine works fine."""

        index = n2.HnswIndex(self.fpsize, "angular") # HnswIndex(f, "L2, euclidean, or angular")
        for fp in self.fingerprints:
            index.add_data(fp)
            
        index.build(n_threads=nthreads)
        index.save('../processed_data/n2_index.hnsw')
        
    def build_knn_graph(self, k):
        """Builds a kNN graph using the approx. NN index built earlier. In practice,
        in most nearest neighbor settings going above k=25 doesn't reall add any benefit."""
        
        index = n2.HnswIndex(self.fpsize, "angular")
        index.load('../processed_data/n2_index.hnsw')

        data = list()
        indices = list()
        indptr = list()
        count = 0
        indptr.append(count)
        
        for i in tqdm(range(self.num_ligs)):
            neighbor_idx = index.search_by_id(i,k,50, include_distances=True)[1:]
            for nidx, distance in neighbor_idx:
                data.append(1-distance)
                indices.append(nidx)
                count+=1
            indptr.append(count)

        self.adj = sparse.csr_matrix( ( data, indices, indptr), shape=(self.num_ligs, self.num_ligs), dtype=np.float16)

        #do a check that the values in the adjacency matrix are in the right place:
        for _ in range(50):
            idx = np.random.choice(self.num_ligs)
            adjacency_indices = self.adj[idx].indices
            adjacency_distances = 1-self.adj[idx].data
            query = index.search_by_id(idx, k, 50, include_distances=True)[1:]
            index_indices = [i[0] for i in query]
            index_distances = [i[1] for i in query]
            assert np.allclose(index_distances, adjacency_distances, atol=1e-3) #high tolerance because np.float16 conversion.
            assert np.allclose(adjacency_indices, index_indices)  
    

    def fit_paris(self):
        """ Uses a super useful library scikit-network to fit a PARIS clusterer on the kNN graph.
        PARIS clustering is hierarchical, so it returns a dendrogram instead of clusters. Later we cut the dendrogram.
        see: Hierarchical Graph Clustering using Node Pair Sampling by Bonald et al  https://arxiv.org/abs/1806.01664"""

        print('fitting:')
        paris = Paris()        
        paris.fit(self.adj)
        self.dendrogram = paris.dendrogram_
        print('Done')

    def cluster(self, method, n_clust=None, threshold=None):
        """Cuts the dendrogram and returns cluster IDs. Straight cuts can either
        set a defined number of clusters, or alternatively set a distance threshold. 
        Cluster sizes can vary widely.
        
        Balanced cuts respect a maximum cluster size. The number of clusters is determined 
        on the fly. """
        
        if method == 'straight':
            if n_clust is not None and threshold is not None:
                raise ValueError('Straight cut takes only one of n_clusters or threshold, not both.')
            self.clusters = cut_straight(self.dendrogram, n_clust, threshold)
        elif method == 'balanced':
            if n_clust is None:
                raise ValueError('Must set maximum cluster size (n_clust) for balanced_cut')
            self.clusters = cut_balanced(self.dendrogram, n_clust)
        else:
            print('Choose \"straight\" or \"balanced\"')

        
